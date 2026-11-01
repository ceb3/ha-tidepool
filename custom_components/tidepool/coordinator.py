"""Data update coordinators: the latest records and summaries of one patient.

A fast and a slow coordinator run per selected patient; all coordinators of a
config entry share the entry's single ``TidepoolClient`` (and so its single
auth strategy), and the same handling of refused or rate limited requests.
Log messages carry no user id, token, email or password: the coordinator name
is generic and API error messages only contain path patterns.

A withdrawn sign-in raises a repair issue for the entry, which the next
successful refresh clears.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import (
    TimestampDataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .api import (
    AuthError,
    NotFoundError,
    RateLimitedError,
    TidepoolClient,
    TidepoolError,
)
from .auth import LoginWithdrawnError
from .const import (
    CONF_FAST_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DOMAIN,
    ISSUE_LOGIN_WITHDRAWN,
    SIGN_IN_DOCS_URL,
    SLOW_INTERVAL,
)
from .models import (
    BASAL_LOOKBACK,
    DAY,
    LATEST_DEVICE_EVENTS,
    PRIME_LOOKBACK,
    WINDOW_TYPES,
    Latest,
    Summary,
    merge_window,
    newest_device_events,
    parse_latest,
    parse_summary,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class FastData:
    """What one fast refresh produces for one patient."""

    latest: Latest
    # The basal, bolus, food and device event records of the last 24 hours,
    # per type.
    window: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class SlowData:
    """What one slow refresh produces for one patient."""

    # Summary periods per kind (``cgm``, ``bgm``), keyed by period (``14d``).
    summaries: dict[str, dict[str, Summary]]
    data_sources: list[dict] = field(default_factory=list)
    data_sets: list[dict] = field(default_factory=list)
    profile: dict = field(default_factory=dict)
    # The newest alarm, reservoir change, status change and prime of each
    # target, keyed as ``models.newest_device_events`` keys them.
    device_events: dict[str, dict] = field(default_factory=dict)

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


def login_withdrawn_issue_id(entry_id: str) -> str:
    return f"{ISSUE_LOGIN_WITHDRAWN}_{entry_id}"


@callback
def async_raise_login_withdrawn(hass: HomeAssistant, entry_id: str) -> None:
    """Raise (or keep) the entry's repair issue: the sign-in is withdrawn.

    Nothing in Home Assistant fixes it, so it is not fixable; it links to the
    docs instead. The placeholders carry nothing about the account.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        login_withdrawn_issue_id(entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_LOGIN_WITHDRAWN,
        translation_placeholders={"docs_url": SIGN_IN_DOCS_URL},
        learn_more_url=SIGN_IN_DOCS_URL,
    )


@callback
def async_clear_login_withdrawn(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the entry's repair issue, if there is one."""
    ir.async_delete_issue(hass, DOMAIN, login_withdrawn_issue_id(entry_id))


class _Withheld(Exception):
    """A shared patient's sharing does not include an optional resource."""


class TidepoolCoordinator[DataT](TimestampDataUpdateCoordinator[DataT]):
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
        entry_id = self.config_entry.entry_id
        try:
            data = await self._async_fetch()
        except LoginWithdrawnError as err:
            async_raise_login_withdrawn(self.hass, entry_id)
            raise UpdateFailed(str(err)) from err
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
        # The request worked, so signing in works (again).
        async_clear_login_withdrawn(self.hass, entry_id)
        return data

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

        A missing resource or a failing server leaves the default. Refusals,
        rate limits and a withdrawn sign-in still count: they say something
        about the whole refresh.
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
        except RateLimitedError, LoginWithdrawnError:
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
        # The whole 24 h (plus the basal lookback) is fetched again each time:
        # pumps upload late, so records can appear with times before the
        # previous refresh. A failing window request keeps the previous window
        # rather than hiding latest.
        now = dt_util.utcnow()
        start = now - DAY - BASAL_LOOKBACK
        fetched = await self._optional(
            lambda: self.client.data(self.user_id, list(WINDOW_TYPES), start, now),
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
        # A 403 also ends the session, so a later request would sign in again:
        # the device events (``/data``, which the fast coordinator already
        # reads) go first and the rest stays as it was.
        withhold = self.user_id != self.client.user_id
        events: list[dict] = []
        try:
            await self._fetch_device_events(events, withhold)
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
        data.device_events = newest_device_events(events)
        return data

    async def _fetch_device_events(self, records: list[dict], withhold: bool) -> None:
        """Add the newest device events to ``records``: a request per subType.

        They are days apart (a reservoir or site change), so they come from
        here rather than the 24 h window.
        """
        now = dt_util.utcnow()
        requests = [
            lambda sub_type=sub_type: self.client.latest(
                self.user_id, "deviceEvent", sub_type
            )
            for sub_type in LATEST_DEVICE_EVENTS
        ]
        requests.append(
            lambda: self.client.data(
                self.user_id, ["deviceEvent"], now - PRIME_LOOKBACK, now, "prime"
            )
        )
        for request in requests:
            records.extend(await self._optional(request, [], withhold=withhold))
