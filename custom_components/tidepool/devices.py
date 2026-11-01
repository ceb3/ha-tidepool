"""Child devices of a patient: the pump, CGM and other devices behind the data.

A child device is registered for each device that produced one of the
patient's latest records, below the patient's device. Its name and model come
from the data sets (uploads) of that device. The raw device id only ever goes
into the registry identifiers: it often embeds a serial number, so it never
reaches a name, model, attribute or log line; serial numbers are not used.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import TidepoolConfigEntry
from .models import Latest, parse_time

UNKNOWN_MODEL = "Unknown device"
# Sorts data sets without a usable time first.
_NO_TIME = datetime.min.replace(tzinfo=UTC)


class DeviceRole(StrEnum):
    """What a child device is, from the types of record it produced."""

    PUMP = "pump"
    CGM = "cgm"
    OTHER = "other"


# Record types that make their device the pump or the CGM.
ROLE_TYPES = {
    DeviceRole.PUMP: ("basal", "bolus", "pumpSettings", "wizard"),
    DeviceRole.CGM: ("cbg", "cgmSettings"),
}
# Device names when no data set gives a model.
ROLE_NAMES = {
    DeviceRole.PUMP: "Insulin pump",
    DeviceRole.CGM: "CGM",
    DeviceRole.OTHER: "Data source",
}


@dataclass(frozen=True)
class ChildDevice:
    """A device of a patient, described by its newest data set."""

    role: DeviceRole
    manufacturer: str | None = None
    model: str | None = None

    @property
    def name(self) -> str:
        if self.model is None:
            return ROLE_NAMES[self.role]
        if self.manufacturer is None or self.model.casefold().startswith(
            self.manufacturer.casefold()
        ):
            return self.model
        return f"{self.manufacturer} {self.model}"


def patient_device_info(user_id: str, name: str) -> DeviceInfo:
    """The patient's own device; every child device hangs below it."""
    return DeviceInfo(
        identifiers={(DOMAIN, user_id)}, name=name, manufacturer="Tidepool"
    )


def child_identifier(user_id: str, device_id: str) -> tuple[str, str]:
    return (DOMAIN, f"{user_id}_{device_id}")


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_device(record: dict, uploads: dict[str, dict]) -> str | None:
    """The device of a record; records of phone apps only carry an upload id."""
    device_id = _text(record.get("deviceId"))
    if device_id is None:
        upload = uploads.get(_text(record.get("uploadId")) or "", {})
        device_id = _text(upload.get("deviceId"))
    return device_id


def _uploads(data_sets: list[dict]) -> dict[str, dict]:
    """Data sets by upload id; a data set's own id is its upload id."""
    return {
        upload_id: data_set
        for data_set in data_sets
        if isinstance(data_set, dict)
        and (upload_id := _text(data_set.get("uploadId") or data_set.get("id")))
    }


def upload_time(data_set: Any) -> datetime | None:
    """When a data set was uploaded, or None if it has no usable time."""
    if not isinstance(data_set, dict):
        return None
    raw = data_set.get("time") or data_set.get("createdTime")
    if not isinstance(raw, str):
        return None
    try:
        return parse_time(raw)
    except ValueError:
        return None


def _describe(role: DeviceRole, data_sets: list[dict]) -> ChildDevice:
    """Name a device from the newest of its data sets."""
    newest = max(data_sets, key=lambda s: upload_time(s) or _NO_TIME, default={})
    manufacturers = [
        name
        for name in newest.get("deviceManufacturers") or []
        if isinstance(name, str) and name.strip()
    ]
    return ChildDevice(
        role=role,
        manufacturer=", ".join(manufacturers) or None,
        model=_text(newest.get("deviceModel")),
    )


def child_devices(latest: Latest, data_sets: list[dict]) -> dict[str, ChildDevice]:
    """The devices behind the latest records, keyed by device id.

    A device that produced pump records is the pump even if it also reads
    glucose; one that produced CGM records is a CGM; any other is generic.
    """
    uploads = _uploads(data_sets)
    types: dict[str, set[str]] = {}
    for record_type, record in latest.by_type.items():
        if (device_id := _record_device(record, uploads)) is not None:
            types.setdefault(device_id, set()).add(record_type)
    devices = {}
    for device_id, produced in types.items():
        role = next(
            (r for r, role_types in ROLE_TYPES.items() if produced & set(role_types)),
            DeviceRole.OTHER,
        )
        own = [s for s in uploads.values() if _text(s.get("deviceId")) == device_id]
        devices[device_id] = _describe(role, own)
    return devices


def role_device(latest: Latest, data_sets: list[dict], role: DeviceRole) -> str | None:
    """The device id of the patient's pump or CGM: the one with the newest record."""
    uploads = _uploads(data_sets)
    records = [latest.by_type[t] for t in ROLE_TYPES[role] if t in latest.by_type]
    for record in sorted(records, key=lambda r: parse_time(r["time"]), reverse=True):
        if (device_id := _record_device(record, uploads)) is not None:
            return device_id
    return None


def identifiers_in_use(entry: TidepoolConfigEntry) -> set[tuple[str, str]]:
    """The identifiers of every patient device and of the child devices in the data."""
    runtime = entry.runtime_data
    in_use = {(DOMAIN, user_id) for user_id in runtime.fast}
    for user_id in runtime.names:
        latest = runtime.fast[user_id].data.latest
        data_sets = runtime.slow[user_id].data.data_sets
        in_use.update(
            child_identifier(user_id, device_id)
            for device_id in child_devices(latest, data_sets)
        )
    return in_use


@callback
def async_register_devices(
    hass: HomeAssistant, entry: TidepoolConfigEntry, user_id: str
) -> None:
    """Register (or rename) the patient device and each of its child devices.

    Child devices stay registered when they stop appearing in the data.
    """
    runtime = entry.runtime_data
    registry = dr.async_get(hass)
    patient = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **patient_device_info(user_id, runtime.names[user_id]),
    )
    latest = runtime.fast[user_id].data.latest
    for device_id, device in child_devices(
        latest, runtime.slow[user_id].data.data_sets
    ).items():
        registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={child_identifier(user_id, device_id)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model or UNKNOWN_MODEL,
            via_device_id=patient.id,
        )


@callback
def async_setup_devices(hass: HomeAssistant, entry: TidepoolConfigEntry) -> None:
    """Register every patient's devices now and again after each refresh.

    Runs before the platforms add entities, which link to these devices by
    identifier. The listeners are added first, so a refresh registers a new
    device before entities move to it.
    """
    runtime = entry.runtime_data
    for user_id in runtime.names:
        async_register_devices(hass, entry, user_id)
        for coordinator in (runtime.fast[user_id], runtime.slow[user_id]):
            entry.async_on_unload(
                coordinator.async_add_listener(
                    lambda user_id=user_id: async_register_devices(hass, entry, user_id)
                )
            )
