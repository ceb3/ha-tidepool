"""Services of the Tidepool integration: refresh now and import glucose history.

The services belong to the integration, not to an entry, so they are registered
once when the integration is set up and work with any number of entries.
Neither the log lines nor the errors carry a user id, token or other
identifier.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .api import AuthError, RateLimitedError, TidepoolError
from .const import (
    ATTR_DAYS,
    ATTR_ENTRY,
    DEFAULT_IMPORT_DAYS,
    DOMAIN,
    IMPORT_CHUNK,
    MAX_IMPORT_DAYS,
    SERVICE_IMPORT_HISTORY,
    SERVICE_REFRESH,
)
from .coordinator import TidepoolConfigEntry, TidepoolCoordinator
from .models import MGDL, MMOL, parse_glucose
from .statistics import async_import_cbg_statistics

_LOGGER = logging.getLogger(__name__)

REFRESH_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY): cv.string})
IMPORT_HISTORY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY): cv.string,
        vol.Optional(ATTR_DAYS, default=DEFAULT_IMPORT_DAYS): vol.All(
            int, vol.Range(min=1, max=MAX_IMPORT_DAYS)
        ),
    }
)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the services."""
    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_HISTORY,
        _async_import_history,
        schema=IMPORT_HISTORY_SCHEMA,
    )


def _error(key: str, **placeholders: str) -> dict:
    return {
        "translation_domain": DOMAIN,
        "translation_key": key,
        "translation_placeholders": placeholders or None,
    }


def _target_entries(
    hass: HomeAssistant, entry_id: str | None
) -> list[TidepoolConfigEntry]:
    """Return the entries a service call is for, which are ready to be used.

    A named entry that is not usable is an error. Without a name every loaded
    entry is used except those whose credentials were rejected: calling the API
    for them would only repeat the rejected login.
    """
    if entry_id is None:
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if not entry.runtime_data.auth_failed
        ]
        if not entries:
            raise ServiceValidationError(**_error("no_entries"))
        return entries
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise ServiceValidationError(**_error("entry_not_found"))
    if entry.state is not ConfigEntryState.LOADED:
        raise ServiceValidationError(**_error("entry_not_loaded"))
    if entry.runtime_data.auth_failed:
        raise ServiceValidationError(**_error("entry_auth_failed"))
    return [entry]


async def _async_refresh(call: ServiceCall) -> None:
    """Refresh the fast and slow data of every patient of the chosen entries.

    The coordinators refresh one after the other under their usual rules, so
    a rejected login stops the rest of its entry without another login.
    """
    coordinators: list[TidepoolCoordinator] = []
    for entry in _target_entries(call.hass, call.data.get(ATTR_ENTRY)):
        runtime = entry.runtime_data
        for user_id, fast in runtime.fast.items():
            coordinators.append(fast)
            if user_id in runtime.slow:
                coordinators.append(runtime.slow[user_id])
    for coordinator in coordinators:
        await coordinator.async_refresh()
    if not all(coordinator.last_update_success for coordinator in coordinators):
        raise HomeAssistantError(**_error("refresh_failed"))


class _HistoryImport:
    """One run of ``import_history``: windows, requests, and what was written."""

    def __init__(self, hass: HomeAssistant, days: int) -> None:
        self._hass = hass
        # Only whole hours are imported, so the window ends with the last one.
        self._end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        self._start = self._end - timedelta(days=days)
        self._unit = MGDL if hass.config.units is US_CUSTOMARY_SYSTEM else MMOL
        # Hours written so far, kept when a later request fails.
        self.hours = 0
        # Whether a refused request was already retried: once per run, as the
        # coordinators retry once per refresh.
        self._retried = False

    async def run(self, entry: TidepoolConfigEntry) -> None:
        """Import the window for every patient of the entry that has a device."""
        runtime = entry.runtime_data
        for user_id in runtime.fast:
            if user_id not in runtime.names:
                continue
            chunk_start = self._start
            while chunk_start < self._end:
                chunk_end = min(chunk_start + IMPORT_CHUNK, self._end)
                records = await self._fetch(entry, user_id, chunk_start, chunk_end)
                # The end of a request is inclusive: a reading on the boundary
                # comes with both neighbours but counts in the later one.
                readings = [
                    reading
                    for record in records
                    if isinstance(record, dict) and record.get("type") == "cbg"
                    if (reading := parse_glucose(record)) is not None
                    if chunk_start <= reading.time < chunk_end
                ]
                self.hours += async_import_cbg_statistics(
                    self._hass, user_id, readings, self._unit
                )
                chunk_start = chunk_end

    async def _fetch(
        self,
        entry: TidepoolConfigEntry,
        user_id: str,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        client = entry.runtime_data.client
        try:
            return await self._request(
                lambda: client.data(user_id, ["cbg"], start, end)
            )
        except AuthError as err:
            if err.status == 403 and user_id != client.user_id:
                # Access to one shared patient was withdrawn; the login works.
                raise HomeAssistantError(
                    **_error("patient_access_refused", hours=str(self.hours))
                ) from err
            entry.runtime_data.auth_failed = True
            entry.async_start_reauth(self._hass)
            raise HomeAssistantError(
                **_error("auth_failed", hours=str(self.hours))
            ) from err
        except RateLimitedError as err:
            raise HomeAssistantError(
                **_error("import_rate_limited", hours=str(self.hours))
            ) from err
        except TidepoolError as err:
            raise HomeAssistantError(
                **_error("import_failed", hours=str(self.hours))
            ) from err

    async def _request[T](self, request: Callable[[], Awaitable[T]]) -> T:
        """Make a request, retrying once per run after a refused request.

        The same rule as the coordinators': a refused request has already
        invalidated the session, so the retry signs in again, but a rejected
        login or a second refusal is final.
        """
        try:
            return await request()
        except AuthError as err:
            if err.from_login or self._retried:
                raise
        self._retried = True
        return await request()


async def _async_import_history(call: ServiceCall) -> None:
    """Write the hourly glucose statistics of the last ``days`` days.

    The window is fetched in 7 day requests, oldest first. A failing request
    stops the run; the hours of the requests before it stay written, and
    running the import again replaces them with the same values.
    """
    hass = call.hass
    days = call.data[ATTR_DAYS]
    # The schema bounds this; the handler does not rely on it.
    if not isinstance(days, int) or not 1 <= days <= MAX_IMPORT_DAYS:
        raise ServiceValidationError(
            **_error("invalid_days", maximum=str(MAX_IMPORT_DAYS))
        )
    entries = _target_entries(hass, call.data.get(ATTR_ENTRY))
    # Long-term statistics are the recorder's: without it there is nowhere to
    # write them, and the manifest only orders setup after it.
    if "recorder" not in hass.config.components:
        raise ServiceValidationError(**_error("recorder_unavailable"))

    history = _HistoryImport(hass, days)
    try:
        for entry in entries:
            await history.run(entry)
    finally:
        _LOGGER.info(
            "Imported %d hourly glucose statistics for the last %d days",
            history.hours,
            days,
        )
