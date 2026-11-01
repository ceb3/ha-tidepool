"""Tests for the slow coordinator and the summary sensors.

Tidepool reports ``percent`` as a 0-1 fraction (the API spec's example is
0.33), and ``coefficientOfVariation`` as a ratio too. The synthetic fixture
tests/fixtures/summary_cgm.json therefore holds fractions (0.71) and the
integration shows percentages (71.0).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
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
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tidepool.const import API_URL, DOMAIN
from custom_components.tidepool.coordinator import (
    SlowData,
    TidepoolSlowCoordinator,
)
from custom_components.tidepool.models import Summary, parse_summary

from .entries import (
    PATIENT_ID,
    USER_ID,
    latest_gets,
    legacy_entry,
    load_fixture,
    login_posts,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    summary_gets,
)

ENABLED_BY_DEFAULT = {
    "summary_cgm_14d_time_in_range",
    "summary_cgm_14d_gmi",
    "summary_cgm_14d_average",
    "summary_cgm_14d_cv",
    "summary_cgm_1d_time_in_range",
    "summary_cgm_1d_low",
    "summary_cgm_1d_high",
}
SUMMARY_URL = f"{API_URL}/v1/summaries/cgm/{USER_ID}"


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    units: UnitSystem = METRIC_SYSTEM,
    patients: list[str] | None = None,
    **slow: Any,
) -> MockConfigEntry:
    hass.config.units = units
    entry = legacy_entry(hass, patients=patients)
    mock_login(aioclient_mock)
    for patient in patients or [USER_ID]:
        mock_latest(aioclient_mock, user_id=patient)
        mock_profile(aioclient_mock, user_id=patient)
        mock_slow(aioclient_mock, user_id=patient, **slow)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _setup_shared(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, **shared_slow: Any
) -> MockConfigEntry:
    """Set up the own account and one shared patient; only the latter's slow
    endpoints get ``shared_slow``."""
    hass.config.units = METRIC_SYSTEM
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    for patient in (USER_ID, PATIENT_ID):
        mock_latest(aioclient_mock, user_id=patient)
        mock_profile(aioclient_mock, user_id=patient)
    mock_slow(aioclient_mock, user_id=USER_ID)
    mock_slow(aioclient_mock, user_id=PATIENT_ID, **shared_slow)
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
    assert state is not None, "disabled or not yet written"
    return state


def _enable(hass: HomeAssistant, key: str) -> None:
    """Enable a disabled-by-default summary sensor, as a user would."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{USER_ID}_{key}")
    assert entity_id is not None
    registry.async_update_entity(entity_id, disabled_by=None)


def _summary_entries(hass: HomeAssistant, entry: MockConfigEntry) -> list:
    registry = er.async_get(hass)
    return [
        e
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "_summary_" in e.unique_id
    ]


def test_summary_parses_periods() -> None:
    summaries = parse_summary(load_fixture("summary_cgm.json"))

    assert sorted(summaries) == ["14d", "1d"]
    day, fortnight = summaries["1d"], summaries["14d"]
    assert isinstance(fortnight, Summary)
    # Fractions in the payload, percentages in the model.
    assert fortnight.time_in_range == 71.0
    # low and high are all time out of range: they include the deeper bands
    # (inLow 0.04 + inVeryLow 0.01; inHigh 0.2 + inVeryHigh 0.04).
    assert fortnight.low == 5.0
    assert fortnight.very_low == 1.0
    assert fortnight.high == 24.0
    assert fortnight.very_high == 4.0
    assert fortnight.extreme_high == 0.0
    assert fortnight.cv == 36.0
    assert fortnight.gmi == 6.8
    # Glucose stays in mmol/L as delivered; mg/dL is derived.
    assert fortnight.average_mmol == 7.2
    assert fortnight.average_mgdl == 129.7
    assert fortnight.sd_mmol == 2.6
    assert fortnight.min_mmol == 2.8
    assert fortnight.max_mmol == 18.4
    assert fortnight.hours_with_data == 320
    assert fortnight.days_with_data == 14
    assert fortnight.delta is not None
    assert fortnight.delta.time_in_range == -3.0
    assert fortnight.delta.average_mmol == 0.4
    assert fortnight.delta.delta is None
    assert day.time_in_range == 62.0
    assert day.low == 7.0
    assert day.high == 31.0
    assert day.delta is None


