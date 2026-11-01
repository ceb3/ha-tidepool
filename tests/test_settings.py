"""Tests for the settings entities and the optional record entities.

All records are synthetic. Every entity here is disabled when first added, so
the entity tests enable them through the registry, as a user would. The pump
in the entity tests keeps UTC+10 (``timezoneOffset`` 600), which differs from
UTC and from Home Assistant's own time zone, so a schedule read in the wrong
zone gives a different answer; at the frozen ``NOW`` it is 22:00 there.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import (
    METRIC_SYSTEM,
    US_CUSTOMARY_SYSTEM,
    UnitSystem,
)
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import DOMAIN
from custom_components.tidepool.models import (
    convert_glucose,
    pump_local_time,
    pump_zone,
    value_at,
)
from custom_components.tidepool.sensor import AS_OF_NOTE, SETTING_NOTE

from .entries import (
    USER_ID,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
HOUR_MS = 3_600_000
PUMP_OFFSET = 600  # minutes: UTC+10

PUMP_DEVICE = "fake-pump-device-0001"
CGM_DEVICE = "fake-cgm-device-0001"
# On the synthetic records; they must never reach a state, attribute or name.
SERIAL = "FAKE-SERIAL-0001"
TRANSMITTER = "FAKE-TRANSMITTER-0001"

PUMP_KEYS = (
    "active_schedule",
    "scheduled_basal_now",
    "carb_ratio_now",
    "sensitivity_now",
    "target_low_now",
    "target_high_now",
    "max_basal",
    "max_bolus",
    "insulin_model",
)
CGM_KEYS = ("alert_high", "alert_low")
RECORD_KEYS = ("last_wizard_entry", "reported_state", "water_intake")
ALL_KEYS = PUMP_KEYS + CGM_KEYS + RECORD_KEYS
# Pump settings shown for information only.
INFORMATIONAL_KEYS = ("carb_ratio_now", "sensitivity_now", "target_low_now")
# What Home Assistant adds to the attributes of the timestamp sensors.
HA_ATTRIBUTES = {"friendly_name", "device_class"}


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def _entry(start_hours: float, **fields: Any) -> dict[str, Any]:
    return {"start": int(start_hours * HOUR_MS), **fields}


def _pump_settings(bg_units: str = "mg/dL", **fields: Any) -> dict[str, Any]:
    record = {
        "type": "pumpSettings",
        "id": "fake-settings-1",
        "time": _iso(_ago(2)),
        "deviceId": PUMP_DEVICE,
        "serialNumber": SERIAL,
        "timezoneOffset": PUMP_OFFSET,
        "activeSchedule": "Weekday",
        "basalSchedules": {
            "Weekday": [
                _entry(0, rate=0.8),
                _entry(6, rate=1.0),
                _entry(22, rate=0.7),
            ],
            "Weekend": [_entry(0, rate=0.5)],
        },
        # Named differently from the active basal schedule, and the only one.
        "carbRatios": {"Default": [_entry(0, amount=12), _entry(20, amount=8)]},
        "insulinSensitivities": {"Default": [_entry(0, amount=50)]},
        "bgTargets": {
            "Default": [
                _entry(0, low=100, high=110),
                _entry(22, low=90, high=120),
            ]
        },
        "basal": {"rateMaximum": {"units": "Units/hour", "value": 3.5}},
        "bolus": {"amountMaximum": {"units": "Units", "value": 10}},
        "insulinModel": {"modelType": "rapidAdult"},
        "units": {"bg": bg_units, "carb": "grams", "insulin": "Units"},
    }
    return record | fields


def _cgm_settings(units: str = "mg/dL", **fields: Any) -> dict[str, Any]:
    record = {
        "type": "cgmSettings",
        "id": "fake-cgm-settings-1",
        "time": _iso(_ago(3)),
        "deviceId": CGM_DEVICE,
        "transmitterId": TRANSMITTER,
        "highAlerts": {"enabled": True, "level": 180, "snooze": 1_800_000},
        "lowAlerts": {"enabled": False, "level": 70, "snooze": 900_000},
        "units": units,
    }
    return record | fields


# Dose suggestion values here are distinctive so a leak would be easy to see.
SUGGESTION = {"carb": 4.55, "correction": 0.77, "net": 5.27}


def _wizard(**fields: Any) -> dict[str, Any]:
    record = {
        "type": "wizard",
        "id": "fake-wizard-1",
        "time": _iso(NOW - timedelta(minutes=30)),
        "deviceId": PUMP_DEVICE,
        "units": "mg/dL",
        "bgInput": 180,
        "carbInput": 45,
        "insulinOnBoard": 1.25,
        "insulinCarbRatio": 10,
        "insulinSensitivity": 50,
        "bgTarget": {"target": 100},
        "bolus": "fake-bolus-1",
        "recommended": SUGGESTION,
    }
    return record | fields


def _reported_state(**fields: Any) -> dict[str, Any]:
    record = {
        "type": "reportedState",
        "id": "fake-state-1",
        "time": _iso(_ago(4)),
        "states": [
            {"state": "stress", "severity": 3},
            {"state": "other", "stateOther": "FAKE FREE TEXT"},
            {"state": "alcohol"},
            {"state": "stress", "severity": 1},
            {"state": "not-a-listed-state"},
        ],
    }
    return record | fields


def _water(units: str = "milliliters", value: float = 250, **fields: Any) -> dict:
    record = {
        "type": "water",
        "id": "fake-water-1",
        "time": _iso(_ago(1)),
        "amount": {"units": units, "value": value},
    }
    return record | fields


def _basal(**fields: Any) -> dict[str, Any]:
    return {
        "type": "basal",
        "id": "fake-basal-1",
        "time": _iso(_ago(1)),
        "deviceId": PUMP_DEVICE,
        "deliveryType": "scheduled",
        "duration": HOUR_MS,
        "rate": 0.8,
        **fields,
    }


def _cbg() -> dict[str, Any]:
    return {
        "type": "cbg",
        "id": "fake-cbg-1",
        "time": _iso(NOW - timedelta(minutes=5)),
        "deviceId": CGM_DEVICE,
        "value": 5.5,
        "units": "mmol/L",
    }


FULL = [
    _basal(),
    _cbg(),
    _pump_settings(),
    _cgm_settings(),
    _wizard(),
    _reported_state(),
    _water(),
]


# --- value_at -----------------------------------------------------------

SCHEDULE = [_entry(0, name="night"), _entry(6, name="day"), _entry(22, name="late")]


@pytest.mark.parametrize(
    ("local_time", "expected"),
    [
        (time(0, 0), "night"),
        (time(5, 59, 59, 999_000), "night"),
        # Exactly at an entry's start the entry applies.
        (time(6, 0), "day"),
        (time(21, 59), "day"),
        (time(22, 0), "late"),
        (time(23, 59, 59), "late"),
        # A timedelta is the offset from midnight; a datetime has its own time.
        (timedelta(hours=6), "day"),
        (timedelta(hours=5, minutes=59, seconds=59), "night"),
        (datetime(2026, 11, 1, 22, 0, tzinfo=UTC), "late"),
        (datetime(2026, 3, 8, 0, 0, tzinfo=timezone(timedelta(hours=-8))), "night"),
    ],
)
def test_value_at_picks_active_schedule_entry(
    local_time: time | timedelta | datetime, expected: str
) -> None:
    entry = value_at(SCHEDULE, local_time)

    assert entry is not None
    assert entry["name"] == expected


def test_value_at_accepts_any_order_and_skips_malformed_entries() -> None:
    schedule = [
        _entry(22, name="late"),
        "junk",
        {"name": "no start"},
        {"start": "6", "name": "text start"},
        {"start": True, "name": "bool start"},
        _entry(6, name="day"),
        None,
    ]

    assert value_at(schedule, time(12))["name"] == "day"
    assert value_at(schedule, time(23))["name"] == "late"


def test_value_at_wraps_to_the_previous_days_last_entry() -> None:
    # A schedule whose first entry starts after midnight carries the previous
    # day's last entry over midnight: before the first entry, that one applies.
    schedule = [_entry(6, name="day"), _entry(22, name="late")]

    for before_first in (time(0, 0), time(3), time(5, 59, 59, 999_000)):
        assert value_at(schedule, before_first)["name"] == "late"
    assert value_at(schedule, time(6, 0))["name"] == "day"
    # A single entry applies all day, whatever its start.
    assert value_at([_entry(6, name="only")], time(3))["name"] == "only"
    # A schedule starting at midnight never wraps: nothing is before it.
    assert value_at(SCHEDULE, time(0, 0))["name"] == "night"


@pytest.mark.parametrize("schedule", [None, [], {}, "junk", 5, [None], [{"x": 1}]])
def test_value_at_without_a_usable_schedule_is_none(schedule: Any) -> None:
    assert value_at(schedule, time(12)) is None


# --- the pump's clock ---------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "hour"),
    [
        # The zone name wins, and follows daylight saving time: Sydney was on
        # UTC+11 at NOW.
        ({"timezone": "Australia/Sydney", "timezoneOffset": -300}, 23),
        # The offset the schedules were made in, then the record's own offset.
        ({"scheduleTimeZoneOffset": 120, "timezoneOffset": -300}, 14),
        ({"timezoneOffset": 360}, 18),
        ({"timezoneOffset": -420}, 5),
        # Fall back down the list when a field is unusable.
        ({"timezone": "Not/AZone", "timezoneOffset": 60}, 13),
        ({"timezone": 5, "scheduleTimeZoneOffset": "x", "timezoneOffset": 60}, 13),
        ({"timezoneOffset": 10_080}, 4),
        # Last resort: Home Assistant's own zone (UTC-8 here).
        ({}, 4),
    ],
)
def test_pump_local_time_derivation(settings: dict, hour: int) -> None:
    default = timezone(timedelta(hours=-8))

    local = pump_local_time(settings, NOW, default)

    assert local.tzinfo is not None
    assert (local.hour, local.minute) == (hour % 24, 0)
    assert local == NOW


HOME = "America/New_York"  # UTC-4 in summer, UTC-5 in winter
SUMMER = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
WINTER = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("settings", "basis", "winter_hour"),
    [
        # Uploaded in summer at the home zone's summer offset: the pump was
        # set to the home zone, so in winter it follows it to UTC-5.
        ({"time": _iso(SUMMER), "timezoneOffset": -240}, "home_zone", 7),
        ({"time": _iso(SUMMER), "scheduleTimeZoneOffset": -240}, "home_zone", 7),
        # Uploaded in winter at the winter offset: the same zone.
        ({"time": _iso(WINTER), "timezoneOffset": -300}, "home_zone", 7),
        # Another offset than the home zone's then: kept fixed.
        ({"time": _iso(SUMMER), "timezoneOffset": 120}, "fixed_offset", 14),
        # The home zone's winter offset, but uploaded in summer: not the home
        # zone (it was UTC-4 then), so kept fixed.
        ({"time": _iso(SUMMER), "timezoneOffset": -300}, "fixed_offset", 7),
        ({"time": _iso(SUMMER), "timezoneOffset": -360}, "fixed_offset", 6),
        # Without a usable record time the offset cannot be matched.
        ({"timezoneOffset": -240}, "fixed_offset", 8),
        ({"time": "junk", "timezoneOffset": -240}, "fixed_offset", 8),
        # A zone name wins; nothing at all is the home zone.
        ({"timezone": "Europe/Paris", "timezoneOffset": -240}, "named_zone", 13),
        ({"time": _iso(SUMMER)}, "home_zone", 7),
    ],
)
def test_pump_zone_basis(settings: dict, basis: str, winter_hour: int) -> None:
    home = ZoneInfo(HOME)

    zone, found = pump_zone(settings, home)

    assert found == basis
    assert WINTER.astimezone(zone).hour == winter_hour


def test_pump_local_time_follows_the_home_zone_across_dst() -> None:
    settings = {"time": _iso(SUMMER), "timezoneOffset": -240}
    home = ZoneInfo(HOME)

    assert pump_local_time(settings, SUMMER, home).hour == 8
    # Read in winter: an hour behind the fixed summer offset.
    assert pump_local_time(settings, WINTER, home).hour == 7


# --- glucose units ------------------------------------------------------


def test_convert_glucose() -> None:
    assert convert_glucose(180, "mg/dL", "mmol/L") == 9.99
    assert convert_glucose(5.5, "mmol/L", "mg/dL") == 99.1
    # The same unit passes through untouched.
    assert convert_glucose(180, "mg/dL", "mg/dL") == 180
    assert convert_glucose(5.55, "mmol/L", "mmol/L") == 5.55


# --- entities -----------------------------------------------------------


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    latest: list[dict],
    units: UnitSystem = METRIC_SYSTEM,
    *,
    enable: bool = True,
) -> MockConfigEntry:
    freezer.move_to(NOW)
    hass.config.units = units
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=latest)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    if enable:
        await _enable(hass, ALL_KEYS)
        assert entry.state is ConfigEntryState.LOADED
    return entry


def _registered(hass: HomeAssistant, key: str) -> er.RegistryEntry:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{USER_ID}_{key}")
    assert entity_id is not None, key
    return registry.entities[entity_id]


async def _enable(hass: HomeAssistant, keys: tuple[str, ...]) -> None:
    """Enable disabled-by-default sensors, as a user would, and let the entry
    reload that follows finish."""
    registry = er.async_get(hass)
    for key in keys:
        registry.async_update_entity(_registered(hass, key).entity_id, disabled_by=None)
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=40))
    await hass.async_block_till_done()


def _patient_device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, USER_ID), entry.entry_id
    )
    assert device is not None
    return device


def _state(hass: HomeAssistant, key: str) -> State:
    state = hass.states.get(_registered(hass, key).entity_id)
    assert state is not None, f"{key} is disabled or not written"
    return state


def _problems(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and (r.levelno >= logging.ERROR or r.name.startswith("custom_components"))
    ]


async def test_settings_disabled_by_default(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, FULL, enable=False)

    pump = _registered(hass, "basal_rate").device_id
    cgm = _registered(hass, "glucose").device_id
    patient = _patient_device(hass, entry)
    assert len({pump, cgm, patient.id}) == 3
    for key in ALL_KEYS:
        registered = _registered(hass, key)
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
        assert hass.states.get(registered.entity_id) is None, key
        assert registered.unique_id == f"{USER_ID}_{key}"
        assert registered.translation_key == key
        assert registered.original_name
        category = registered.entity_category
        assert category == ("diagnostic" if key in PUMP_KEYS + CGM_KEYS else None)
    # Pump settings and the calculator record are the pump's; alert levels are
    # the CGM's; a reported state and water are not any device's.
    for key in (*PUMP_KEYS, "last_wizard_entry"):
        assert _registered(hass, key).device_id == pump, key
    for key in CGM_KEYS:
        assert _registered(hass, key).device_id == cgm, key
    for key in ("reported_state", "water_intake"):
        assert _registered(hass, key).device_id == patient.id, key


async def test_entities_use_the_patient_device_without_a_pump(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    anonymous = [
        {k: v for k, v in r.items() if k != "deviceId"}
        for r in (_pump_settings(), _wizard(), _cgm_settings())
    ]
    entry = await _setup(hass, aioclient_mock, freezer, anonymous, enable=False)

    patient = _patient_device(hass, entry)
    for key in ALL_KEYS:
        assert _registered(hass, key).device_id == patient.id, key


async def test_names_avoid_dose_words(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL, enable=False)

    component = Path(__file__).parent.parent / "custom_components" / DOMAIN
    strings = json.loads((component / "strings.json").read_text())
    assert strings == json.loads((component / "translations" / "en.json").read_text())
    for key in ALL_KEYS:
        registered = _registered(hass, key)
        texts = [
            registered.entity_id,
            registered.original_name,
            strings["entity"]["sensor"][key]["name"],
        ]
        for text in texts:
            assert "recommend" not in text.lower(), key
            assert "correction" not in text.lower(), key


async def test_pump_settings_show_the_schedule_entry_now(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)

    # 22:00 on the pump: the entries that start at 22 and 20 are in force.
    expected = {
        "active_schedule": ("Weekday", None),
        "scheduled_basal_now": ("0.7", "U/h"),
        "carb_ratio_now": ("8", "g/U"),
        # Metric Home Assistant, pump in mg/dL: 50 mg/dL and 90..120 mg/dL.
        "sensitivity_now": ("2.77", "mmol/L/U"),
        "target_low_now": ("4.99", "mmol/L"),
        "target_high_now": ("6.66", "mmol/L"),
        "max_basal": ("3.5", "U/h"),
        "max_bolus": ("10", "U"),
        "insulin_model": ("rapidAdult", None),
    }
    for key, (value, unit) in expected.items():
        state = _state(hass, key)
        assert state.state == value, key
        assert state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) == unit, key
    for key in ("target_low_now", "target_high_now"):
        assert _state(hass, key).attributes["device_class"] == (
            "blood_glucose_concentration"
        )
    assert "device_class" not in _state(hass, "sensitivity_now").attributes


async def test_schedule_follows_the_pump_clock(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, FULL)
    fast = entry.runtime_data.fast[USER_ID]

    # The pump is on UTC+10: its midnight is 14:00 UTC. Basal entries start at
    # 00:00 (0.8), 06:00 (1.0) and 22:00 (0.7).
    for utc, rate in (
        ("2026-10-31T14:00:00Z", "0.8"),  # midnight exactly
        ("2026-10-31T13:59:59Z", "0.7"),  # a second before: the last entry
        ("2026-10-31T19:59:59Z", "0.8"),
        ("2026-10-31T20:00:00Z", "1.0"),  # exactly at an entry's start
        ("2026-11-01T11:59:59Z", "1.0"),
        ("2026-11-01T12:00:00Z", "0.7"),  # exactly at the last entry's start
    ):
        freezer.move_to(datetime.fromisoformat(utc))
        await fast.async_refresh()
        await hass.async_block_till_done()
        assert _state(hass, "scheduled_basal_now").state == rate, utc


async def test_schedule_moves_with_the_clock_between_refreshes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)
    assert _state(hass, "scheduled_basal_now").state == "0.7"

    # Two hours on it is midnight at the pump: no refresh, only the minute tick.
    freezer.tick(timedelta(hours=2))
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(minutes=1))
    await hass.async_block_till_done()

    assert _state(hass, "scheduled_basal_now").state == "0.8"


async def test_pump_zone_by_name_follows_daylight_saving_time(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sydney is on UTC+11 in November, whatever offset the record carries:
    # 23:00 there at NOW, so the entry that starts at 22:00 applies (at the
    # record's UTC-5 it would be 07:00).
    settings = _pump_settings(timezone="Australia/Sydney", timezoneOffset=-300)
    await _setup(hass, aioclient_mock, freezer, [settings])

    assert _state(hass, "scheduled_basal_now").state == "0.7"
    assert _state(hass, "target_low_now").state == "4.99"
    assert _state(hass, "target_low_now").attributes["zone_basis"] == "named_zone"
    # Loading the zone's data must not block the event loop.
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name != "homeassistant.loader"
    ]


async def test_schedule_without_a_clock_uses_home_assistant_zone(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone("Etc/GMT-5")  # UTC+5
    settings = _pump_settings()
    del settings["timezoneOffset"]
    await _setup(hass, aioclient_mock, freezer, [settings])

    # 17:00 in Home Assistant's zone.
    assert _state(hass, "scheduled_basal_now").state == "1.0"
    basis = _state(hass, "scheduled_basal_now").attributes["zone_basis"]
    assert basis == "home_zone"


SCHEDULE_NOW_KEYS = (
    "scheduled_basal_now",
    "carb_ratio_now",
    "sensitivity_now",
    "target_low_now",
    "target_high_now",
)


async def test_schedule_follows_the_home_zone_across_dst(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A summer upload at the home zone's offset, read in winter."""
    await hass.config.async_set_time_zone(HOME)
    settings = _pump_settings(time=_iso(SUMMER), timezoneOffset=-240)
    entry = await _setup(hass, aioclient_mock, freezer, [settings])
    fast = entry.runtime_data.fast[USER_ID]

    # 05:30 at the pump in summer (UTC-4) and in winter (UTC-5); a fixed
    # summer offset would make the winter one 06:30, when 1.0 starts.
    for when in (
        datetime(2026, 7, 16, 9, 30, tzinfo=UTC),
        datetime(2026, 12, 1, 10, 30, tzinfo=UTC),
    ):
        freezer.move_to(when)
        await fast.async_refresh()
        await hass.async_block_till_done()
        assert _state(hass, "scheduled_basal_now").state == "0.8", when
    for key in SCHEDULE_NOW_KEYS:
        assert _state(hass, key).attributes["zone_basis"] == "home_zone", key
    # Only the sensors that follow the schedule carry it.
    for key in ("active_schedule", "max_bolus", "insulin_model"):
        assert "zone_basis" not in _state(hass, key).attributes, key


