"""Tests for the alarm, meal and bolus events and the device event timestamps.

All records are synthetic. An event "fires" when its entity's state (the time
it was triggered) changes; the restored state of a reloaded entity does not
count.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    mock_restore_cache_with_extra_data,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import DOMAIN
from custom_components.tidepool.models import merge_window

from .entries import (
    PATIENT_ID,
    USER_ID,
    device_event_gets,
    latest_url,
    login_posts,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)

ALARM = "event.fake_owner_alarm"
BOLUS = "event.fake_owner_bolus"
MEAL = "event.fake_owner_meal"


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_event(
    record_id: str, when: datetime, sub_type: str, **fields: Any
) -> dict[str, Any]:
    return {
        "type": "deviceEvent",
        "subType": sub_type,
        "id": record_id,
        "time": _iso(when),
        # Present on real records; must never reach an attribute.
        "deviceId": "fake-pump-device-0001",
        "uploadId": "fake-upload-0001",
        **fields,
    }


def _alarm(record_id: str, when: datetime, alarm_type: str = "occlusion") -> dict:
    return _device_event(record_id, when, "alarm", alarmType=alarm_type)


def _bolus(record_id: str, when: datetime, **fields: Any) -> dict[str, Any]:
    return {
        "type": "bolus",
        "id": record_id,
        "time": _iso(when),
        "subType": "normal",
        "normal": 1.0,
        **fields,
    }


def _food(record_id: str, when: datetime, grams: float | None = None) -> dict:
    record: dict[str, Any] = {"type": "food", "id": record_id, "time": _iso(when)}
    if grams is not None:
        record["nutrition"] = {"carbohydrate": {"net": grams, "units": "grams"}}
    return record


def _ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    window: list[dict] | None = None,
    **slow: Any,
) -> MockConfigEntry:
    freezer.move_to(NOW)
    entry = password_entry(hass)
    _mock_all(aioclient_mock, window, **slow)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _mock_all(
    aioclient_mock: AiohttpClientMocker, window: list[dict] | None, **slow: Any
) -> None:
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=[], window=window)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock, **slow)


async def _refresh(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    window: list[dict],
) -> None:
    """Run one fast refresh whose window request returns ``window``."""
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, payload=[], window=window)
    await entry.runtime_data.fast[USER_ID].async_refresh()
    await hass.async_block_till_done()


def _fired(changes: list[Event], entity_id: str, restored: str | None = None) -> list:
    """The attributes of each event fired by ``entity_id``, oldest first.

    Each firing is a new trigger time; an unload (unavailable) and the
    restored trigger time coming back after a reload are not firings.
    """
    fired = []
    states = {STATE_UNKNOWN, STATE_UNAVAILABLE, restored}
    for change in changes:
        new: State | None = change.data["new_state"]
        if change.data["entity_id"] != entity_id or new is None:
            continue
        if new.state not in states:
            states.add(new.state)
            fired.append(dict(new.attributes))
    return fired


def _sensor(hass: HomeAssistant, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{USER_ID}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


# --- events -------------------------------------------------------------


async def test_alarm_event_fires_once_per_record(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    alarm = _alarm("al1", _ago(1), "no_insulin")

    await _refresh(hass, aioclient_mock, entry, [alarm])
    await _refresh(hass, aioclient_mock, entry, [alarm])

    (fired,) = _fired(changes, ALARM)
    assert fired["event_type"] == "no_insulin"
    # The record's own time and type; nothing that identifies a device.
    assert {k: fired[k] for k in ("alarm_type", "time")} == {
        "alarm_type": "no_insulin",
        "time": "2026-11-01T11:00:00+00:00",
    }
    assert not {"deviceId", "uploadId", "id", "device_id"} & fired.keys()
    entity = er.async_get(hass).async_get(ALARM)
    assert entity is not None
    assert entity.unique_id == f"{USER_ID}_alarm"


async def test_unlisted_alarm_type_fires_as_other(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    await _refresh(hass, aioclient_mock, entry, [_alarm("al1", _ago(1), "beeping")])

    (fired,) = _fired(changes, ALARM)
    assert fired["event_type"] == "other"
    assert fired["alarm_type"] == "other"


async def test_first_load_does_not_fire_history(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    window = [
        _alarm("al1", _ago(3)),
        _bolus("bo1", _ago(2)),
        _food("f1", _ago(2), 30),
    ]
    entry = await _setup(hass, aioclient_mock, freezer, window=window)
    await _refresh(hass, aioclient_mock, entry, window)

    for entity_id in (ALARM, BOLUS, MEAL):
        assert _fired(changes, entity_id) == [], entity_id
        assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_bolus_and_meal_events(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    window = [
        _bolus(
            "bo1",
            _ago(2),
            subType="dual/square",
            normal=1.5,
            extended=1.0,
            expectedExtended=2.0,
            deliveryContext="device",
        ),
        _bolus("bo2", _ago(1), subType="automated", normal=0.25),
        _food("f1", _ago(2), 45),
        _food("f2", _ago(1)),
    ]

    await _refresh(hass, aioclient_mock, entry, window)

    assert _fired(changes, BOLUS) == [
        {
            "event_type": "dual_square",
            "event_types": ["normal", "square", "dual_square", "automated", "other"],
            "friendly_name": "Fake Owner Bolus",
            # Delivered only: the programmed but undelivered part is ignored.
            "units": 2.5,
            "time": "2026-11-01T10:00:00+00:00",
            "context": "device",
        },
        {
            "event_type": "automated",
            "event_types": ["normal", "square", "dual_square", "automated", "other"],
            "friendly_name": "Fake Owner Bolus",
            "units": 0.25,
            "time": "2026-11-01T11:00:00+00:00",
            "context": None,
        },
    ]
    meals = _fired(changes, MEAL)
    assert [m["event_type"] for m in meals] == ["meal", "meal"]
    assert meals[0]["carbohydrates"] == 45.0
    assert meals[0]["time"] == "2026-11-01T10:00:00+00:00"
    assert "carbohydrates" not in meals[1]


async def test_late_old_record_fires_once(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A record uploaded late is new to us: it fires once, with its own time."""
    newer = _bolus("bo2", _ago(1))
    entry = await _setup(hass, aioclient_mock, freezer, window=[newer])
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    late = _bolus("bo1", _ago(20), normal=3.0)

    await _refresh(hass, aioclient_mock, entry, [late, newer])
    await _refresh(hass, aioclient_mock, entry, [late, newer])

    (fired,) = _fired(changes, BOLUS)
    assert fired["units"] == 3.0
    assert fired["time"] == "2026-10-31T16:00:00+00:00"


