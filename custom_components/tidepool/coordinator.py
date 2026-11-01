"""Data update coordinators: the latest records and summaries of one patient.

A fast and a slow coordinator run per selected patient; all coordinators of a
config entry share the entry's single ``TidepoolClient`` (and so its single
auth strategy), and the same handling of refused or rate limited requests.
Log messages carry no user id, token, email or password: the coordinator name
is generic and API error messages only contain path patterns.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    AuthError,
    NotFoundError,
    RateLimitedError,
    TidepoolClient,
    TidepoolError,
)
from .const import (
    CONF_FAST_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DOMAIN,
    SLOW_INTERVAL,
)
from .models import (
    DAY,
    WINDOW_TYPES,
    Latest,
    Summary,
    merge_window,
    parse_latest,
    parse_summary,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class FastData:
    """What one fast refresh produces for one patient."""

    latest: Latest
    # The basal, bolus and food records of the last 24 hours, per type.
    window: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class SlowData:
    """What one slow refresh produces for one patient."""

    # Summary periods per kind (``cgm``, ``bgm``), keyed by period (``14d``).
    summaries: dict[str, dict[str, Summary]]
    data_sources: list[dict] = field(default_factory=list)
    data_sets: list[dict] = field(default_factory=list)
    profile: dict = field(default_factory=dict)

    def summary(self, kind: str, period: str) -> Summary | None:
        """Return one period of a summary, or None if Tidepool has none."""
        return self.summaries.get(kind, {}).get(period)


@dataclass
class TidepoolRuntime:
    """Runtime data of a loaded entry (``entry.runtime_data``)."""

    client: TidepoolClient
    fast: dict[str, TidepoolFastCoordinator]
    # One slow coordinator per patient whose device has entities.
    slow: dict[str, TidepoolSlowCoordinator]
    # Set by the first coordinator whose credentials are rejected; every other
    # coordinator of the entry then fails without calling the API until the
    # entry is reloaded (after reauth), so one bad password costs one login.
    auth_failed: bool = False
    # Device name per patient; a patient missing here gets no entities.
    names: dict[str, str] = field(default_factory=dict)


type TidepoolConfigEntry = ConfigEntry[TidepoolRuntime]


class _Withheld(Exception):
    """A shared patient's sharing does not include an optional resource."""


class TidepoolCoordinator[DataT](DataUpdateCoordinator[DataT]):
    """A coordinator of one patient that maps API failures to Home Assistant's.

    Subclasses fetch in ``_async_fetch``, calling the API through ``_call`` so
    a refused request is retried once per refresh and every failure is
    handled alike.
    """

    config_entry: TidepoolConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TidepoolConfigEntry,
        client: TidepoolClient,
        user_id: str,
        name: str,
        update_interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {name}",
            update_interval=update_interval,
        )
        self.client = client
        self.user_id = user_id
        # Whether this refresh already retried a refused request (signing in
        # again); a refresh never retries twice.
        self._retried = False

    async def _async_fetch(self) -> DataT:
        """Fetch this coordinator's data; implemented by subclasses."""
        raise NotImplementedError

    @override
    async def _async_update_data(self) -> DataT:
        runtime = self.config_entry.runtime_data
        if runtime.auth_failed:
            raise ConfigEntryAuthFailed("Tidepool credentials were rejected")
        self._retried = False
        try:
            return await self._async_fetch()
        except AuthError as err:
            if err.status == 403 and self.user_id != self.client.user_id:
                # Access to one shared patient was withdrawn; the login itself
                # still works, so the other patients carry on.
                raise UpdateFailed("access to this patient was refused") from err
            runtime.auth_failed = True
            raise ConfigEntryAuthFailed(str(err)) from err
        except RateLimitedError as err:
            raise UpdateFailed(
                str(err), retry_after=self._retry_after_seconds(err.retry_after)
            ) from err
        except TidepoolError as err:
            raise UpdateFailed(str(err)) from err

    async def _call[T](
        self, request: Callable[[], Awaitable[T]], *, retry_403: bool = True
    ) -> T:
        """Make a request, retrying once per refresh after a refused request.

        A refused request has already invalidated the session, so the retry
        signs in again silently (an expired session token). A rejected login
        is not retried: it would only repeat the wrong credentials. A 403 is
        retried unless ``retry_403`` is False. Once one request of a refresh
        was retried, a later refusal in the same refresh is not: a session
        refused right after signing in again needs reauth, not another login.
        """
        try:
            return await request()
        except AuthError as err:
            if err.from_login or self._retried or (err.status == 403 and not retry_403):
                raise
        self._retried = True
        return await request()

    async def _optional[T](
        self,
        request: Callable[[], Awaitable[T]],
        default: T,
        *,
        withhold: bool = False,
    ) -> T:
        """Make a request whose failure must not hide the rest of the refresh.

        A missing resource or a failing server leaves the default. Refusals and
        rate limits still count: they say something about the whole refresh.
        With ``withhold`` a 403 is not one of them (the patient's sharing just
        does not include this): it is not retried, so costs no extra login, and
        raises ``_Withheld``.
        """
        try:
            return await self._call(request, retry_403=not withhold)
        except AuthError as err:
            if withhold and err.status == 403 and not err.from_login:
                raise _Withheld from err
            raise
        except RateLimitedError:
            raise
        except TidepoolError as err:
            _LOGGER.debug("An optional request failed: %s", err)
            return default

    def _retry_after_seconds(self, retry_after: float | None) -> float | None:
        """Delay the next refresh until ``Retry-After``, never below the interval.

        The coordinator uses ``UpdateFailed.retry_after`` in place of the
        interval for the next refresh only.
        """
        if retry_after is None or self.update_interval is None:
            return None
        return max(retry_after, self.update_interval.total_seconds())