@pytest.mark.parametrize(
    "payload", [{}, [], None, {"periods": []}, {"periods": {"1d": 3}}]
)
def test_summary_parse_tolerates_junk(payload: Any) -> None:
    assert parse_summary(payload) == {}


def test_summary_parse_skips_malformed_values() -> None:
    summaries = parse_summary(
        {"periods": {"1d": {"averageGlucoseMmol": "x", "inTarget": {"percent": True}}}}
    )

    assert summaries["1d"].average_mmol is None
    assert summaries["1d"].average_mgdl is None
    assert summaries["1d"].time_in_range is None


async def test_default_enabled_summary_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock)

    summaries = _summary_entries(hass, entry)
    # Four periods, 14 metrics each.
    assert len(summaries) == 56
    enabled = {
        e.unique_id.removeprefix(f"{USER_ID}_") for e in summaries if not e.disabled_by
    }
    assert enabled == ENABLED_BY_DEFAULT
    for registered in summaries:
        if registered.unique_id.removeprefix(f"{USER_ID}_") not in ENABLED_BY_DEFAULT:
            assert registered.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    # Only the enabled ones have a state.
    assert all(
        (hass.states.get(e.entity_id) is None) == bool(e.disabled_by) for e in summaries
    )


async def test_summary_sensor_states_and_attributes(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)

    tir = _state(hass, "summary_cgm_14d_time_in_range")
    assert tir.state == "71.0"
    assert tir.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert tir.attributes["state_class"] == "measurement"
    assert "device_class" not in tir.attributes
    assert _state(hass, "summary_cgm_14d_cv").state == "36.0"
    assert (
        _state(hass, "summary_cgm_14d_cv").attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    )
    gmi = _state(hass, "summary_cgm_14d_gmi")
    assert gmi.state == "6.8"
    assert gmi.attributes[ATTR_UNIT_OF_MEASUREMENT] == "%"
    assert _state(hass, "summary_cgm_1d_time_in_range").state == "62.0"
    assert _state(hass, "summary_cgm_1d_low").state == "7.0"
    assert _state(hass, "summary_cgm_1d_high").state == "31.0"


@pytest.mark.parametrize(
    ("units", "expected", "unit"),
    [(US_CUSTOMARY_SYSTEM, "129.7", "mg/dL"), (METRIC_SYSTEM, "7.2", "mmol/L")],
)
async def test_summary_average_converts_units(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    units: UnitSystem,
    expected: str,
    unit: str,
) -> None:
    await _setup(hass, aioclient_mock, units=units)

    average = _state(hass, "summary_cgm_14d_average")
    assert average.state == expected
    assert average.attributes[ATTR_UNIT_OF_MEASUREMENT] == unit
    assert average.attributes["device_class"] == "blood_glucose_concentration"
    assert average.attributes["state_class"] == "measurement"


async def test_disabled_summary_sensors_work_once_enabled(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock, units=US_CUSTOMARY_SYSTEM)
    for key in ("summary_cgm_14d_max", "summary_cgm_14d_days_with_data"):
        _enable(hass, key)
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(seconds=40))
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    maximum = _state(hass, "summary_cgm_14d_max")
    assert maximum.state == "331.5"  # 18.4 mmol/L
    assert maximum.attributes[ATTR_UNIT_OF_MEASUREMENT] == "mg/dL"
    assert _state(hass, "summary_cgm_14d_days_with_data").state == "14"


async def test_summary_missing_period_is_unknown(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    payload = load_fixture("summary_cgm.json")
    del payload["periods"]["1d"]
    entry = await _setup(hass, aioclient_mock, cgm=payload)

    slow = entry.runtime_data.slow[USER_ID]
    assert slow.data.summary("cgm", "1d") is None
    assert slow.data.summary("cgm", "7d") is None
    assert slow.data.summary("cgm", "14d") is not None
    assert _state(hass, "summary_cgm_1d_time_in_range").state == STATE_UNKNOWN
    assert _state(hass, "summary_cgm_1d_low").state == STATE_UNKNOWN
    assert _state(hass, "summary_cgm_14d_time_in_range").state == "71.0"


async def test_bgm_not_found_is_no_summary(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = await _setup(hass, aioclient_mock, bgm_status=404)

    slow = entry.runtime_data.slow[USER_ID]
    assert slow.last_update_success
    assert slow.last_exception is None
    assert slow.data.summary("bgm", "14d") is None
    assert slow.data.summary("cgm", "14d") is not None
    assert summary_gets(aioclient_mock, USER_ID, "bgm") == 1
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ]


async def test_empty_bgm_summary_is_no_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock, bgm_status=200)

    slow = entry.runtime_data.slow[USER_ID]
    assert slow.last_update_success
    assert slow.data.summary("bgm", "14d") is None


