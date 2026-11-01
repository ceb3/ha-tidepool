"""Tests for the patient device and its glucose sensors."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp
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

from .entries import (
    PATIENT_ID,
    USER_ID,
    latest_gets,
    legacy_entry,
    login_posts,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
)

READING = {"type": "cbg", "id": "rec-1", "time": "2026-11-01T12:00:00Z", "value": 5.5}
READING_TIME = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    payload: list[dict] | None = None,
    units: UnitSystem = METRIC_SYSTEM,
    full_name: str | None = "Fake Owner",
    patients: list[str] | None = None,
) -> MockConfigEntry:
    hass.config.units = units
    entry = legacy_entry(hass, patients=patients)
    mock_login(aioclient_mock)
    for patient in patients or [USER_ID]:
        mock_latest(aioclient_mock, user_id=patient, payload=payload)
        mock_profile(aioclient_mock, user_id=patient, full_name=full_name)
        mock_slow(aioclient_mock, patient)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _state(hass: HomeAssistant, key: str, user_id: str = USER_ID) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{user_id}_{key}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_glucose_state_mgdl_for_us_units(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock, [READING], units=US_CUSTOMARY_SYSTEM)

    state = _state(hass, "glucose")
    assert state.state == "99.1"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "mg/dL"
    assert state.attributes["device_class"] == "blood_glucose_concentration"
    assert state.attributes["state_class"] == "measurement"
    assert state.attributes["reading_time"] == "2026-11-01T12:00:00+00:00"
    assert state.attributes["source_units"] == "mmol/L"


async def test_glucose_state_mmol_for_metric_units(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock, [READING], units=METRIC_SYSTEM)

    state = _state(hass, "glucose")
    assert state.state == "5.5"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "mmol/L"
    assert state.attributes["source_units"] == "mmol/L"


async def test_glucose_unknown_when_no_cbg(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock, [])

    glucose = _state(hass, "glucose")
    assert glucose.state == STATE_UNKNOWN
    assert "reading_time" not in glucose.attributes
    assert _state(hass, "glucose_age").state == STATE_UNKNOWN


async def test_glucose_age_minutes(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(READING_TIME + timedelta(minutes=12))
    await _setup(hass, aioclient_mock, [READING])

    state = _state(hass, "glucose_age")
    assert state.state == "12"
    assert state.attributes[ATTR_UNIT_OF_MEASUREMENT] == "min"
    entity = er.async_get(hass).async_get(state.entity_id)
    assert entity is not None
    assert entity.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_glucose_age_ticks_without_new_data(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(READING_TIME + timedelta(minutes=12, seconds=30))
    await _setup(hass, aioclient_mock, [READING])
    assert _state(hass, "glucose_age").state == "12"

    # Less than the 5 minute poll interval: the age moves on its own.
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    assert _state(hass, "glucose_age").state == "13"
    assert latest_gets(aioclient_mock, USER_ID) == 1


async def test_patient_device_registered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock, patients=[USER_ID, PATIENT_ID])

    registry = dr.async_get(hass)
    entry_id = entry.entry_id
    device = registry.async_get_device_by_identifier((DOMAIN, USER_ID), entry_id)
    other = registry.async_get_device_by_identifier((DOMAIN, PATIENT_ID), entry_id)
    assert device is not None
    assert other is not None
    assert device.id != other.id
    assert device.name == "Fake Owner"
    assert device.manufacturer == "Tidepool"
    # One device per patient, each holding both of that patient's sensors.
    for patient, expected in ((USER_ID, device), (PATIENT_ID, other)):
        for key in ("glucose", "glucose_age"):
            entity_id = er.async_get(hass).async_get_entity_id(
                "sensor", DOMAIN, f"{patient}_{key}"
            )
            assert entity_id is not None
            entry = er.async_get(hass).async_get(entity_id)
            assert entry is not None
            assert entry.device_id == expected.id


@pytest.mark.parametrize("full_name", [None, "  "])
async def test_device_name_falls_back_without_profile(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, full_name: str | None
) -> None:
    entry = await _setup(hass, aioclient_mock, full_name=full_name)

    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, USER_ID), entry.entry_id
    )
    assert device is not None
    assert device.name == "Tidepool patient"


async def _setup_with_profiles(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    **second_profile: Any,
) -> MockConfigEntry:
    """Set up two patients; the first has a good profile, the second as given."""
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID)
    mock_profile(aioclient_mock, USER_ID)
    mock_slow(aioclient_mock, USER_ID)
    mock_profile(aioclient_mock, PATIENT_ID, **second_profile)
    mock_slow(aioclient_mock, PATIENT_ID)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _tidepool_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    registry = er.async_get(hass)
    return [
        e.unique_id for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    ]


def _glucose_entities(hass: HomeAssistant, entry: MockConfigEntry) -> list[str]:
    """The glucose sensors only; the summary sensors are in test_summary.py."""
    return [e for e in _tidepool_entities(hass, entry) if "_summary_" not in e]


@pytest.mark.parametrize(
    "profile",
    [{"status": 429}, {"status": 500}, {"exc": aiohttp.ClientConnectionError()}],
)
async def test_profile_failure_retries_setup_without_registering(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, profile: dict[str, Any]
) -> None:
    """A transient profile failure must not bake a fallback name into entity ids.

    The entry goes to SETUP_RETRY and Home Assistant sets it up again later.
    """
    entry = await _setup_with_profiles(hass, aioclient_mock, **profile)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _tidepool_entities(hass, entry) == []

    # Once Tidepool answers, the retry registers both patients by their names.
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    for patient in (USER_ID, PATIENT_ID):
        mock_latest(aioclient_mock, patient)
        mock_profile(aioclient_mock, patient, full_name=f"Fake {patient[-4:]}")
        mock_slow(aioclient_mock, patient)
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=10))
    await hass.async_block_till_done(wait_background_tasks=True)

    assert entry.state is ConfigEntryState.LOADED
    assert len(_glucose_entities(hass, entry)) == 4
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, PATIENT_ID), entry.entry_id
    )
    assert device is not None
    assert device.name == "Fake 0002"


async def test_profile_refused_skips_only_that_patient(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await _setup_with_profiles(hass, aioclient_mock, status=403)

    assert entry.state is ConfigEntryState.LOADED
    assert sorted(_glucose_entities(hass, entry)) == [
        f"{USER_ID}_glucose",
        f"{USER_ID}_glucose_age",
    ]
    assert entry.runtime_data.auth_failed is False
    # The refused request invalidated the shared session, so the next request
    # (the following refresh) signs in again; setup itself needed one login.
    assert login_posts(aioclient_mock) == 1
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("profile" in r.getMessage() for r in warnings)
    for record in caplog.records:
        assert PATIENT_ID not in record.getMessage()


async def test_profile_404_uses_fallback_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup_with_profiles(hass, aioclient_mock, status=404)

    assert entry.state is ConfigEntryState.LOADED
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, PATIENT_ID), entry.entry_id
    )
    assert device is not None
    assert device.name == "Tidepool patient"


async def test_listeners_keep_polling(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The sensors are coordinator listeners, so the fast refresh keeps running."""
    entry = await _setup(hass, aioclient_mock)
    assert entry.runtime_data.fast[USER_ID].last_update_success
    assert latest_gets(aioclient_mock, USER_ID) == 1

    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()

    assert latest_gets(aioclient_mock, USER_ID) == 2
