"""Alarm, meal and bolus events of a patient, from the 24 h window.

Each record fires its event once, when its id first shows up in the window.
A record uploaded late, with a time well before the newest seen record,
still fires once (it is new to us); the event carries the record's own time.

Seen ids are kept as a set, not as the newest seen id: records arrive out of
order, so an id or a time alone cannot tell a late upload from an old one.
The set lives in the entity's restore data, keyed by id with the record's
time, and is pruned to the window, so it stays small. Without restore data
(the entity's first load) the records already there are only marked seen:
history is never replayed. After a restart or reload the restored set marks
the records seen before it, and only records that arrived since fire.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Self, override

from homeassistant.components.event import (
    EventEntity,
    EventEntityDescription,
    EventExtraStoredData,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import FastData, TidepoolConfigEntry, TidepoolFastCoordinator
from .devices import DeviceRole
from .entity import TidepoolEntity
from .models import (
    ALARM_TYPES,
    DAY,
    alarm_type,
    delivered_bolus,
    net_carbs,
    parse_time,
)

PARALLEL_UPDATES = 0

# At most this many events fire per refresh, the newest ones; the others are
# only marked seen, so a long upload gap does not flood automations.
MAX_EVENTS_PER_REFRESH = 5
# Seen ids are kept a little longer than the window, so a record still in the
# window (pruned against an earlier "now") is never forgotten while there.
SEEN_KEEP = DAY + timedelta(hours=1)

# Bolus subtypes as the Tidepool data spec lists them, by event type.
BOLUS_TYPES = {
    "normal": "normal",
    "square": "square",
    "dual/square": "dual_square",
    "automated": "automated",
}


@dataclass(frozen=True, kw_only=True)
class TidepoolEventDescription(EventEntityDescription):
    """An event fired per new record of the 24 h window.

    ``records_fn`` picks the event's records (in time order) from the fast
    data; ``event_fn`` gives a record's event type and attributes.
    """

    records_fn: Callable[[FastData], list[dict]]
    event_fn: Callable[[dict], tuple[str, dict[str, Any]]]


def _time(record: dict) -> str:
    # Window records always have a usable time; merging checked it.
    return parse_time(record["time"]).isoformat()


def _alarm(record: dict) -> tuple[str, dict[str, Any]]:
    kind = alarm_type(record)
    return kind, {"alarm_type": kind, "time": _time(record)}


def _meal(record: dict) -> tuple[str, dict[str, Any]]:
    attrs: dict[str, Any] = {"time": _time(record)}
    if (carbs := net_carbs(record)) is not None:
        attrs["carbohydrates"] = carbs
    return "meal", attrs


def _bolus(record: dict) -> tuple[str, dict[str, Any]]:
    sub_type, context = record.get("subType"), record.get("deliveryContext")
    kind = BOLUS_TYPES.get(sub_type, "other") if isinstance(sub_type, str) else "other"
    return kind, {
        "units": delivered_bolus(record),
        "time": _time(record),
        "context": context if isinstance(context, str) else None,
    }


EVENTS = (
    TidepoolEventDescription(
        key="alarm",
        translation_key="alarm",
        event_types=list(ALARM_TYPES),
        records_fn=lambda data: [
            r for r in data.window.get("deviceEvent", []) if r.get("subType") == "alarm"
        ],
        event_fn=_alarm,
    ),
    TidepoolEventDescription(
        key="meal",
        translation_key="meal",
        event_types=["meal"],
        records_fn=lambda data: data.window.get("food", []),
        event_fn=_meal,
    ),
    TidepoolEventDescription(
        key="bolus",
        translation_key="bolus",
        event_types=[*BOLUS_TYPES.values(), "other"],
        records_fn=lambda data: data.window.get("bolus", []),
        event_fn=_bolus,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TidepoolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the events of every selected patient."""
    runtime = entry.runtime_data
    async_add_entities(
        TidepoolEvent(runtime.fast[user_id], description, name)
        for user_id, name in runtime.names.items()
        for description in EVENTS
    )


@dataclass
class SeenEventsData(EventExtraStoredData):
    """The event's own restore data plus the record ids seen (id: time)."""

    seen: dict[str, str] = field(default_factory=dict)

    @classmethod
    @override
    def from_dict(cls, restored: dict[str, Any]) -> Self | None:
        base = EventExtraStoredData.from_dict(restored)
        seen = restored.get("seen")
        if base is None or not isinstance(seen, dict):
            return None
        return cls(
            base.last_event_type,
            base.last_event_attributes,
            {k: v for k, v in seen.items() if _parses(v)},
        )


def _parses(value: Any) -> bool:
    """Whether a restored seen time is a usable timestamp."""
    if not isinstance(value, str):
        return False
    try:
        parse_time(value)
    except ValueError:
        return False
    return True


class TidepoolEvent(TidepoolEntity[TidepoolFastCoordinator], EventEntity):
    """Fires once per new record of a kind; on the pump device once there is one."""

    _device_role = DeviceRole.PUMP
    entity_description: TidepoolEventDescription
    # None until the records already there are marked seen.
    _seen: dict[str, str] | None = None

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_extra_data()
        if restored is not None:
            data = SeenEventsData.from_dict(restored.as_dict())
            self._seen = None if data is None else data.seen
        self._async_fire_new()

    @property
    @override
    def extra_restore_state_data(self) -> SeenEventsData:
        base = super().extra_restore_state_data
        return SeenEventsData(
            base.last_event_type, base.last_event_attributes, dict(self._seen or {})
        )

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        self._async_fire_new()
        super()._handle_coordinator_update()

    @callback
    def _async_fire_new(self) -> None:
        """Fire the records not seen before, the newest few at most."""
        description = self.entity_description
        records = description.records_fn(self.coordinator.data)
        first_load = self._seen is None
        seen = self._seen or {}
        new = [r for r in records if r["id"] not in seen]
        seen.update((r["id"], _time(r)) for r in new)
        cutoff = dt_util.utcnow() - SEEN_KEEP
        self._seen = {k: t for k, t in seen.items() if parse_time(t) >= cutoff}
        if first_load:
            return
        for record in new[-MAX_EVENTS_PER_REFRESH:]:
            self._trigger_event(*description.event_fn(record))
            self.async_write_ha_state()
