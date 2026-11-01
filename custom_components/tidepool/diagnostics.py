"""Diagnostics for a Tidepool config entry: shape and counts, nothing identifying.

The report is built from counts, flags, record type names and bucketed ages
only: no record content, user id, name, email, device id, serial number, time
or credential is ever copied into it. Patients are numbered in the order of the
entry (``patient_1``…). ``async_redact_data`` runs over it all the same, as a
second line of defence should a later change copy a raw value in.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import CONF_FAST_INTERVAL, CONF_PATIENTS, DEFAULT_FAST_INTERVAL, DOMAIN
from .coordinator import (
    TidepoolConfigEntry,
    TidepoolCoordinator,
    TidepoolFastCoordinator,
    TidepoolSlowCoordinator,
)
from .devices import child_devices
from .models import WINDOW_TYPES, parse_time

TO_REDACT = {
    "password",
    "email",
    "token",
    "tokens",
    "session_token",
    "access_token",
    "refresh_token",
    "userid",
    "user_id",
    "userId",
    "patients",
    "patient_id",
    "deviceId",
    "uploadId",
    "id",
    "serialNumber",
    "deviceSerialNumber",
    "transmitterId",
    "fullName",
    "name",
    "title",
    "unique_id",
}

# Upper bounds of the age buckets, with their labels.
_AGE_BUCKETS = (
    (timedelta(minutes=5), "under 5 min"),
    (timedelta(minutes=15), "under 15 min"),
    (timedelta(hours=1), "under 1 h"),
    (timedelta(hours=6), "under 6 h"),
    (timedelta(hours=24), "under 24 h"),
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TidepoolConfigEntry
) -> dict[str, Any]:
    """Return the shape of the entry and of each patient's data."""
    integration = await async_get_integration(hass, DOMAIN)
    patients: list[str] = entry.options.get(
        CONF_PATIENTS, entry.data.get(CONF_PATIENTS, [])
    )
    loaded = entry.state is ConfigEntryState.LOADED
    runtime = getattr(entry, "runtime_data", None) if loaded else None
    report: dict[str, Any] = {
        "version": str(integration.version),
        "entry": {
            "data_keys": sorted(entry.data),
            "option_keys": sorted(entry.options),
            "patient_count": len(patients),
            "fast_interval_minutes": entry.options.get(
                CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL
            ),
        },
        "loaded": runtime is not None,
        "auth_failed": bool(runtime and runtime.auth_failed),
        "per_patient": {},
    }
    if runtime is not None:
        now = dt_util.utcnow()
        for number, (user_id, fast) in enumerate(runtime.fast.items(), start=1):
            slow = runtime.slow.get(user_id)
            report["per_patient"][f"patient_{number}"] = {
                "own_account": user_id == entry.unique_id,
                "has_device": user_id in runtime.names,
                "fast": _fast(fast, now),
                "slow": None if slow is None else _slow(slow, now),
                "child_devices": _roles(fast, slow),
            }
    return async_redact_data(report, TO_REDACT)


def _age(when: datetime | None, now: datetime) -> str | None:
    """Bucket an age, so no time that could identify a record is reported."""
    if when is None:
        return None
    age = now - when
    return next((label for bound, label in _AGE_BUCKETS if age < bound), "over 24 h")


def _coordinator(coordinator: TidepoolCoordinator, now: datetime) -> dict[str, Any]:
    interval = coordinator.update_interval
    seconds = None if interval is None else int(interval.total_seconds())
    return {
        "last_update_success": coordinator.last_update_success,
        "update_interval_seconds": seconds,
        "last_update_age": _age(coordinator.last_update_success_time, now),
    }


def _fast(coordinator: TidepoolFastCoordinator, now: datetime) -> dict[str, Any]:
    report = _coordinator(coordinator, now)
    data = coordinator.data
    if data is None:
        return report | {
            "newest_record_age": None,
            "latest_types": [],
            "window_sizes": {},
        }
    times = [
        parse_time(record["time"])
        for record in data.latest.by_type.values()
        if isinstance(record.get("time"), str)
    ]
    return report | {
        "newest_record_age": _age(max(times, default=None), now),
        "latest_types": sorted(data.latest.by_type),
        "window_sizes": {
            record_type: len(data.window.get(record_type, []))
            for record_type in sorted(WINDOW_TYPES)
        },
    }


def _slow(coordinator: TidepoolSlowCoordinator, now: datetime) -> dict[str, Any]:
    report = _coordinator(coordinator, now)
    data = coordinator.data
    if data is None:
        return report
    return report | {
        "summary_periods": {
            kind: sorted(periods) for kind, periods in sorted(data.summaries.items())
        },
        "data_sources": len(data.data_sources),
        "data_sets": len(data.data_sets),
        "profile": bool(data.profile),
        "device_events": sorted(data.device_events),
    }


def _roles(
    fast: TidepoolFastCoordinator, slow: TidepoolSlowCoordinator | None
) -> dict[str, int]:
    """Count a patient's child devices per role (pump, CGM, other)."""
    if fast.data is None or slow is None or slow.data is None:
        return {}
    devices = child_devices(fast.data.latest, slow.data.data_sets)
    roles = Counter(device.role.value for device in devices.values())
    return dict(sorted(roles.items()))
