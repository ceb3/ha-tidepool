"""Glucose, summary, pump, insulin and carbs sensors of a patient."""

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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .coordinator import (
    FastData,
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolSlowCoordinator,
)
from .entity import TidepoolEntity
from .models import (
    BASAL_DELIVERY_TYPES,
    SUMMARY_PERIODS,
    Summary,
    basal_units,
    carbs_on_board,
    delivered_bolus,
    insulin_on_board,
    mmol_to_mgdl,
    net_carbs,
    newest_with,
    parse_time,
    window_sum,
)

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


INSULIN_UNITS = "U"
BASAL_RATE_UNITS = "U/h"
# Shown with the on-board values: they come from a past dosing decision.
AS_OF_NOTE = "As of the last dosing decision; not a live value."


@dataclass(frozen=True, kw_only=True)
class PumpSensorDescription(SensorEntityDescription):
    """A value from the latest records or the 24 h window.

    ``value_fn`` gets the fast data and the current time; ``attrs_fn`` gives
    the attributes that say where the value came from.
    """

    value_fn: Callable[[FastData, datetime], Any]
    attrs_fn: Callable[[FastData], dict[str, Any] | None] = lambda _: None


def _basal(data: FastData) -> dict | None:
    """The newest basal segment: what the pump is (or last was) delivering."""
    return data.latest.by_type.get("basal")


def _basal_rate(data: FastData, _now: datetime) -> float | None:
    rate = (_basal(data) or {}).get("rate")
    return float(rate) if isinstance(rate, int | float) else None


def _basal_mode(data: FastData, _now: datetime) -> str | None:
    delivery = (_basal(data) or {}).get("deliveryType")
    return delivery if delivery in BASAL_DELIVERY_TYPES else None


def _basal_attrs(data: FastData) -> dict[str, Any] | None:
    basal = _basal(data)
    if basal is None:
        return None
    return {"delivery": basal.get("deliveryType"), "time": _time_attr(basal)}


def _time_attr(record: dict) -> str:
    # Latest and window records always have a usable time; parsing checked it.
    return parse_time(record["time"]).isoformat()


def _last_bolus(data: FastData, _now: datetime) -> float | None:
    bolus = data.latest.by_type.get("bolus")
    return None if bolus is None else delivered_bolus(bolus)


def _last_bolus_attrs(data: FastData) -> dict[str, Any] | None:
    bolus = data.latest.by_type.get("bolus")
    if bolus is None:
        return None
    return {
        "time": _time_attr(bolus),
        "sub_type": bolus.get("subType"),
        "context": bolus.get("deliveryContext"),
    }


def _has_insulin(data: FastData) -> bool:
    """Whether the window holds pump records; without them totals are unknown."""
    return bool(data.window.get("basal") or data.window.get("bolus"))


def _basal_24h(data: FastData, now: datetime) -> float | None:
    if not _has_insulin(data):
        return None
    return round(basal_units(data.window.get("basal", []), now), 3)


def _bolus_24h(data: FastData, now: datetime) -> float | None:
    if not _has_insulin(data):
        return None
    return round(window_sum(data.window.get("bolus", []), delivered_bolus, now), 3)


def _total_24h(data: FastData, now: datetime) -> float | None:
    basal, bolus = _basal_24h(data, now), _bolus_24h(data, now)
    return None if basal is None or bolus is None else round(basal + bolus, 3)


def _last_carbs(data: FastData) -> tuple[float, datetime, dict] | None:
    food = [*data.window.get("food", []), *_latest(data, "food")]
    return newest_with(food, net_carbs)


def _carbs_last(data: FastData, _now: datetime) -> float | None:
    found = _last_carbs(data)
    return None if found is None else found[0]


def _carbs_last_attrs(data: FastData) -> dict[str, Any] | None:
    found = _last_carbs(data)
    return None if found is None else {"time": found[1].isoformat()}


def _carbs_24h(data: FastData, now: datetime) -> float | None:
    food = data.window.get("food")
    return round(window_sum(food, net_carbs, now), 1) if food else None


def _latest(data: FastData, record_type: str) -> list[dict]:
    record = data.latest.by_type.get(record_type)
    return [] if record is None else [record]