async def test_schedule_keeps_a_fixed_offset_unlike_the_home_zone(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await hass.config.async_set_time_zone(HOME)
    # UTC+10 is not New York's offset at any time of year.
    entry = await _setup(hass, aioclient_mock, freezer, FULL)
    fast = entry.runtime_data.fast[USER_ID]

    # 05:30 at UTC+10 on either side of New York's change: the same entry.
    for when in (
        datetime(2026, 7, 15, 19, 30, tzinfo=UTC),
        datetime(2026, 11, 30, 19, 30, tzinfo=UTC),
    ):
        freezer.move_to(when)
        await fast.async_refresh()
        await hass.async_block_till_done()
        assert _state(hass, "scheduled_basal_now").state == "0.8", when
    for key in SCHEDULE_NOW_KEYS:
        assert _state(hass, key).attributes["zone_basis"] == "fixed_offset", key


@pytest.mark.parametrize(
    ("units", "pump_bg", "levels", "expected"),
    [
        # Same unit: untouched. Different: the shared factor.
        (METRIC_SYSTEM, "mmol/L", (5.0, 7.0, 2.5, 10.0, 3.9), "5.0 7.0 2.5 10.0 3.9"),
        (METRIC_SYSTEM, "mg/dL", (90, 120, 45, 180, 70), "4.99 6.66 2.5 9.99 3.88"),
        (US_CUSTOMARY_SYSTEM, "mg/dL", (90, 120, 45, 180, 70), "90 120 45 180 70"),
        (
            US_CUSTOMARY_SYSTEM,
            "mmol/L",
            (5.0, 7.0, 2.5, 10.0, 3.9),
            "90.1 126.1 45.0 180.2 70.3",
        ),
        # The unit's spelling varies in real records.
        (METRIC_SYSTEM, "mg/dl", (90, 120, 45, 180, 70), "4.99 6.66 2.5 9.99 3.88"),
    ],
)
async def test_glucose_settings_follow_the_unit_system(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    units: UnitSystem,
    pump_bg: str,
    levels: tuple[float, ...],
    expected: str,
) -> None:
    low, high, sensitivity, alert_high, alert_low = levels
    settings = _pump_settings(
        pump_bg,
        bgTargets={"Default": [_entry(0, low=low, high=high)]},
        insulinSensitivities={"Default": [_entry(0, amount=sensitivity)]},
    )
    cgm = _cgm_settings(
        pump_bg,
        highAlerts={"enabled": True, "level": alert_high, "snooze": 0},
        lowAlerts={"enabled": True, "level": alert_low, "snooze": 0},
    )
    await _setup(hass, aioclient_mock, freezer, [settings, cgm], units)

    keys = (
        "target_low_now",
        "target_high_now",
        "sensitivity_now",
        "alert_high",
        "alert_low",
    )
    assert " ".join(_state(hass, k).state for k in keys) == expected
    unit = "mg/dL" if units is US_CUSTOMARY_SYSTEM else "mmol/L"
    for key in ("target_low_now", "target_high_now", "alert_high", "alert_low"):
        assert _state(hass, key).attributes[ATTR_UNIT_OF_MEASUREMENT] == unit
    sensitivity = _state(hass, "sensitivity_now")
    assert sensitivity.attributes[ATTR_UNIT_OF_MEASUREMENT] == f"{unit}/U"


async def test_settings_without_a_glucose_unit_are_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # Guessing the unit would be a wrong number, not an unknown one.
    settings = _pump_settings(units={"carb": "grams"})
    cgm = _cgm_settings(units="furlongs")
    await _setup(hass, aioclient_mock, freezer, [settings, cgm])

    for key in (
        "sensitivity_now",
        "target_low_now",
        "target_high_now",
        "alert_high",
        "alert_low",
    ):
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert _state(hass, "carb_ratio_now").state == "8"


async def test_informational_settings_say_so(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)

    as_of = _ago(2).isoformat()
    for key in (*INFORMATIONAL_KEYS, "target_high_now"):
        attributes = _state(hass, key).attributes
        assert attributes["as_of"] == as_of, key
        assert attributes["note"] == "Pump setting as last uploaded; not advice."
        assert attributes["note"] == SETTING_NOTE
    # Values that are not guidance carry no such label.
    assert "note" not in _state(hass, "max_bolus").attributes


async def test_schedules_pick_the_active_or_the_only_one(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    settings = _pump_settings(
        activeSchedule="Holiday",
        # Two schedules and neither is active: not a guess between them.
        carbRatios={
            "Weekday": [_entry(0, amount=12)],
            "Weekend": [_entry(0, amount=9)],
        },
        # Named like the active schedule: that one.
        insulinSensitivities={
            "Weekend": [_entry(0, amount=30)],
            "Holiday": [_entry(0, amount=60)],
        },
    )
    await _setup(hass, aioclient_mock, freezer, [settings])

    assert _state(hass, "active_schedule").state == "Holiday"
    # No basal schedule is named active, and neither of two is a guess.
    assert _state(hass, "scheduled_basal_now").state == STATE_UNKNOWN
    assert _state(hass, "carb_ratio_now").state == STATE_UNKNOWN
    # 60 mg/dL in mmol/L.
    assert _state(hass, "sensitivity_now").state == "3.33"


async def test_cgm_alert_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)

    high = _state(hass, "alert_high")
    assert high.attributes["enabled"] is True
    assert high.attributes["snooze_minutes"] == 30
    low = _state(hass, "alert_low")
    assert low.attributes["enabled"] is False
    assert low.attributes["snooze_minutes"] == 15
    assert high.attributes["device_class"] == "blood_glucose_concentration"


async def test_wizard_record_is_read_only_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)

    state = _state(hass, "last_wizard_entry")
    assert state.state == (NOW - timedelta(minutes=30)).isoformat()
    assert state.attributes["device_class"] == "timestamp"
    attributes = {k: v for k, v in state.attributes.items() if k not in HA_ATTRIBUTES}
    # The safe list: what was typed in, the pump's own on-board figure with
    # its label, and when. Nothing from the dose suggestion.
    assert set(attributes) == {
        "carbs_entered",
        "glucose_entered",
        "insulin_on_board",
        "time",
        "as_of",
        "note",
    }
    assert attributes == {
        "carbs_entered": 45,
        "glucose_entered": 9.99,
        "insulin_on_board": 1.25,
        "time": (NOW - timedelta(minutes=30)).isoformat(),
        "as_of": (NOW - timedelta(minutes=30)).isoformat(),
        "note": AS_OF_NOTE,
    }
    text = str(state.attributes) + state.state
    for value in SUGGESTION.values():
        assert str(value) not in text
    for word in ("recommend", "correction"):
        assert word not in text.lower()