async def test_sparse_account_without_summaries_loads(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    hass.config.units = METRIC_SYSTEM
    entry = legacy_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=[])
    mock_profile(aioclient_mock, full_name=None)  # 404
    aioclient_mock.get(SUMMARY_URL, status=404)
    aioclient_mock.get(f"{API_URL}/v1/summaries/bgm/{USER_ID}", status=404)
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sources", status=404)
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sets", status=404)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    slow = entry.runtime_data.slow[USER_ID]
    assert slow.last_update_success
    assert slow.data.summaries == {"cgm": {}, "bgm": {}}
    assert slow.data.data_sources == []
    assert slow.data.data_sets == []
    assert slow.data.profile == {}
    for key in ENABLED_BY_DEFAULT:
        assert _state(hass, key).state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ]


async def test_empty_cgm_payload_is_no_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock, cgm={})

    assert entry.runtime_data.slow[USER_ID].data.summary("cgm", "14d") is None
    assert _state(hass, "summary_cgm_14d_average").state == STATE_UNKNOWN


@pytest.mark.parametrize("failing", ["data_sources", "data_sets"])
async def test_optional_failure_keeps_summaries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, failing: str
) -> None:
    entry = await _setup(hass, aioclient_mock, **{f"{failing}_status": 500})

    slow = entry.runtime_data.slow[USER_ID]
    assert slow.last_update_success
    assert slow.data.summary("cgm", "14d") is not None
    assert getattr(slow.data, failing) == []
    assert _state(hass, "summary_cgm_14d_time_in_range").state == "71.0"


async def test_slow_profile_is_kept(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock)

    assert entry.runtime_data.slow[USER_ID].data.profile == {"fullName": "Fake Owner"}


async def test_slow_coordinator_per_patient_polls_hourly(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    hass.config.units = METRIC_SYSTEM
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID], fast_interval=2)
    mock_login(aioclient_mock)
    for patient in (USER_ID, PATIENT_ID):
        mock_latest(aioclient_mock, user_id=patient)
        mock_profile(aioclient_mock, user_id=patient)
        mock_slow(aioclient_mock, user_id=patient)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = entry.runtime_data
    assert list(runtime.slow) == [USER_ID, PATIENT_ID]
    for user_id, coordinator in runtime.slow.items():
        assert isinstance(coordinator, TidepoolSlowCoordinator)
        assert isinstance(coordinator.data, SlowData)
        assert coordinator.user_id == user_id
        # The fast interval option does not touch the slow coordinator.
        assert coordinator.update_interval == timedelta(hours=1)
        assert summary_gets(aioclient_mock, user_id) == 1
    assert login_posts(aioclient_mock) == 1

    # Fast refreshes come and go without touching the summaries.
    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(minutes=30))
    await hass.async_block_till_done()
    assert latest_gets(aioclient_mock, USER_ID) > 1
    assert summary_gets(aioclient_mock, USER_ID) == 1

    async_fire_time_changed(hass, datetime.now(UTC) + timedelta(hours=1, seconds=1))
    await hass.async_block_till_done()
    assert summary_gets(aioclient_mock, USER_ID) == 2
    assert summary_gets(aioclient_mock, PATIENT_ID) == 2


async def test_no_dosing_vocabulary_in_summary_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _setup(hass, aioclient_mock)

    for registered in _summary_entries(hass, entry):
        text = f"{registered.unique_id} {registered.original_name}".lower()
        assert "recommended" not in text
        assert "correction" not in text