def _newest_time(record_type: str) -> Callable[[FastData, datetime], Any]:
    def value(data: FastData, _now: datetime) -> datetime | None:
        record = data.latest.by_type.get(record_type)
        return None if record is None else parse_time(record["time"])

    return value


def _injection_attrs(data: FastData) -> dict[str, Any] | None:
    dose = data.latest.by_type.get("insulin", {}).get("dose")
    total = dose.get("total") if isinstance(dose, dict) else None
    return {"dose": total} if isinstance(total, int | float) else None


def _on_board(
    value_fn: Callable[[dict], float | None],
) -> tuple[
    Callable[[FastData, datetime], Any], Callable[[FastData], dict[str, Any] | None]
]:
    """The value and attributes of an on-board amount of the newest decision."""

    def decision(data: FastData) -> tuple[float, datetime, dict] | None:
        records = [*_latest(data, "dosingDecision"), *_latest(data, "wizard")]
        return newest_with(records, value_fn)

    def value(data: FastData, _now: datetime) -> float | None:
        found = decision(data)
        return None if found is None else found[0]

    def attrs(data: FastData) -> dict[str, Any] | None:
        found = decision(data)
        if found is None:
            return None
        _, time, record = found
        return {"as_of": time.isoformat(), "source": record["type"], "note": AS_OF_NOTE}

    return value, attrs


_IOB_VALUE, _IOB_ATTRS = _on_board(insulin_on_board)
_COB_VALUE, _COB_ATTRS = _on_board(carbs_on_board)


def _insulin_24h(
    key: str, value_fn: Callable[[FastData, datetime], Any]
) -> PumpSensorDescription:
    # A rolling 24 h total rises and falls, so it is a measurement; a total
    # state class would make long-term statistics count it as consumption.
    return PumpSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=INSULIN_UNITS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


PUMP_SENSORS = (
    PumpSensorDescription(
        key="basal_rate",
        translation_key="basal_rate",
        native_unit_of_measurement=BASAL_RATE_UNITS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_basal_rate,
        attrs_fn=_basal_attrs,
    ),
    PumpSensorDescription(
        key="basal_mode",
        translation_key="basal_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(BASAL_DELIVERY_TYPES),
        value_fn=_basal_mode,
    ),
    # Single events carry no state class: their statistics would mean nothing.
    PumpSensorDescription(
        key="last_bolus",
        translation_key="last_bolus",
        native_unit_of_measurement=INSULIN_UNITS,
        suggested_display_precision=2,
        value_fn=_last_bolus,
        attrs_fn=_last_bolus_attrs,
    ),
    _insulin_24h("insulin_basal_24h", _basal_24h),
    _insulin_24h("insulin_bolus_24h", _bolus_24h),
    _insulin_24h("insulin_total_24h", _total_24h),
    PumpSensorDescription(
        key="carbs_last",
        translation_key="carbs_last",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        value_fn=_carbs_last,
        attrs_fn=_carbs_last_attrs,
    ),
    PumpSensorDescription(
        key="carbs_24h",
        translation_key="carbs_24h",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_carbs_24h,
    ),
    PumpSensorDescription(
        key="last_injection",
        translation_key="last_injection",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_newest_time("insulin"),
        attrs_fn=_injection_attrs,
    ),
    PumpSensorDescription(
        key="last_activity",
        translation_key="last_activity",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_newest_time("physicalActivity"),
    ),
    # Recorded by the pump or app at its last dosing decision; never live.
    PumpSensorDescription(
        key="iob_at_last_decision",
        translation_key="iob_at_last_decision",
        native_unit_of_measurement=INSULIN_UNITS,
        suggested_display_precision=2,
        value_fn=_IOB_VALUE,
        attrs_fn=_IOB_ATTRS,
    ),
    PumpSensorDescription(
        key="cob_at_last_decision",
        translation_key="cob_at_last_decision",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        value_fn=_COB_VALUE,
        attrs_fn=_COB_ATTRS,
    ),
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
        entities.extend(PumpSensor(coordinator, d, name) for d in PUMP_SENSORS)
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


class PumpSensor(TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
    """A pump, insulin or carbs value of the latest records or the 24 h window."""

    entity_description: PumpSensorDescription

    @property
    @override
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, dt_util.utcnow())

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator.data)