async def test_wizard_missing_fields_are_unknown_attributes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    sparse = {
        "type": "wizard",
        "id": "fake-wizard-2",
        "time": _iso(_ago(1)),
        "bgInput": 8,
    }
    await _setup(hass, aioclient_mock, freezer, [sparse])

    attributes = _state(hass, "last_wizard_entry").attributes
    assert attributes["carbs_entered"] is None
    assert attributes["insulin_on_board"] is None
    # No unit: not a number to trust.
    assert attributes["glucose_entered"] is None


async def test_reported_state_and_water(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, FULL)

    reported = _state(hass, "reported_state")
    assert reported.state == _ago(4).isoformat()
    # The names the spec lists, once each, in the order given; what is not
    # listed is "other"; free text is never shown.
    assert reported.attributes["states"] == ["stress", "other", "alcohol"]
    assert "FAKE FREE TEXT" not in str(reported.attributes)
    water = _state(hass, "water_intake")
    assert water.state == "250"
    assert water.attributes[ATTR_UNIT_OF_MEASUREMENT] == "mL"
    assert water.attributes["time"] == _ago(1).isoformat()


@pytest.mark.parametrize(
    ("units", "unit"),
    [
        ("gallons", "gal"),
        ("ounces", "fl. oz."),
        ("liters", "L"),
        ("milliliters", "mL"),
    ],
)
async def test_water_keeps_its_units(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    units: str,
    unit: str,
) -> None:
    await _setup(hass, aioclient_mock, freezer, [_water(units, 12)])

    water = _state(hass, "water_intake")
    assert water.state == "12"
    assert water.attributes[ATTR_UNIT_OF_MEASUREMENT] == unit