def _mock_summary_sequence(
    aioclient_mock: AiohttpClientMocker, user_id: str, statuses: list[int]
) -> None:
    """Answer the cgm summary GETs for ``user_id`` with ``statuses`` in turn."""
    remaining = list(statuses)

    async def respond(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        status = remaining.pop(0)
        if status == 200:
            return AiohttpClientMockResponse(
                method, url, json=load_fixture("summary_cgm.json")
            )
        return AiohttpClientMockResponse(method, url, status=status)

    aioclient_mock.get(f"{API_URL}/v1/summaries/cgm/{user_id}", side_effect=respond)


async def _loaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, patients=None
) -> MockConfigEntry:
    entry = await _setup(hass, aioclient_mock, patients=patients)
    aioclient_mock.clear_requests()
    return entry


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == "reauth"
    ]


async def test_expired_session_relogs_in_silently_for_summaries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    mock_login(aioclient_mock)
    _mock_summary_sequence(aioclient_mock, USER_ID, [401, 200])
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]

    await slow.async_refresh()
    await hass.async_block_till_done()

    assert slow.last_update_success
    assert slow.data.summary("cgm", "14d") is not None
    assert entry.runtime_data.auth_failed is False
    assert _reauth_flows(hass) == []
    assert login_posts(aioclient_mock) == 1
    assert summary_gets(aioclient_mock, USER_ID) == 2


async def test_summary_401_twice_triggers_reauth_with_one_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    mock_login(aioclient_mock)
    aioclient_mock.get(SUMMARY_URL, status=401)
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]

    await slow.async_refresh()
    await hass.async_block_till_done()

    assert not slow.last_update_success
    assert entry.runtime_data.auth_failed is True
    assert len(_reauth_flows(hass)) == 1
    assert login_posts(aioclient_mock) == 1  # one silent re-login, no loop
    assert summary_gets(aioclient_mock, USER_ID) == 2

    # The shared flag stops the fast coordinator from calling the API at all.
    aioclient_mock.clear_requests()
    await entry.runtime_data.fast[USER_ID].async_refresh()
    assert aioclient_mock.call_count == 0


