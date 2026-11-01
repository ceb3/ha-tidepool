"""Base entity: a sensor of a patient, on the patient's device or a child device."""

from __future__ import annotations

from typing import override

from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import TidepoolCoordinator
from .devices import DeviceRole, child_identifier, patient_device_info, role_device


class TidepoolEntity[C: TidepoolCoordinator](CoordinatorEntity[C]):
    """An entity of one patient, fed by one of that patient's coordinators.

    An entity with a ``_device_role`` belongs to the patient's pump or CGM once
    the data shows one, and to the patient's device until then. When a refresh
    shows a different device the registry entry moves to it in place, keeping
    its unique id and entity id; the devices themselves are registered by
    ``devices.async_setup_devices`` before entities are added.
    """

    _attr_has_entity_name = True
    _device_role: DeviceRole | None = None

    def __init__(
        self,
        coordinator: C,
        description: EntityDescription,
        device_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.user_id}_{description.key}"
        self._device_name = device_name
        self._attr_device_info = self._resolve_device_info()

    def _resolve_device_info(self) -> DeviceInfo:
        user_id = self.coordinator.user_id
        if self._device_role is not None:
            runtime = self.coordinator.config_entry.runtime_data
            device_id = role_device(
                runtime.fast[user_id].data.latest,
                runtime.slow[user_id].data.data_sets,
                self._device_role,
            )
            if device_id is not None:
                return DeviceInfo(identifiers={child_identifier(user_id, device_id)})
        return patient_device_info(user_id, self._device_name)

    @callback
    @override
    def _handle_coordinator_update(self) -> None:
        if self._device_role is not None:
            self._async_follow_device()
        super()._handle_coordinator_update()

    @callback
    def _async_follow_device(self) -> None:
        """Move the registry entry to the device the data now points at."""
        info = self._resolve_device_info()
        (identifier,) = info["identifiers"]
        if self.device_info and {identifier} == self.device_info.get("identifiers"):
            return
        device = dr.async_get(self.hass).async_get_device_by_identifier(
            identifier, self.coordinator.config_entry.entry_id
        )
        if device is None or self.registry_entry is None:
            return
        self._attr_device_info = info
        er.async_get(self.hass).async_update_entity(self.entity_id, device_id=device.id)