class TidepoolFastCoordinator(TidepoolCoordinator[FastData]):
    """Polls the latest record of each type for one patient."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TidepoolConfigEntry,
        client: TidepoolClient,
        user_id: str,
    ) -> None:
        minutes = entry.options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)
        super().__init__(
            hass, entry, client, user_id, "fast", timedelta(minutes=minutes)
        )

    @override
    async def _async_fetch(self) -> FastData:
        payload = await self._call(lambda: self.client.latest(self.user_id))
        latest = parse_latest(payload)
        # The whole 24 h is fetched again each time: pumps upload late, so
        # records can appear with times before the previous refresh. A failing
        # window request keeps the previous window rather than hiding latest.
        now = dt_util.utcnow()
        fetched = await self._optional(
            lambda: self.client.data(self.user_id, list(WINDOW_TYPES), now - DAY, now),
            [],
        )
        previous = self.data.window if self.data is not None else {}
        newest = [latest.by_type[t] for t in WINDOW_TYPES if t in latest.by_type]
        return FastData(
            latest=latest, window=merge_window(previous, [*fetched, *newest], now)
        )


class TidepoolSlowCoordinator(TidepoolCoordinator[SlowData]):
    """Polls the summaries, data sources, data sets and profile of one patient."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TidepoolConfigEntry,
        client: TidepoolClient,
        user_id: str,
    ) -> None:
        super().__init__(hass, entry, client, user_id, "slow", SLOW_INTERVAL)

    @override
    async def _async_fetch(self) -> SlowData:
        # An account without a kind of data has no summary of it (a 404).
        try:
            cgm = await self._call(lambda: self.client.summary(self.user_id, "cgm"))
        except NotFoundError:
            cgm = {}
        bgm = await self._optional(lambda: self.client.summary(self.user_id, "bgm"), {})
        data = SlowData(
            summaries={"cgm": parse_summary(cgm), "bgm": parse_summary(bgm)}
        )
        # A shared patient may withhold these (403); the rest is then skipped.
        withhold = self.user_id != self.client.user_id
        try:
            data.data_sources = await self._optional(
                lambda: self.client.data_sources(self.user_id), [], withhold=withhold
            )
            data.data_sets = await self._optional(
                lambda: self.client.data_sets(self.user_id), [], withhold=withhold
            )
            data.profile = await self._optional(
                lambda: self.client.profile(self.user_id), {}, withhold=withhold
            )
        except _Withheld:
            _LOGGER.debug("A shared patient withholds some optional data")
        return data