async def test_one_relogin_per_slow_refresh(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The cgm summary needed a silent re-login; a refusal of the next request
    in the same refresh asks for reauth instead of signing in again."""
    entry = await _loaded(hass, aioclient_mock)
    mock_login(aioclient_mock)
    _mock_summary_sequence(aioclient_mock, USER_ID, [401, 200])
    aioclient_mock.get(f"{API_URL}/v1/summaries/bgm/{USER_ID}", status=401)
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]

    await slow.async_refresh()
    await hass.async_block_till_done()

    assert not slow.last_update_success
    assert entry.runtime_data.auth_failed is True
    assert len(_reauth_flows(hass)) == 1
    assert login_posts(aioclient_mock) == 1
    assert summary_gets(aioclient_mock, USER_ID, "bgm") == 1


async def test_rejected_login_during_slow_refresh_is_not_retried(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    mock_login(aioclient_mock, status=401)
    _mock_summary_sequence(aioclient_mock, USER_ID, [401])
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]

    await slow.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.auth_failed is True
    assert login_posts(aioclient_mock) == 1
    assert summary_gets(aioclient_mock, USER_ID) == 1


async def test_summary_403_on_other_patient_does_not_reauth_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_URL}/v1/summaries/cgm/{PATIENT_ID}", status=403)
    mock_slow_rest(aioclient_mock, PATIENT_ID)
    mock_slow_rest(aioclient_mock, USER_ID)
    other = entry.runtime_data.slow[PATIENT_ID]
    own = entry.runtime_data.slow[USER_ID]

    await other.async_refresh()
    await own.async_refresh()
    await hass.async_block_till_done()

    assert not other.last_update_success
    assert isinstance(other.last_exception, UpdateFailed)
    assert own.last_update_success
    assert entry.runtime_data.auth_failed is False
    assert _reauth_flows(hass) == []
    assert entry.state is ConfigEntryState.LOADED


async def test_summary_rate_limited_keeps_stale_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    aioclient_mock.get(SUMMARY_URL, status=429, headers={"Retry-After": "7200"})
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]
    before = slow.data

    await slow.async_refresh()

    assert not slow.last_update_success
    assert isinstance(slow.last_exception, UpdateFailed)
    assert slow.data is before
    assert entry.runtime_data.auth_failed is False


def mock_slow_rest(aioclient_mock: AiohttpClientMocker, user_id: str = USER_ID) -> None:
    """Mock the rest of a user's slow endpoints and profile after a custom cgm mock.

    The mock registered first wins, so the caller's cgm mock shadows the one
    ``mock_slow`` adds.
    """
    mock_slow(aioclient_mock, user_id=user_id)
    mock_profile(aioclient_mock, user_id=user_id)


def _gets(aioclient_mock: AiohttpClientMocker, path: str) -> int:
    return sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "get" and url.path == path
    )


async def test_shared_patient_optional_403_degrades_without_extra_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Restricted sharing must not block the entry or cost extra logins."""
    entry = await _setup_shared(hass, aioclient_mock, data_sources_status=403)

    shared = entry.runtime_data.slow[PATIENT_ID]
    assert shared.last_update_success
    assert shared.data.summary("cgm", "14d") is not None
    assert shared.data.data_sources == []
    assert entry.runtime_data.auth_failed is False
    assert _reauth_flows(hass) == []
    assert login_posts(aioclient_mock) == 1
    # No retry, and the other optional requests were skipped for this cycle
    # (the profile was read once, by the setup that names the device).
    assert _gets(aioclient_mock, f"/v1/users/{PATIENT_ID}/data_sources") == 1
    assert _gets(aioclient_mock, f"/v1/users/{PATIENT_ID}/data_sets") == 0
    assert _gets(aioclient_mock, f"/metadata/{PATIENT_ID}/profile") == 1
    # The own account is untouched.
    assert entry.runtime_data.slow[USER_ID].data.summary("cgm", "14d") is not None


async def test_shared_patient_optional_403_keeps_earlier_optional_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    sources = [{"id": "fake-source-1"}]
    aioclient_mock.get(f"{API_URL}/v1/users/{PATIENT_ID}/data_sources", json=sources)
    entry = await _setup_shared(hass, aioclient_mock, data_sets_status=403)

    shared = entry.runtime_data.slow[PATIENT_ID]
    assert shared.last_update_success
    assert shared.data.data_sources == sources
    assert shared.data.data_sets == []
    assert login_posts(aioclient_mock) == 1
    assert _gets(aioclient_mock, f"/metadata/{PATIENT_ID}/profile") == 1


async def test_own_account_optional_403_is_an_auth_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sources", status=403)
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]

    await slow.async_refresh()
    await hass.async_block_till_done()

    assert not slow.last_update_success
    assert entry.runtime_data.auth_failed is True
    assert len(_reauth_flows(hass)) == 1
    assert login_posts(aioclient_mock) == 1  # one silent re-login, no loop
    assert _gets(aioclient_mock, f"/v1/users/{USER_ID}/data_sources") == 2


async def test_optional_rate_limit_keeps_stale_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _loaded(hass, aioclient_mock)
    aioclient_mock.get(
        f"{API_URL}/v1/users/{USER_ID}/data_sets",
        status=429,
        headers={"Retry-After": "7200"},
    )
    mock_slow_rest(aioclient_mock)
    slow = entry.runtime_data.slow[USER_ID]
    before = slow.data

    await slow.async_refresh()

    assert not slow.last_update_success
    assert isinstance(slow.last_exception, UpdateFailed)
    assert slow.data is before
    assert entry.runtime_data.auth_failed is False


async def test_bgm_server_error_leaves_no_bgm_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The bgm summary is optional: a failing server loses only that summary."""
    entry = await _setup(hass, aioclient_mock, bgm_status=500)

    slow = entry.runtime_data.slow[USER_ID]
    assert slow.last_update_success
    assert slow.data.summary("bgm", "14d") is None
    assert slow.data.summary("cgm", "14d") is not None


async def test_bgm_403_on_shared_patient_fails_like_the_cgm_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Summary requests keep the existing rule; only the extras degrade on 403."""
    entry = await _loaded(hass, aioclient_mock, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_URL}/v1/summaries/bgm/{PATIENT_ID}", status=403)
    mock_slow_rest(aioclient_mock, PATIENT_ID)
    shared = entry.runtime_data.slow[PATIENT_ID]

    await shared.async_refresh()

    assert not shared.last_update_success
    assert isinstance(shared.last_exception, UpdateFailed)
    assert entry.runtime_data.auth_failed is False
