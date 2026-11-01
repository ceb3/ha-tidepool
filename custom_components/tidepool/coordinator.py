"""Fast data update coordinator: the latest records of one patient.

One coordinator runs per selected patient; all coordinators of a config entry
share the entry's single ``TidepoolClient`` (and so its single auth strategy).
Log messages carry no user id, token, email or password: the coordinator name
is generic and API error messages only contain path patterns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import override

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, RateLimitedError, TidepoolClient, TidepoolError
from .const import CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL, DOMAIN
from .models import Latest, parse_latest

_LOGGER = logging.getLogger(__name__)


@dataclass
class FastData:
    """What one fast refresh produces for one patient."""

    latest: Latest
    # Recent records per type; filled by a later refresh step, empty for now.
    window: dict[str, list[dict]] = field(default_factory=dict)


@dataclass
class TidepoolRuntime:
    """Runtime data of a loaded entry (``entry.runtime_data``)."""

    client: TidepoolClient
    fast: dict[str, TidepoolFastCoordinator]
    # Set by the first coordinator whose credentials are rejected; every other
    # coordinator of the entry then fails without calling the API until the
    # entry is reloaded (after reauth), so one bad password costs one login.
    auth_failed: bool = False
    # Device name per patient; a patient missing here gets no entities.
    names: dict[str, str] = field(default_factory=dict)


type TidepoolConfigEntry = ConfigEntry[TidepoolRuntime]


class TidepoolFastCoordinator(DataUpdateCoordinator[FastData]):
    """Polls the latest record of each type for one patient."""

    config_entry: TidepoolConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: TidepoolConfigEntry,
        client: TidepoolClient,
        user_id: str,
    ) -> None:
        minutes = entry.options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} fast",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.user_id = user_id

    @override
    async def _async_update_data(self) -> FastData:
        runtime = self.config_entry.runtime_data
        if runtime.auth_failed:
            raise ConfigEntryAuthFailed("Tidepool credentials were rejected")
        try:
            payload = await self._async_latest_with_one_retry()
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
        return FastData(latest=parse_latest(payload))

    async def _async_latest_with_one_retry(self) -> list[dict]:
        """Fetch the latest records, retrying once after a refused request.

        A refused request has already invalidated the session, so the retry
        signs in again silently (an expired session token). A rejected login
        is not retried: it would only repeat the wrong credentials.
        """
        try:
            return await self.client.latest(self.user_id)
        except AuthError as err:
            if err.from_login:
                raise
        return await self.client.latest(self.user_id)

    def _retry_after_seconds(self, retry_after: float | None) -> float | None:
        """Delay the next refresh until ``Retry-After``, never below the interval.

        The coordinator uses ``UpdateFailed.retry_after`` in place of the
        interval for the next refresh only.
        """
        if retry_after is None or self.update_interval is None:
            return None
        return max(retry_after, self.update_interval.total_seconds())