async def test_water_in_an_unlisted_unit_is_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, [_water("hogsheads", 2)])

    assert _state(hass, "water_intake").state == STATE_UNKNOWN


async def test_missing_settings_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, [])

    for key in ALL_KEYS:
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert entry.state is ConfigEntryState.LOADED
    assert not _problems(caplog)


async def test_malformed_settings_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    junk = [
        _pump_settings(
            activeSchedule=["x"],
            basalSchedules=[1, 2],
            carbRatios={"Default": "no"},
            insulinSensitivities={"Default": [{"start": "0", "amount": 5}]},
            bgTargets={"Default": [{"start": 0, "low": "x", "high": None}]},
            basal={"rateMaximum": "x"},
            bolus=[],
            insulinModel={"modelType": 5},
            timezoneOffset="x",
        ),
        _cgm_settings(highAlerts="x", lowAlerts={"level": "x", "snooze": "x"}),
        _wizard(bgInput="x", carbInput=None, insulinOnBoard=True, units=5),
        _reported_state(states="x"),
        _water(amount="x"),
    ]
    await _setup(hass, aioclient_mock, freezer, junk)

    for key in ALL_KEYS:
        if key in ("last_wizard_entry", "reported_state"):
            continue
        assert _state(hass, key).state == STATE_UNKNOWN, key
    assert _state(hass, "reported_state").attributes["states"] == []
    assert not _problems(caplog)


async def test_partial_pump_settings_fill_what_they_can(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(
        hass, aioclient_mock, freezer, [{**_pump_settings(), "basalSchedules": None}]
    )

    assert _state(hass, "scheduled_basal_now").state == STATE_UNKNOWN
    assert _state(hass, "max_basal").state == "3.5"
    assert _state(hass, "active_schedule").state == "Weekday"
    assert not _problems(caplog)


async def test_identifying_data_never_shown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer, FULL)

    texts = []
    for registered in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        texts += [registered.entity_id, str(registered.original_name)]
        state = hass.states.get(registered.entity_id)
        if state is not None:
            texts += [state.state, str(state.attributes)]
    assert texts
    for text in texts:
        for secret in (SERIAL, TRANSMITTER, PUMP_DEVICE, CGM_DEVICE):
            assert secret not in text
    # And no entity is made of the transmitter.
    assert not [
        e
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if "transmitter" in e.unique_id
    ]
