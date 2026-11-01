"""Pump delivery binary sensors of a patient."""

from __future__ import annotations

from dataclasses import dataclass
from typing import override

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import TidepoolConfigEntry, TidepoolFastCoordinator
from .entity import TidepoolEntity

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
    """Add the delivery binary sensors of every selected patient."""
    runtime = entry.runtime_data
    async_add_entities(
        DeliveryBinarySensor(runtime.fast[user_id], description, name)
        for user_id, name in runtime.names.items()
        for description in DELIVERY_SENSORS
    )


class DeliveryBinarySensor(TidepoolEntity[TidepoolFastCoordinator], BinarySensorEntity):
    """On while the newest basal segment has the description's delivery type."""

    entity_description: DeliveryBinarySensorDescription

    @property
    @override
    def is_on(self) -> bool | None:
        basal = self.coordinator.data.latest.by_type.get("basal") or {}
        delivery = basal.get("deliveryType")
        if not isinstance(delivery, str):
            return None
        return delivery == self.entity_description.delivery
