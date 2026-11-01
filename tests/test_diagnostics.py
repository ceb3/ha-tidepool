"""Tests for the config entry diagnostics: shape and counts, nothing identifying.

Every id, name, email, serial number, token and the password below is
synthetic, and distinctive enough that finding it anywhere in the download is
a leak.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.diagnostics import (
    _get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.tidepool.diagnostics import async_get_config_entry_diagnostics

from .entries import (
    EMAIL,
    PASSWORD,
    PATIENT_ID,
    SESSION_TOKEN,
    USER_ID,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

OWNER_NAME = "Fakeowner Zyxwv"
PATIENT_NAME = "Fakepatient Qwvut"
PUMP_ID = "fake-pump-device-diag-7Q"
CGM_ID = "fake-cgm-device-diag-3K"
UPLOAD_ID = "fake-upload-diag-91"
SERIAL = "FAKESERIALDIAG55"
TRANSMITTER = "FAKETXDIAG8"
SOURCE_ID = "fake-source-diag-0042"
EXTERNAL_ID = "fake-external-diag-0042"
RECORD_PREFIX = "fake-record-diag-"

SECRETS = (
    EMAIL,
    PASSWORD,
    SESSION_TOKEN,
    USER_ID,
    PATIENT_ID,
    OWNER_NAME,
    PATIENT_NAME,
    PUMP_ID,
    CGM_ID,
    UPLOAD_ID,
    SERIAL,
    TRANSMITTER,
    SOURCE_ID,
    EXTERNAL_ID,
    RECORD_PREFIX,
)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(record_type: str, minutes_ago: int, **fields: Any) -> dict[str, Any]:
    return {
        "type": record_type,
        "id": f"{RECORD_PREFIX}{record_type}",
        "time": _iso(NOW - timedelta(minutes=minutes_ago)),
        "uploadId": UPLOAD_ID,
        **fields,
    }


LATEST = [
    _record("cbg", 3, value=5.5, units="mmol/L", deviceId=CGM_ID),
    _record("smbg", 90, value=6.1, units="mmol/L", deviceId=CGM_ID),
    _record("basal", 20, rate=0.8, duration=1800000, deviceId=PUMP_ID),
    _record("cgmSettings", 600, transmitterId=TRANSMITTER, deviceId=CGM_ID),
]
WINDOW = [
    _record("basal", 20, rate=0.8, duration=1800000, deviceId=PUMP_ID),
    _record("bolus", 40, normal=2.0, subType="normal", deviceId=PUMP_ID)
    | {"id": f"{RECORD_PREFIX}bolus-1"},
    _record("bolus", 200, normal=1.0, subType="normal", deviceId=PUMP_ID)
    | {"id": f"{RECORD_PREFIX}bolus-2"},
]
DATA_SETS = [
    {
        "type": "upload",
        "id": UPLOAD_ID,
        "uploadId": UPLOAD_ID,
        "deviceId": PUMP_ID,
        "deviceManufacturers": ["Fakecorp"],
        "deviceModel": "Fakepump 1",
        "deviceSerialNumber": SERIAL,
        "time": _iso(NOW - timedelta(hours=2)),
    }
]
DATA_SOURCES = [
    {
        "id": SOURCE_ID,
        "providerName": "fakeprovider",
        "providerExternalId": EXTERNAL_ID,
        "state": "connected",
    }
]
DEVICE_EVENTS = {
    "alarm": [
        _record("deviceEvent", 30, subType="alarm", alarmType="low_insulin")
        | {"deviceId": PUMP_ID}
    ]
}


async def _load(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
):
    freezer.move_to(NOW)
    entry = password_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID, payload=LATEST, window=WINDOW)
    mock_profile(aioclient_mock, USER_ID, full_name=OWNER_NAME)
    mock_slow(
        aioclient_mock,
        USER_ID,
        data_sources=DATA_SOURCES,
        data_sets=DATA_SETS,
        device_events=DEVICE_EVENTS,
    )
    # The shared patient has no data at all.
    mock_latest(aioclient_mock, PATIENT_ID, payload=[])
    mock_profile(aioclient_mock, PATIENT_ID, full_name=PATIENT_NAME)
    mock_slow(aioclient_mock, PATIENT_ID, cgm={})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, secret


def _assert_no_record_times(text: str) -> None:
    """No record or upload time either: ages are bucketed."""
    assert "2026-11-01T" not in text
    assert "2026-10-31T" not in text


async def test_diagnostics_redacts_everything(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    started = dt_util.utcnow()
    entry = await _load(hass, aioclient_mock, freezer)
    freezer.tick(timedelta(minutes=2))
    # The shared patient's session expires and the login is then withdrawn:
    # the open repair issue is part of the download too.
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, PATIENT_ID, status=401)
    mock_login(aioclient_mock, status=410)
    await entry.runtime_data.fast[PATIENT_ID].async_refresh()
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_no_secrets(json.dumps(diagnostics))
    _assert_no_record_times(json.dumps(diagnostics))

    # The whole download, through Home Assistant, with the open issue. Back at
    # the real time: the test client's access token is not valid at NOW.
    freezer.move_to(started)
    download = await _get_diagnostics_for_config_entry(hass, hass_client, entry)
    _assert_no_secrets(json.dumps(download))
    _assert_no_record_times(json.dumps(download["data"]))
    [issue] = download["issues"]
    assert issue["issue_id"] == f"login_withdrawn_{entry.entry_id}"
    assert download["data"]["per_patient"].keys() == diagnostics["per_patient"].keys()

    # Not vacuous: the shape is there, with the expected counts.
    assert diagnostics["version"] == "0.1.0"
    assert diagnostics["loaded"] is True
    assert diagnostics["auth_failed"] is False
    assert diagnostics["entry"] == {
        "data_keys": ["email", "password", "patients"],
        "option_keys": [],
        "patient_count": 2,
        "fast_interval_minutes": 5,
    }
    own = diagnostics["per_patient"]["patient_1"]
    assert own["own_account"] is True
    assert own["has_device"] is True
    assert own["fast"] == {
        "last_update_success": True,
        "update_interval_seconds": 300,
        "last_update_age": "under 5 min",
        "newest_record_age": "under 15 min",
        "latest_types": ["basal", "cbg", "cgmSettings", "smbg"],
        "window_sizes": {"basal": 1, "bolus": 2, "deviceEvent": 0, "food": 0},
    }
    assert own["slow"] == {
        "last_update_success": True,
        "update_interval_seconds": 3600,
        "last_update_age": "under 5 min",
        "summary_periods": {"bgm": [], "cgm": ["14d", "1d"]},
        "data_sources": 1,
        "data_sets": 1,
        "profile": True,
        "device_events": ["alarm"],
    }
    assert own["child_devices"] == {"cgm": 1, "pump": 1}

    shared = diagnostics["per_patient"]["patient_2"]
    assert shared["own_account"] is False
    assert shared["fast"]["last_update_success"] is False
    assert shared["fast"]["latest_types"] == []
    assert shared["fast"]["newest_record_age"] is None
    assert shared["slow"]["summary_periods"] == {"bgm": [], "cgm": []}
    assert shared["child_devices"] == {}


async def test_diagnostics_of_sparse_account(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An account without any data, profile or summary still reports its shape."""
    freezer.move_to(NOW)
    entry = password_entry(hass, fast_interval=10)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=[])
    mock_profile(aioclient_mock, full_name=None)
    mock_slow(aioclient_mock, cgm={})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_no_secrets(json.dumps(diagnostics))
    _assert_no_record_times(json.dumps(diagnostics))

    assert diagnostics["entry"]["option_keys"] == ["fast_interval"]
    assert diagnostics["entry"]["fast_interval_minutes"] == 10
    patient = diagnostics["per_patient"]["patient_1"]
    assert patient["fast"]["update_interval_seconds"] == 600
    assert patient["fast"]["latest_types"] == []
    assert patient["fast"]["window_sizes"] == {
        "basal": 0,
        "bolus": 0,
        "deviceEvent": 0,
        "food": 0,
    }
    assert patient["slow"]["data_sources"] == 0
    assert patient["slow"]["profile"] is False
    assert patient["slow"]["device_events"] == []
    assert patient["child_devices"] == {}


async def test_diagnostics_of_auth_failed_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _load(hass, aioclient_mock, freezer)
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock, status=401)
    mock_latest(aioclient_mock, USER_ID, status=401)
    await entry.runtime_data.fast[USER_ID].async_refresh()
    await hass.async_block_till_done()
    assert entry.runtime_data.auth_failed is True

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_no_secrets(json.dumps(diagnostics))
    _assert_no_record_times(json.dumps(diagnostics))

    assert diagnostics["auth_failed"] is True
    own = diagnostics["per_patient"]["patient_1"]
    assert own["fast"]["last_update_success"] is False
    # The data of the last good refresh is still described.
    assert own["fast"]["latest_types"] == ["basal", "cbg", "cgmSettings", "smbg"]


async def test_diagnostics_of_entry_not_loaded(hass: HomeAssistant) -> None:
    entry = password_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    _assert_no_secrets(json.dumps(diagnostics))
    _assert_no_record_times(json.dumps(diagnostics))

    assert diagnostics["loaded"] is False
    assert diagnostics["per_patient"] == {}
    assert diagnostics["entry"]["patient_count"] == 1
