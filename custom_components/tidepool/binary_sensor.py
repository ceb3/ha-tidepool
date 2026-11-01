"""Pump delivery and data source connection binary sensors of a patient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, override

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import (
    SlowData,
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolSlowCoordinator,
)
from .devices import DeviceRole
from .entity import TidepoolEntity
from .models import basal_period, current_basal

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DeliveryBinarySensorDescription(BinarySensorEntityDescription):
    """Whether the newest basal segment has a given delivery type."""

    delivery: str


DELIVERY_SENSORS = (
    DeliveryBinarySensorDescription(
        key="automated_delivery_active",
        translation_key="automated_delivery_active",
        delivery="automated",
    ),
    DeliveryBinarySensorDescription(
        key="delivery_suspended",
        translation_key="delivery_suspended",
        delivery="suspend",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TidepoolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the delivery and data source binary sensors of every selected patient."""
    runtime = entry.runtime_data
    async_add_entities(
        DeliveryBinarySensor(runtime.fast[user_id], description, name)
        for user_id, name in runtime.names.items()
        for description in DELIVERY_SENSORS
    )
    for user_id, name in runtime.names.items():
        _track_sources(entry, runtime.slow[user_id], name, async_add_entities)


# Connection state of each data source state; any other state is unknown.
SOURCE_CONNECTED = {"connected": True, "error": False}


def _sources(data: SlowData) -> dict[str, dict]:
    """The data sources of a patient by id, skipping ones without an id."""
    return {
        source["id"]: source
        for source in data.data_sources
        if isinstance(source, dict)
        and isinstance(source.get("id"), str)
        and source["id"]
    }


def _track_sources(
    entry: TidepoolConfigEntry,
    coordinator: TidepoolSlowCoordinator,
    name: str,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a sensor per data source now and for each one a refresh brings."""
    known: set[str] = set()

    @callback
    def add_new_sources() -> None:
        new = {k: v for k, v in _sources(coordinator.data).items() if k not in known}
        known.update(new)
        async_add_entities(
            DataSourceBinarySensor(coordinator, _source_description(k, v), name)
            for k, v in new.items()
        )

    add_new_sources()
    entry.async_on_unload(coordinator.async_add_listener(add_new_sources))


def _source_description(source_id: str, source: dict) -> BinarySensorEntityDescription:
    provider = source.get("providerName")
    return BinarySensorEntityDescription(
        key=f"source_{source_id}",
        translation_key="data_source",
        translation_placeholders={
            "provider": provider if isinstance(provider, str) else "Data source"
        },
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
    )


class DeliveryBinarySensor(TidepoolEntity[TidepoolFastCoordinator], BinarySensorEntity):
    """On while the newest basal segment has the description's delivery type.

    Unknown once that segment ended more than ``BASAL_STALE_GRACE`` ago.
    """

    _device_role = DeviceRole.PUMP
    entity_description: DeliveryBinarySensorDescription

    @property
    @override
    def is_on(self) -> bool | None:
        latest = self.coordinator.data.latest
        delivery = (current_basal(latest, dt_util.utcnow()) or {}).get("deliveryType")
        if not isinstance(delivery, str):
            return None
        return delivery == self.entity_description.delivery

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return basal_period(self.coordinator.data.latest)


class DataSourceBinarySensor(
    TidepoolEntity[TidepoolSlowCoordinator], BinarySensorEntity
):
    """Whether a data source (e.g. a cloud account Tidepool imports from) works.

    The unique id is ``{patient}_source_{data source id}``.
    """

    @property
    def _source(self) -> dict | None:
        source_id = self.entity_description.key.removeprefix("source_")
        return _sources(self.coordinator.data).get(source_id)

    @property
    @override
    def is_on(self) -> bool | None:
        state = (self._source or {}).get("state")
        return SOURCE_CONNECTED.get(state) if isinstance(state, str) else None

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        source = self._source
        if source is None:
            return None
        return {
            "state": source.get("state"),
            "last_import": source.get("lastImportTime"),
            "latest_data": source.get("latestDataTime"),
        }