async def test_burst_fires_only_the_five_newest(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    # Seven boluses uploaded at once after a long gap, out of time order.
    burst = [_bolus(f"bo{i}", _ago(i), normal=float(i)) for i in (3, 7, 1, 5, 2, 6, 4)]

    await _refresh(hass, aioclient_mock, entry, burst)
    await _refresh(hass, aioclient_mock, entry, burst)

    # The five newest, oldest first; the two oldest are marked seen.
    assert [f["units"] for f in _fired(changes, BOLUS)] == [5.0, 4.0, 3.0, 2.0, 1.0]


async def test_restart_restores_seen_ids(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Records seen before a restart stay seen; one that arrived since fires."""
    restored = "2026-11-01T09:00:00.000+00:00"
    seen = _alarm("al1", _ago(4))
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(ALARM, restored, {"event_type": "occlusion"}),
                {
                    "last_event_type": "occlusion",
                    "last_event_attributes": {"alarm_type": "occlusion"},
                    "seen": {"al1": "2026-11-01T08:00:00+00:00"},
                },
            )
        ],
    )
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    window = [seen, _alarm("al2", _ago(2), "low_power")]

    entry = await _setup(hass, aioclient_mock, freezer, window=window)
    await _refresh(hass, aioclient_mock, entry, window)

    (fired,) = _fired(changes, ALARM, restored)
    assert fired["event_type"] == "low_power"


async def test_reload_does_not_replay(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A reload restores the seen ids: nothing is replayed, and a record that
    arrived while the entry was unloaded still fires (a fresh baseline would
    swallow it)."""
    entry = await _setup(hass, aioclient_mock, freezer, window=[_alarm("al1", _ago(3))])
    window = [_alarm("al1", _ago(3)), _alarm("al2", _ago(2))]
    await _refresh(hass, aioclient_mock, entry, window)
    fired_state = hass.states.get(ALARM).state
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    aioclient_mock.clear_requests()
    window = [*window, _alarm("al3", _ago(1), "low_power")]
    _mock_all(aioclient_mock, window)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await _refresh(hass, aioclient_mock, entry, window)

    assert [f["event_type"] for f in _fired(changes, ALARM, fired_state)] == [
        "low_power"
    ]


async def test_events_on_pump_device(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    bolus = _bolus("bo1", _ago(1), deviceId="fake-pump-device-0001")
    mock_latest(aioclient_mock, payload=[bolus])
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    devices = {
        registry.async_get(entity_id).device_id for entity_id in (ALARM, BOLUS, MEAL)
    }
    pump = registry.async_get(
        registry.async_get_entity_id("sensor", DOMAIN, f"{USER_ID}_basal_rate")
    )
    assert devices == {pump.device_id}


# --- the window and the slow requests ------------------------------------


def test_window_keeps_device_events_for_24_hours() -> None:
    kept = _alarm("al1", NOW - timedelta(hours=23, minutes=59))
    dropped = _alarm("al0", NOW - timedelta(hours=24, minutes=1))

    window = merge_window({}, [kept, dropped], NOW)

    assert window["deviceEvent"] == [kept]


async def test_slow_device_event_requests(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)

    requests = [dict(url.query) for url in device_event_gets(aioclient_mock, USER_ID)]
    # The newest of each subType; a prime over 30 days, newest per target.
    assert requests == [
        {"latest": "true", "type": "deviceEvent", "subType": "alarm"},
        {"latest": "true", "type": "deviceEvent", "subType": "reservoirChange"},
        {"latest": "true", "type": "deviceEvent", "subType": "status"},
        {
            "type": "deviceEvent",
            "subType": "prime",
            "startDate": "2026-10-02T12:00:00.000Z",
            "endDate": "2026-11-01T12:00:00.000Z",
        },
    ]


# --- timestamp sensors ----------------------------------------------------


async def test_reservoir_change_timestamp(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    events = {
        "reservoirChange": [_device_event("rc1", _ago(50), "reservoirChange")],
        "alarm": [_alarm("al1", _ago(30), "low_insulin")],
        "status": [
            _device_event(
                "st1",
                _ago(5),
                "status",
                status="suspended",
                reason={"suspended": "manual"},
            )
        ],
    }
    await _setup(hass, aioclient_mock, freezer, device_events=events)

    reservoir = _sensor(hass, "last_reservoir_change")
    assert reservoir.state == "2026-10-30T10:00:00+00:00"
    assert reservoir.attributes["device_class"] == "timestamp"
    entity = er.async_get(hass).async_get(reservoir.entity_id)
    assert entity.entity_category == "diagnostic"
    alarm = _sensor(hass, "last_alarm")
    assert alarm.state == "2026-10-31T06:00:00+00:00"
    assert alarm.attributes["alarm_type"] == "low_insulin"
    status = _sensor(hass, "last_status_change")
    assert status.state == "2026-11-01T07:00:00+00:00"
    assert status.attributes["status"] == "suspended"
    assert status.attributes["reason"] == "manual"


async def test_site_change_from_prime_event(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    primes = [
        _device_event("p1", _ago(80), "prime", primeTarget="cannula", volume=0.3),
        _device_event("p2", _ago(30), "prime", primeTarget="tubing", volume=10),
        _device_event("p3", _ago(29), "prime", primeTarget="cannula", volume=0.3),
        # The newest prime is a tubing prime: the site change stays at p3.
        _device_event("p4", _ago(2), "prime", primeTarget="tubing", volume=5),
    ]
    await _setup(hass, aioclient_mock, freezer, device_events={"prime": primes})

    site = _sensor(hass, "last_site_change")
    assert site.state == "2026-10-31T07:00:00+00:00"
    assert site.attributes["tubing"] == "2026-11-01T10:00:00+00:00"
    tubing = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{USER_ID}_last_tubing_prime"
    )
    entity = er.async_get(hass).async_get(tubing)
    assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION


DEVICE_EVENT_SENSORS = (
    "last_alarm",
    "last_reservoir_change",
    "last_site_change",
    "last_status_change",
)


async def test_sparse_account_shows_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, aioclient_mock, freezer)

    for key in DEVICE_EVENT_SENSORS:
        state = _sensor(hass, key)
        assert state.state == STATE_UNKNOWN, key
        assert state.attributes.get("tubing") is None
    for entity_id in (ALARM, BOLUS, MEAL):
        assert hass.states.get(entity_id).state == STATE_UNKNOWN
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING
        and (r.levelno >= logging.ERROR or r.name.startswith("custom_components"))
    ]


async def test_shared_patient_withholding_device_events_degrades(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A 403 skips the shared patient's other device event requests, costs no
    login, and leaves the rest of the slow data and the entry loaded."""
    freezer.move_to(NOW)
    entry = password_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    for patient in (USER_ID, PATIENT_ID):
        mock_latest(aioclient_mock, user_id=patient, payload=[])
        mock_profile(aioclient_mock, user_id=patient)
    mock_slow(aioclient_mock, user_id=USER_ID)
    mock_slow(aioclient_mock, user_id=PATIENT_ID, device_events_status=403)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    shared = entry.runtime_data.slow[PATIENT_ID]
    assert shared.last_update_success
    assert shared.data.device_events == {}
    assert shared.data.summary("cgm", "14d") is not None
    assert len(device_event_gets(aioclient_mock, PATIENT_ID)) == 1
    assert login_posts(aioclient_mock) == 1
    assert entry.runtime_data.auth_failed is False


# --- a failed device event request keeps its previous record (R20) --------


def _fail_device_event_request(
    aioclient_mock: AiohttpClientMocker, sub_type: str, failure: dict[str, Any]
) -> None:
    """Answer one subType's device event request with ``failure``.

    Registered after ``mock_slow``, so it is moved in front of its mocks.
    """
    query: dict[str, str] = {"type": "deviceEvent", "subType": sub_type}
    if sub_type != "prime":
        query["latest"] = "true"
    aioclient_mock.get(latest_url(USER_ID), params=query, **failure)
    mocks = aioclient_mock._mocks
    mocks.insert(0, mocks.pop())


@pytest.mark.parametrize("failure", [{"status": 500}, {"exc": TimeoutError()}])
@pytest.mark.parametrize(
    ("failing", "kept"),
    [("reservoirChange", "last_reservoir_change"), ("prime", "last_site_change")],
)
async def test_failed_device_event_request_keeps_its_timestamp(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    failure: dict[str, Any],
    failing: str,
    kept: str,
) -> None:
    first = {
        "reservoirChange": [_device_event("rc1", _ago(50), "reservoirChange")],
        "alarm": [_alarm("al1", _ago(30))],
        "prime": [_device_event("p1", _ago(49), "prime", primeTarget="cannula")],
    }
    entry = await _setup(hass, aioclient_mock, freezer, device_events=first)
    slow = entry.runtime_data.slow[USER_ID]
    before = _sensor(hass, kept).state
    assert before != STATE_UNKNOWN

    # The next hour: a newer alarm, while one request fails.
    later = {
        "reservoirChange": [_device_event("rc2", _ago(1), "reservoirChange")],
        "alarm": [_alarm("al2", _ago(2))],
        "prime": [_device_event("p2", _ago(1), "prime", primeTarget="cannula")],
    }
    aioclient_mock.clear_requests()
    mock_slow(aioclient_mock, device_events=later)
    mock_profile(aioclient_mock)
    _fail_device_event_request(aioclient_mock, failing, failure)
    await slow.async_refresh()
    await hass.async_block_till_done()

    assert slow.last_update_success
    assert _sensor(hass, kept).state == before
    assert _sensor(hass, "last_alarm").state == _ago(2).isoformat()
