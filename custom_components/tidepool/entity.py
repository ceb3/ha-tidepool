"""Base entity: every sensor of a patient belongs to that patient's device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import TidepoolCoordinator


class TidepoolEntity[C: TidepoolCoordinator](CoordinatorEntity[C]):
    """An entity of one patient, fed by one of that patient's coordinators."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: C,
        description: EntityDescription,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        user_id = coordinator.user_id
        self._attr_unique_id = f"{user_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, user_id)},
            name=device_name,
            manufacturer="Tidepool",
        )
