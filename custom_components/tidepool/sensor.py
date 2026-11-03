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
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfMass,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .coordinator import (
    FastData,
    SlowData,
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolSlowCoordinator,
)
from .devices import DeviceRole, last_upload
from .entity import TidepoolEntity
from .models import (
    BASAL_DELIVERY_TYPES,
    MGDL,
    MMOL,
    SUMMARY_PERIODS,
    Summary,
    active_entry,
    alarm_type,
    basal_period,
    basal_units,
    carbs_on_board,
    convert_glucose,
    current_basal,
    decision_is_current,
    delivered_bolus,
    dig,
    glucose_unit,
    insulin_on_board,
    is_number,
    mmol_to_mgdl,
    net_carbs,
    newest_with,
    parse_time,
    pump_zone,
    reported_states,
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
class FastSensorDescription(SensorEntityDescription):
    """A value from the latest records or the 24 h window.

    ``value_fn`` gets the fast data and the current time; ``attrs_fn`` gives
    the attributes that say where the value came from.
    """

    value_fn: Callable[[FastData, datetime], Any]
    attrs_fn: Callable[[FastData], dict[str, Any] | None] = lambda _: None


def _basal_rate(data: FastData, now: datetime) -> float | None:
    rate = (current_basal(data.latest, now) or {}).get("rate")
    return float(rate) if is_number(rate) else None


def _basal_mode(data: FastData, now: datetime) -> str | None:
    delivery = (current_basal(data.latest, now) or {}).get("deliveryType")
    return delivery if delivery in BASAL_DELIVERY_TYPES else None


def _basal_rate_attrs(data: FastData) -> dict[str, Any] | None:
    # Shown even when the segment is stale, so the unknown state is explained.
    period = basal_period(data.latest)
    if period is None:
        return None
    return {"delivery": data.latest.by_type["basal"].get("deliveryType"), **period}


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
    return {"dose": total} if is_number(total) else None


def _on_board(
    value_fn: Callable[[dict], float | None],
) -> tuple[
    Callable[[FastData, datetime], Any], Callable[[FastData], dict[str, Any] | None]
]:
    """The value and attributes of an on-board amount of the newest decision."""

    def decision(data: FastData) -> tuple[float, datetime, dict] | None:
        records = [*_latest(data, "dosingDecision"), *_latest(data, "wizard")]
        return newest_with(records, value_fn)

    def value(data: FastData, now: datetime) -> float | None:
        # Unknown once the decision is stale; the attributes still say when.
        found = decision(data)
        if found is None or not decision_is_current(found[1], now):
            return None
        return found[0]

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
) -> FastSensorDescription:
    # A rolling 24 h total rises and falls, so it is a measurement; a total
    # state class would make long-term statistics count it as consumption.
    return FastSensorDescription(
        key=key,
        translation_key=key,
        native_unit_of_measurement=INSULIN_UNITS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


PUMP_SENSORS = (
    FastSensorDescription(
        key="basal_rate",
        translation_key="basal_rate",
        native_unit_of_measurement=BASAL_RATE_UNITS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_basal_rate,
        attrs_fn=_basal_rate_attrs,
    ),
    FastSensorDescription(
        key="basal_mode",
        translation_key="basal_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(BASAL_DELIVERY_TYPES),
        value_fn=_basal_mode,
        attrs_fn=lambda data: basal_period(data.latest),
    ),
    # Single events carry no state class: their statistics would mean nothing.
    FastSensorDescription(
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
    FastSensorDescription(
        key="carbs_last",
        translation_key="carbs_last",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        value_fn=_carbs_last,
        attrs_fn=_carbs_last_attrs,
    ),
    FastSensorDescription(
        key="carbs_24h",
        translation_key="carbs_24h",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_carbs_24h,
    ),
    FastSensorDescription(
        key="last_injection",
        translation_key="last_injection",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_newest_time("insulin"),
        attrs_fn=_injection_attrs,
    ),
    FastSensorDescription(
        key="last_activity",
        translation_key="last_activity",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=_newest_time("physicalActivity"),
    ),
    # Recorded by the pump or app at its last dosing decision; never live.
    FastSensorDescription(
        key="iob_at_last_decision",
        translation_key="iob_at_last_decision",
        native_unit_of_measurement=INSULIN_UNITS,
        suggested_display_precision=2,
        value_fn=_IOB_VALUE,
        attrs_fn=_IOB_ATTRS,
    ),
    FastSensorDescription(
        key="cob_at_last_decision",
        translation_key="cob_at_last_decision",
        native_unit_of_measurement=UnitOfMass.GRAMS,
        suggested_display_precision=0,
        value_fn=_COB_VALUE,
        attrs_fn=_COB_ATTRS,
    ),
)

# The record types with a "last data" time, by key suffix. Only the main ones
# start enabled; the rest are there for a user who wants to watch them.
LAST_DATA_TYPES = {
    "cbg": "cbg",
    "basal": "basal",
    "bolus": "bolus",
    "smbg": "smbg",
    "food": "food",
    "insulin": "insulin",
    "physical_activity": "physicalActivity",
    "device_event": "deviceEvent",
    "pump_settings": "pumpSettings",
    "cgm_settings": "cgmSettings",
    "wizard": "wizard",
    "upload": "upload",
}
LAST_DATA_ENABLED = ("cbg", "basal", "bolus")

LAST_DATA_SENSORS = tuple(
    FastSensorDescription(
        key=f"last_data_{suffix}",
        translation_key=f"last_data_{suffix}",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=suffix in LAST_DATA_ENABLED,
        value_fn=_newest_time(record_type),
    )
    for suffix, record_type in LAST_DATA_TYPES.items()
)

LAST_UPLOAD = SensorEntityDescription(
    key="last_upload",
    translation_key="last_upload",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
)


@dataclass(frozen=True, kw_only=True)
class DeviceEventSensorDescription(SensorEntityDescription):
    """When the newest device event of a kind happened.

    ``event`` is its key in ``SlowData.device_events``; ``attrs_fn`` gets
    that record and the slow data.
    """

    event: str
    attrs_fn: Callable[[dict, SlowData], dict[str, Any] | None] = lambda *_: None


def _tubing_attrs(_record: dict, data: SlowData) -> dict[str, Any] | None:
    tubing = data.device_events.get("prime_tubing")
    return None if tubing is None else {"tubing": _time_attr(tubing)}


# Delivery statuses and their reasons as the Tidepool data spec lists them.
DELIVERY_STATUSES = ("suspended", "resumed")
STATUS_REASONS = ("automatic", "manual")


def _status_attrs(record: dict, _data: SlowData) -> dict[str, Any]:
    status = record.get("status")
    if status not in DELIVERY_STATUSES:
        return {"status": None, "reason": None}
    reason = record.get("reason")
    reason = reason.get(status) if isinstance(reason, dict) else None
    return {"status": status, "reason": reason if reason in STATUS_REASONS else None}


def _device_event_sensor(
    key: str,
    event: str,
    attrs_fn: Callable[[dict, SlowData], dict[str, Any] | None] = lambda *_: None,
    *,
    enabled: bool = True,
) -> DeviceEventSensorDescription:
    return DeviceEventSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=enabled,
        event=event,
        attrs_fn=attrs_fn,
    )


DEVICE_EVENT_SENSORS = (
    _device_event_sensor(
        "last_alarm", "alarm", lambda record, _: {"alarm_type": alarm_type(record)}
    ),
    _device_event_sensor("last_reservoir_change", "reservoirChange"),
    # A cannula prime is the site change; a tubing prime alone is not.
    _device_event_sensor("last_site_change", "prime_cannula", _tubing_attrs),
    _device_event_sensor("last_tubing_prime", "prime_tubing", enabled=False),
    _device_event_sensor("last_status_change", "status", _status_attrs),
)


# Pump and CGM settings, the bolus calculator's last entry and the optional
# records. All start disabled, and a missing record leaves them unknown. A
# setting is shown as the device last uploaded it: nothing is computed from
# one beyond picking the schedule entry in force and converting its unit.
SETTING_NOTE = "Pump setting as last uploaded; not advice."
MINUTE_MS = 60_000
# Water amounts keep the unit they were recorded in.
WATER_UNITS = {
    "gallons": UnitOfVolume.GALLONS,
    "ounces": UnitOfVolume.FLUID_OUNCES,
    "liters": UnitOfVolume.LITERS,
    "milliliters": UnitOfVolume.MILLILITERS,
}


@dataclass(frozen=True)
class RecordContext:
    """What a record sensor's functions get besides the record itself."""

    # Now on the clock of the pump the record came from.
    local_time: datetime
    # The glucose unit of Home Assistant's unit system.
    unit: str
    # How the pump's clock was found (``models.pump_zone``).
    zone_basis: str

    def glucose(self, record: dict, value: Any) -> float | None:
        """A glucose value of the record, in Home Assistant's unit."""
        source = glucose_unit(record)
        if source is None or not is_number(value):
            return None
        return convert_glucose(value, source, self.unit)


@dataclass(frozen=True, kw_only=True)
class RecordSensorDescription(SensorEntityDescription):
    """A value of the latest record of one type.

    ``value_fn`` and ``attrs_fn`` get the record and its context; a sensor
    with ``unit_fn`` takes its unit from them too. ``role`` is the device the
    sensor belongs to, the patient's own when there is none.
    """

    record: str
    value_fn: Callable[[dict, RecordContext], Any]
    attrs_fn: Callable[[dict, RecordContext], dict[str, Any]] = lambda *_: {}
    unit_fn: Callable[[dict, RecordContext], str | None] | None = None
    role: DeviceRole | None = None


def _text_at(*keys: str) -> Callable[[dict, RecordContext], str | None]:
    def value(record: dict, _context: RecordContext) -> str | None:
        found = dig(record, *keys)
        return found if isinstance(found, str) else None

    return value


def _number_at(*keys: str) -> Callable[[dict, RecordContext], float | None]:
    def value(record: dict, _context: RecordContext) -> float | None:
        found = dig(record, *keys)
        return found if is_number(found) else None

    return value


def _schedule_value(
    key: str, field: str, *, sole: bool = True, glucose: bool = False
) -> Callable[[dict, RecordContext], float | None]:
    """The ``field`` of the entry now in force of the schedules ``key``."""

    def value(record: dict, context: RecordContext) -> float | None:
        entry = active_entry(record, key, context.local_time, sole=sole)
        amount = None if entry is None else entry.get(field)
        if glucose:
            return context.glucose(record, amount)
        return amount if is_number(amount) else None

    return value


def _setting_attrs(record: dict, _context: RecordContext) -> dict[str, Any]:
    return {"as_of": _time_attr(record), "note": SETTING_NOTE}


def _zone_attrs(record: dict, context: RecordContext) -> dict[str, Any]:
    """How the pump's clock a schedule is read on was found."""
    return {"zone_basis": context.zone_basis}


def _schedule_setting_attrs(record: dict, context: RecordContext) -> dict[str, Any]:
    return _setting_attrs(record, context) | _zone_attrs(record, context)


def _alert_value(key: str) -> Callable[[dict, RecordContext], float | None]:
    return lambda record, context: context.glucose(record, dig(record, key, "level"))


def _alert_attrs(key: str) -> Callable[[dict, RecordContext], dict[str, Any]]:
    def attrs(record: dict, _context: RecordContext) -> dict[str, Any]:
        enabled, snooze = dig(record, key, "enabled"), dig(record, key, "snooze")
        return {
            "enabled": enabled if isinstance(enabled, bool) else None,
            "snooze_minutes": snooze // MINUTE_MS if is_number(snooze) else None,
        }

    return attrs


def _record_time(record: dict, _context: RecordContext) -> datetime:
    return parse_time(record["time"])


def _wizard_attrs(record: dict, context: RecordContext) -> dict[str, Any]:
    # Only what was entered and the pump's own on-board figure; the rest of the
    # record, the calculator's suggestion above all, is deliberately not read.
    carbs = record.get("carbInput")
    return {
        "carbs_entered": carbs if is_number(carbs) else None,
        "glucose_entered": context.glucose(record, record.get("bgInput")),
        "insulin_on_board": insulin_on_board(record),
        "time": _time_attr(record),
        "as_of": _time_attr(record),
        "note": AS_OF_NOTE,
    }


def _water_unit(record: dict, _context: RecordContext) -> str | None:
    return WATER_UNITS.get(dig(record, "amount", "units"))


def _water_amount(record: dict, context: RecordContext) -> float | None:
    if _water_unit(record, context) is None:
        return None
    return _number_at("amount", "value")(record, context)


def _setting(
    key: str,
    record: str,
    value_fn: Callable[[dict, RecordContext], Any],
    *,
    role: DeviceRole = DeviceRole.PUMP,
    **fields: Any,
) -> RecordSensorDescription:
    return RecordSensorDescription(
        key=key,
        translation_key=key,
        record=record,
        value_fn=value_fn,
        role=role,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        **fields,
    )


def _glucose_setting(
    key: str,
    record: str,
    value_fn: Callable[[dict, RecordContext], Any],
    *,
    per_insulin: bool = False,
    **fields: Any,
) -> RecordSensorDescription:
    # A glucose per unit of insulin is no concentration: no device class.
    suffix = "/U" if per_insulin else ""
    return _setting(
        key,
        record,
        value_fn,
        unit_fn=lambda _, context: f"{context.unit}{suffix}",
        device_class=(
            None if per_insulin else SensorDeviceClass.BLOOD_GLUCOSE_CONCENTRATION
        ),
        **fields,
    )


RECORD_SENSORS = (
    _setting("active_schedule", "pumpSettings", _text_at("activeSchedule")),
    _setting(
        "scheduled_basal_now",
        "pumpSettings",
        _schedule_value("basalSchedules", "rate", sole=False),
        native_unit_of_measurement=BASAL_RATE_UNITS,
        attrs_fn=_zone_attrs,
    ),
    _setting(
        "carb_ratio_now",
        "pumpSettings",
        _schedule_value("carbRatios", "amount"),
        native_unit_of_measurement="g/U",
        attrs_fn=_schedule_setting_attrs,
    ),
    _glucose_setting(
        "sensitivity_now",
        "pumpSettings",
        _schedule_value("insulinSensitivities", "amount", glucose=True),
        per_insulin=True,
        attrs_fn=_schedule_setting_attrs,
    ),
    _glucose_setting(
        "target_low_now",
        "pumpSettings",
        _schedule_value("bgTargets", "low", glucose=True),
        attrs_fn=_schedule_setting_attrs,
    ),
    _glucose_setting(
        "target_high_now",
        "pumpSettings",
        _schedule_value("bgTargets", "high", glucose=True),
        attrs_fn=_schedule_setting_attrs,
    ),
    _setting(
        "max_basal",
        "pumpSettings",
        _number_at("basal", "rateMaximum", "value"),
        native_unit_of_measurement=BASAL_RATE_UNITS,
    ),
    _setting(
        "max_bolus",
        "pumpSettings",
        _number_at("bolus", "amountMaximum", "value"),
        native_unit_of_measurement=INSULIN_UNITS,
    ),
    _setting("insulin_model", "pumpSettings", _text_at("insulinModel", "modelType")),
    *(
        _glucose_setting(
            key,
            "cgmSettings",
            _alert_value(alerts),
            role=DeviceRole.CGM,
            attrs_fn=_alert_attrs(alerts),
        )
        for key, alerts in (("alert_high", "highAlerts"), ("alert_low", "lowAlerts"))
    ),
    # The records below are data rather than settings: no diagnostic category.
    RecordSensorDescription(
        key="last_wizard_entry",
        translation_key="last_wizard_entry",
        record="wizard",
        role=DeviceRole.PUMP,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=_record_time,
        attrs_fn=_wizard_attrs,
    ),
    RecordSensorDescription(
        key="reported_state",
        translation_key="reported_state",
        record="reportedState",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_registry_enabled_default=False,
        value_fn=_record_time,
        attrs_fn=lambda record, _: {"states": reported_states(record)},
    ),
    RecordSensorDescription(
        key="water_intake",
        translation_key="water_intake",
        record="water",
        entity_registry_enabled_default=False,
        value_fn=_water_amount,
        unit_fn=_water_unit,
        attrs_fn=lambda record, _: {"time": _time_attr(record)},
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
        entities.extend(RecordSensor(coordinator, d, name) for d in RECORD_SENSORS)
        entities.extend(FastSensor(coordinator, d, name) for d in LAST_DATA_SENSORS)
        slow = runtime.slow[user_id]
        entities.extend(SummarySensor(slow, d, name) for d in SUMMARY_SENSORS)
        entities.append(LastUploadSensor(slow, LAST_UPLOAD, name))
        entities.extend(DeviceEventSensor(slow, d, name) for d in DEVICE_EVENT_SENSORS)
    async_add_entities(entities)


class GlucoseSensor(TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
    """The newest continuous glucose reading, in the Home Assistant unit system."""

    _device_role = DeviceRole.CGM

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


class _MinuteTick(Entity):
    """An entity whose value moves with the clock, so it is written each minute.

    The tick runs a minute after the entity was added, not on the minute a
    value is due, so the shown value can trail the clock by up to a minute.
    """

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_tick, AGE_INTERVAL)
        )

    @callback
    def _async_tick(self, _now: datetime) -> None:
        self.async_write_ha_state()


class GlucoseAgeSensor(
    _MinuteTick, TidepoolEntity[TidepoolFastCoordinator], SensorEntity
):
    """Whole minutes since the newest glucose reading.

    The age grows even when a refresh brings nothing new.
    """

    _device_role = DeviceRole.CGM

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


class FastSensor(TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
    """A value of the latest records or the 24 h window."""

    entity_description: FastSensorDescription

    @property
    @override
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data, dt_util.utcnow())

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator.data)


class PumpSensor(FastSensor):
    """A pump, insulin or carbs value; on the pump device once there is one."""

    _device_role = DeviceRole.PUMP


class RecordSensor(_MinuteTick, TidepoolEntity[TidepoolFastCoordinator], SensorEntity):
    """A setting or other value of the latest record of one type.

    The settings that follow a schedule move with the pump's clock, so these
    are written every minute and not only when the data is refreshed.
    """

    entity_description: RecordSensorDescription

    def __init__(
        self,
        coordinator: TidepoolFastCoordinator,
        description: RecordSensorDescription,
        device_name: str,
    ) -> None:
        self._device_role = description.role
        super().__init__(coordinator, description, device_name)

    @property
    def _record(self) -> dict | None:
        return self.coordinator.data.latest.by_type.get(self.entity_description.record)

    def _record_context(self, record: dict) -> RecordContext:
        us_units = self.hass.config.units is US_CUSTOMARY_SYSTEM
        zone, basis = pump_zone(record, dt_util.get_default_time_zone())
        return RecordContext(
            dt_util.utcnow().astimezone(zone), MGDL if us_units else MMOL, basis
        )

    @property
    @override
    def native_unit_of_measurement(self) -> str | None:
        description = self.entity_description
        if description.unit_fn is None:
            return description.native_unit_of_measurement
        record = self._record or {}
        return description.unit_fn(record, self._record_context(record))

    @property
    @override
    def native_value(self) -> Any:
        record = self._record
        if record is None:
            return None
        return self.entity_description.value_fn(record, self._record_context(record))

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._record
        if record is None:
            return None
        return self.entity_description.attrs_fn(record, self._record_context(record))


class LastUploadSensor(TidepoolEntity[TidepoolSlowCoordinator], SensorEntity):
    """When data last arrived: the newest data set or data source import."""

    @property
    @override
    def native_value(self) -> datetime | None:
        data = self.coordinator.data
        return last_upload(data.data_sets, data.data_sources)


class DeviceEventSensor(TidepoolEntity[TidepoolSlowCoordinator], SensorEntity):
    """When the newest alarm, reservoir change, prime or status change happened."""

    _device_role = DeviceRole.PUMP
    entity_description: DeviceEventSensorDescription

    @property
    def _record(self) -> dict | None:
        return self.coordinator.data.device_events.get(self.entity_description.event)

    @property
    @override
    def native_value(self) -> datetime | None:
        record = self._record
        return None if record is None else parse_time(record["time"])

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        record = self._record
        if record is None:
            return None
        return self.entity_description.attrs_fn(record, self.coordinator.data)
