"""Glucose sensors of a patient."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, override

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .coordinator import (
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolSlowCoordinator,
)
from .entity import TidepoolEntity
from .models import SUMMARY_PERIODS, Summary, mmol_to_mgdl

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


@dataclass(frozen=True, kw_only=True)
class SummarySensorDescription(SensorEntityDescription):
    """A summary statistic of one period."""

    kind: str
    period: str
    value_fn: Callable[[Summary], float | None]
    # Glucose statistics follow the Home Assistant unit system; value_fn then
    # returns mmol/L.
    glucose: bool = False


@dataclass(frozen=True)
class _Metric:
    """A statistic of a summary, offered for every period."""

    name: str
    value_fn: Callable[[Summary], float | None]
    unit: str | None = None
    glucose: bool = False
    device_class: SensorDeviceClass | None = None
    category: EntityCategory | None = None
    precision: int = 1
    # Periods whose sensor is enabled when the entry is set up; the rest start
    # disabled and a user switches on the ones they want.
    enabled_periods: tuple[str, ...] = ()


# Shown in entity names, e.g. "Time in range (24 h)".
PERIOD_LABELS = {"1d": "24 h", "7d": "7 d", "14d": "14 d", "30d": "30 d"}

SUMMARY_METRICS = (
    _Metric(
        "time_in_range",
        lambda s: s.time_in_range,
        PERCENTAGE,
        enabled_periods=("1d", "14d"),
    ),
    _Metric("low", lambda s: s.low, PERCENTAGE, enabled_periods=("1d",)),
    _Metric("very_low", lambda s: s.very_low, PERCENTAGE),
    _Metric("high", lambda s: s.high, PERCENTAGE, enabled_periods=("1d",)),
    _Metric("very_high", lambda s: s.very_high, PERCENTAGE),
    _Metric("extreme_high", lambda s: s.extreme_high, PERCENTAGE),
    _Metric(
        "average",
        lambda s: s.average_mmol,
        glucose=True,
        device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
        enabled_periods=("14d",),
    ),
    _Metric("gmi", lambda s: s.gmi, PERCENTAGE, enabled_periods=("14d",)),
    _Metric("cv", lambda s: s.cv, PERCENTAGE, enabled_periods=("14d",)),
    _Metric("sd", lambda s: s.sd_mmol, glucose=True),
    _Metric(
        "min",
        lambda s: s.min_mmol,
        glucose=True,
        device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
    ),
    _Metric(
        "max",
        lambda s: s.max_mmol,
        glucose=True,
        device_class=SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION,
    ),
    _Metric(
        "hours_with_data",
        lambda s: s.hours_with_data,
        UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        category=EntityCategory.DIAGNOSTIC,
        precision=0,
    ),
    _Metric(
        "days_with_data",
        lambda s: s.days_with_data,
        UnitOfTime.DAYS,
        device_class=SensorDeviceClass.DURATION,
        category=EntityCategory.DIAGNOSTIC,
        precision=0,
    ),
)

# The unique id of each sensor ends in its key: summary_<kind>_<period>_<metric>.
SUMMARY_SENSORS = tuple(
    SummarySensorDescription(
        key=f"summary_cgm_{period}_{metric.name}",
        translation_key=f"summary_{metric.name}",
        translation_placeholders={"period": PERIOD_LABELS[period]},
        kind="cgm",
        period=period,
        value_fn=metric.value_fn,
        glucose=metric.glucose,
        device_class=metric.device_class,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=metric.unit,
        entity_category=metric.category,
        suggested_display_precision=metric.precision,
        entity_registry_enabled_default=period in metric.enabled_periods,
    )
    for period in SUMMARY_PERIODS
    for metric in SUMMARY_METRICS
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
        slow = runtime.slow[user_id]
        entities.extend(SummarySensor(slow, d, name) for d in SUMMARY_SENSORS)
    async_add_entities(entities)


class GlucoseSensor(TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
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


class GlucoseAgeSensor(TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
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


class SummarySensor(TidepoolEntity[TidepoolSlowCoordinator], SensorEntity):
    """One statistic of one period of the continuous glucose summary."""

    entity_description: SummarySensorDescription

    @property
    def _us_units(self) -> bool:
        return self.hass.config.units is US_CUSTOMARY_SYSTEM

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        if not self.entity_description.glucose:
            return self.entity_description.native_unit_of_measurement
        return "mg/dL" if self._us_units else SOURCE_UNITS

    @property
    @override
    def native_value(self) -> float | None:
        description = self.entity_description
        summary = self.coordinator.data.summary(description.kind, description.period)
        if summary is None:
            return None
        value = description.value_fn(summary)
        if value is not None and description.glucose and self._us_units:
            return mmol_to_mgdl(value)
        return value
