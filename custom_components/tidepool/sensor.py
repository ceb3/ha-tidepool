"""Glucose sensors of a patient."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .coordinator import TidepoolConfigEntry
from .entity import TidepoolEntity
from .models import mmol_to_mgdl

# Tidepool reports glucose in mmol/L whatever the account's display setting.
SOURCE_UNITS = "mmol/L"
# How often the age of the newest reading is re-evaluated without new data.
AGE_INTERVAL = timedelta(minutes=1)

PARALLEL_UPDATES = 0

GLUCOSE = SensorEntityDescription(
    key="glucose",
    translation_key="glucose",
    device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
    state_class=SensorStateClass.MEASUREMENT,
)
GLUCOSE_AGE = SensorEntityDescription(
    key="glucose_age",
    translation_key="glucose_age",
    device_class=SensorDeviceClass.DURATION,
    native_unit_of_measurement=UnitOfTime.MINUTES,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TidepoolConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of every selected patient."""
    runtime = entry.runtime_data
    entities: list[SensorEntity] = []
    for user_id, name in runtime.names.items():
        coordinator = runtime.fast[user_id]
        entities.append(GlucoseSensor(coordinator, GLUCOSE, name))
        entities.append(GlucoseAgeSensor(coordinator, GLUCOSE_AGE, name))
    async_add_entities(entities)


class GlucoseSensor(TidepoolEntity, SensorEntity):
    """The newest continuous glucose reading, in the Home Assistant unit system."""

    @property
    def _us_units(self) -> bool:
        return self.hass.config.units is US_CUSTOMARY_SYSTEM

    # The unit follows Home Assistant's unit system, not a per-entity choice.
    @property
    @override
    def native_unit_of_measurement(self) -> str:
        return "mg/dL" if self._us_units else SOURCE_UNITS

    @property
    @override
    def native_value(self) -> float | None:
        cbg = self.coordinator.data.latest.cbg
        if cbg is None:
            return None
        return mmol_to_mgdl(cbg.value_mmol) if self._us_units else cbg.value_mmol

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        cbg = self.coordinator.data.latest.cbg
        if cbg is None:
            return None
        return {"reading_time": cbg.time.isoformat(), "source_units": SOURCE_UNITS}


class GlucoseAgeSensor(TidepoolEntity, SensorEntity):
    """Whole minutes since the newest glucose reading."""

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # The age grows even when a refresh brings nothing new. The tick runs a
        # minute after the entity was added, not on the minute the reading is
        # due, so the shown age can trail the true age by up to a minute.
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, AGE_INTERVAL)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_write_ha_state()

    @property
    @override
    def native_value(self) -> int | None:
        cbg = self.coordinator.data.latest.cbg
        if cbg is None:
            return None
        age = dt_util.utcnow() - cbg.time
        return max(0, int(age.total_seconds() // 60))
