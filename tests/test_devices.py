"""Tests for the child devices and the data health entities.

All records, device ids, serial numbers and data source ids are synthetic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import DOMAIN
from custom_components.tidepool.devices import DeviceRole, child_devices
from custom_components.tidepool.models import parse_latest

from .entries import (
    USER_ID,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

PUMP_ID = "fake-pump-device-0001"
CGM_ID = "fake-cgm-device-0001"
PHONE_ID = "fake-phone-device-0001"
SERIAL = "FAKESERIAL9999"
SOURCE_ON = "fakesource00000000000000000000001"
SOURCE_ERROR = "fakesource00000000000000000000002"
SOURCE_OFF = "fakesource00000000000000000000003"

# Every entity of these keys belongs to the pump device once there is one.
PUMP_KEYS = (
    ("sensor", "basal_rate"),
    ("sensor", "basal_mode"),
    ("sensor", "last_bolus"),
    ("sensor", "insulin_total_24h"),
    ("sensor", "carbs_24h"),
    ("sensor", "iob_at_last_decision"),
    ("binary_sensor", "automated_delivery_active"),
    ("binary_sensor", "delivery_suspended"),
)
CGM_KEYS = (("sensor", "glucose"), ("sensor", "glucose_age"))
PATIENT_KEYS = (("sensor", "summary_cgm_14d_time_in_range"), ("sensor", "last_upload"))


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(record_type: str, minutes_ago: int, **fields: Any) -> dict[str, Any]:
    return {
        "type": record_type,
        "id": f"rec-{record_type}",
        "time": _iso(NOW - timedelta(minutes=minutes_ago)),
        **fields,
    }


def _data_set(
    upload_id: str,
    device_id: str,
    minutes_ago: int,
    *,
    manufacturers: list[str],
    model: str | None,
    tags: list[str],
) -> dict[str, Any]:
    data_set: dict[str, Any] = {
        "type": "upload",
        "id": upload_id,
        "uploadId": upload_id,
        "deviceId": device_id,
        "deviceManufacturers": manufacturers,
        "deviceSerialNumber": SERIAL,
        "deviceTags": tags,
        "dataSetType": "normal",
        "client": {"name": "org.example.fakeuploader", "version": "1.0.0"},
        "time": _iso(NOW - timedelta(minutes=minutes_ago)),
        "createdTime": _iso(NOW - timedelta(minutes=minutes_ago)),
    }
    if model is not None:
        data_set["deviceModel"] = model
    return data_set


def _source(source_id: str, provider: str, state: str) -> dict[str, Any]:
    return {
        "id": source_id,
        "providerName": provider,
        "providerType": "oauth",
        "providerExternalId": "fake-external-0001",
        "state": state,
        "dataSetId": "fake-data-set-0001",
        "createdTime": _iso(NOW - timedelta(days=30)),
        "modifiedTime": _iso(NOW - timedelta(hours=1)),
        "earliestDataTime": _iso(NOW - timedelta(days=30)),
        "latestDataTime": _iso(NOW - timedelta(minutes=5)),
        "lastImportTime": _iso(NOW - timedelta(minutes=4)),
    }


PUMP_RECORDS = [
    _record(
        "basal",
        10,
        deviceId=PUMP_ID,
        uploadId="up-pump",
        deliveryType="automated",
        rate=0.8,
        duration=1_800_000,
    ),
    _record("bolus", 60, deviceId=PUMP_ID, uploadId="up-pump", normal=1.5),
]
CGM_RECORDS = [
    _record("cbg", 5, deviceId=CGM_ID, uploadId="up-cgm", value=5.5),
    _record("cgmSettings", 600, deviceId=CGM_ID, uploadId="up-cgm"),
]
# A phone health source: its records carry an upload id but no device id.
PHONE_RECORDS = [_record("smbg", 30, uploadId="up-phone", value=6.1)]
LATEST = [*PUMP_RECORDS, *CGM_RECORDS, *PHONE_RECORDS]

DATA_SETS = [
    _data_set(
        "up-pump",
        PUMP_ID,
        20,
        manufacturers=["FakeCorp"],
        model="Pump X1",
        tags=["insulin-pump"],
    ),
    _data_set(
        "up-cgm",
        CGM_ID,
        90,
        manufacturers=["FakeSense"],
        model="FakeSense G1",
        tags=["cgm"],
    ),
    _data_set(
        "up-phone",
        PHONE_ID,
        45,
        manufacturers=["FakeHealth"],
        model=None,
        tags=["bgm"],
    ),
]

# Strings that must never reach a device, entity name, state or attribute.
SECRETS = (PUMP_ID, CGM_ID, PHONE_ID, SERIAL, SOURCE_ON, SOURCE_ERROR, "up-pump")


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    latest: list[dict] | None = None,
    data_sets: list[dict] | None = None,
    data_sources: list[dict] | None = None,
) -> MockConfigEntry:
    freezer.move_to(NOW)
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=latest or [])
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock, data_sets=data_sets, data_sources=data_sources)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _entity(hass: HomeAssistant, platform: str, key: str) -> er.RegistryEntry:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(platform, DOMAIN, f"{USER_ID}_{key}")
    assert entity_id is not None, key
    registered = registry.async_get(entity_id)
    assert registered is not None
    return registered


def _device(hass: HomeAssistant, entry: MockConfigEntry, key: str) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, key), entry.entry_id
    )
    assert device is not None, key
    return device


def _child_devices(hass: HomeAssistant, entry: MockConfigEntry) -> list:
    patient = _device(hass, entry, USER_ID)
    return [
        device
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        if device.id != patient.id
    ]


# --- device detection ---------------------------------------------------


def test_device_roles_from_record_types() -> None:
    combo = [
        _record("pumpSettings", 5, deviceId="fake-combo"),
        _record("cbg", 5, deviceId="fake-combo", value=5.5),
    ]
    devices = child_devices(parse_latest([*LATEST, *combo]), DATA_SETS)

    assert {key: device.role for key, device in devices.items()} == {
        PUMP_ID: DeviceRole.PUMP,
        CGM_ID: DeviceRole.CGM,
        # Found through the upload id of its records.
        PHONE_ID: DeviceRole.OTHER,
        # A pump that also reads glucose is named as a pump.
        "fake-combo": DeviceRole.PUMP,
    }
    assert devices[PUMP_ID].manufacturer == "FakeCorp"
    assert devices[PUMP_ID].name == "FakeCorp Pump X1"
    # The model already names the manufacturer.
    assert devices[CGM_ID].name == "FakeSense G1"
    assert devices[PHONE_ID].name == "Data source"


def test_records_without_device_are_skipped() -> None:
    latest = parse_latest(
        [
            _record("food", 5),
            _record("bolus", 5, deviceId="", uploadId="up-unknown", normal=1.0),
        ]
    )
    assert child_devices(latest, DATA_SETS) == {}


# --- child devices --------------------------------------------------------


async def test_child_devices_created_per_device(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(
        hass, aioclient_mock, freezer, latest=LATEST, data_sets=DATA_SETS
    )

    patient = _device(hass, entry, USER_ID)
    pump = _device(hass, entry, f"{USER_ID}_{PUMP_ID}")
    cgm = _device(hass, entry, f"{USER_ID}_{CGM_ID}")
    phone = _device(hass, entry, f"{USER_ID}_{PHONE_ID}")
    assert len(_child_devices(hass, entry)) == 3
    for child in (pump, cgm, phone):
        assert child.via_device_id == patient.id
        assert child.serial_number is None
    assert (pump.name, pump.manufacturer, pump.model) == (
        "FakeCorp Pump X1",
        "FakeCorp",
        "Pump X1",
    )
    assert (cgm.name, cgm.manufacturer, cgm.model) == (
        "FakeSense G1",
        "FakeSense",
        "FakeSense G1",
    )
    assert (phone.name, phone.model) == ("Data source", "Unknown device")


async def test_unknown_device_falls_back(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    # No data set describes either device.
    entry = await _setup(
        hass, aioclient_mock, freezer, latest=[*PUMP_RECORDS, *CGM_RECORDS]
    )

    pump = _device(hass, entry, f"{USER_ID}_{PUMP_ID}")
    cgm = _device(hass, entry, f"{USER_ID}_{CGM_ID}")
    assert (pump.name, pump.manufacturer, pump.model) == (
        "Insulin pump",
        None,
        "Unknown device",
    )
    assert (cgm.name, cgm.model) == ("CGM", "Unknown device")


async def test_entities_attach_to_pump_and_cgm(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(
        hass, aioclient_mock, freezer, latest=LATEST, data_sets=DATA_SETS
    )

    expected = (
        (PUMP_KEYS, _device(hass, entry, f"{USER_ID}_{PUMP_ID}")),
        (CGM_KEYS, _device(hass, entry, f"{USER_ID}_{CGM_ID}")),
        (PATIENT_KEYS, _device(hass, entry, USER_ID)),
    )
    for keys, device in expected:
        for platform, key in keys:
            registered = _entity(hass, platform, key)
            assert registered.unique_id == f"{USER_ID}_{key}"
            assert registered.device_id == device.id, key


async def test_entities_move_in_place_when_devices_appear(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    patient = _device(hass, entry, USER_ID)
    before = {
        key: _entity(hass, platform, key) for platform, key in PUMP_KEYS + CGM_KEYS
    }
    assert all(r.device_id == patient.id for r in before.values())
    assert _child_devices(hass, entry) == []

    # The next fast refresh brings records from a pump and a CGM.
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, payload=LATEST)
    await entry.runtime_data.fast[USER_ID].async_refresh()
    await hass.async_block_till_done()

    pump = _device(hass, entry, f"{USER_ID}_{PUMP_ID}")
    cgm = _device(hass, entry, f"{USER_ID}_{CGM_ID}")
    assert pump.name == "Insulin pump"
    for keys, device in ((PUMP_KEYS, pump), (CGM_KEYS, cgm)):
        for platform, key in keys:
            registered = _entity(hass, platform, key)
            # The same registry entry, now on the child device.
            assert registered.entity_id == before[key].entity_id
            assert registered.id == before[key].id
            assert registered.device_id == device.id, key

    # The slow refresh then names the pump from its data set.
    aioclient_mock.clear_requests()
    mock_slow(aioclient_mock, data_sets=DATA_SETS)
    mock_profile(aioclient_mock)
    await entry.runtime_data.slow[USER_ID].async_refresh()
    await hass.async_block_till_done()

    pump = _device(hass, entry, f"{USER_ID}_{PUMP_ID}")
    assert (pump.name, pump.model) == ("FakeCorp Pump X1", "Pump X1")


async def test_no_device_id_or_serial_exposed(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    sources = [_source(SOURCE_ON, "fakeprovider", "connected")]
    entry = await _setup(
        hass,
        aioclient_mock,
        freezer,
        latest=LATEST,
        data_sets=DATA_SETS,
        data_sources=sources,
    )

    texts: list[str] = []
    for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id):
        texts += [
            str(getattr(device, field))
            for field in (
                "name",
                "manufacturer",
                "model",
                "model_id",
                "hw_version",
                "sw_version",
                "serial_number",
            )
        ]
    for registered in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        texts += [registered.entity_id, str(registered.original_name)]
        state = hass.states.get(registered.entity_id)
        if state is not None:
            texts += [state.state, str(state.attributes)]
    assert texts
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


# --- data health ----------------------------------------------------------


async def test_last_data_entities(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, latest=LATEST)

    for key in ("cbg", "basal", "bolus"):
        registered = _entity(hass, "sensor", f"last_data_{key}")
        assert registered.disabled_by is None
        assert registered.entity_category == "diagnostic"
    state = hass.states.get(_entity(hass, "sensor", "last_data_cbg").entity_id)
    assert state is not None
    assert state.state == (NOW - timedelta(minutes=5)).isoformat()
    for key in (
        "smbg",
        "food",
        "insulin",
        "physical_activity",
        "device_event",
        "pump_settings",
        "cgm_settings",
        "wizard",
        "upload",
    ):
        registered = _entity(hass, "sensor", f"last_data_{key}")
        assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_last_upload_timestamp(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer, data_sets=DATA_SETS)

    registered = _entity(hass, "sensor", "last_upload")
    assert registered.entity_category == "diagnostic"
    state = hass.states.get(registered.entity_id)
    assert state is not None
    # The pump's upload, 20 minutes ago, is the newest.
    assert state.state == (NOW - timedelta(minutes=20)).isoformat()


async def test_data_source_connectivity_state(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    sources = [
        _source(SOURCE_ON, "fakeprovider", "connected"),
        _source(SOURCE_ERROR, "otherprovider", "error"),
        _source(SOURCE_OFF, "thirdprovider", "disconnected"),
    ]
    await _setup(hass, aioclient_mock, freezer, data_sources=sources)

    expected = {SOURCE_ON: STATE_ON, SOURCE_ERROR: STATE_OFF, SOURCE_OFF: STATE_UNKNOWN}
    for source_id, value in expected.items():
        registered = _entity(hass, "binary_sensor", f"source_{source_id}")
        assert registered.entity_category == "diagnostic"
        assert registered.disabled_by is None
        state = hass.states.get(registered.entity_id)
        assert state is not None
        assert state.state == value
        assert state.attributes["device_class"] == "connectivity"
    state = hass.states.get(
        _entity(hass, "binary_sensor", f"source_{SOURCE_ON}").entity_id
    )
    assert state is not None
    assert state.attributes["friendly_name"] == "Fake Owner fakeprovider connection"


async def test_data_source_added_on_refresh(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    first = [_source(SOURCE_ON, "fakeprovider", "connected")]
    entry = await _setup(hass, aioclient_mock, freezer, data_sources=first)
    slow = entry.runtime_data.slow[USER_ID]

    later = [*first, _source(SOURCE_ERROR, "otherprovider", "error")]
    for _ in range(2):
        aioclient_mock.clear_requests()
        mock_slow(aioclient_mock, data_sources=later)
        mock_profile(aioclient_mock)
        await slow.async_refresh()
        await hass.async_block_till_done()

    sources = [
        registered
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if "_source_" in registered.unique_id
    ]
    assert len(sources) == 2
    state = hass.states.get(
        _entity(hass, "binary_sensor", f"source_{SOURCE_ERROR}").entity_id
    )
    assert state is not None
    assert state.state == STATE_OFF


async def test_sparse_account_has_no_child_devices_or_sources(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)

    assert _child_devices(hass, entry) == []
    assert not [
        registered
        for registered in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if "_source_" in registered.unique_id
    ]
    for key in ("last_upload", "last_data_cbg"):
        state = hass.states.get(_entity(hass, "sensor", key).entity_id)
        assert state is not None
        assert state.state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ]
