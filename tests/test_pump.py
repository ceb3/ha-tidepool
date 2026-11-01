"""Tests for the pump, insulin, carbs and dosing-context entities.

All records are synthetic. Expected insulin totals are worked out by hand in
the comments next to each case; rates and durations are chosen so that the
float arithmetic is exact.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tidepool.api import RateLimitedError
from custom_components.tidepool.const import DOMAIN
from custom_components.tidepool.models import basal_units

from .entries import (
    LATEST_QUERY,
    USER_ID,
    data_gets,
    latest_gets,
    latest_url,
    login_posts,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
    window_gets,
)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
DAY = timedelta(hours=24)
HOUR_MS = 3_600_000

COMPONENT = Path(__file__).parent.parent / "custom_components" / DOMAIN


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _basal(
    record_id: str,
    start: datetime,
    hours: float,
    delivery: str,
    rate: float | None = None,
) -> dict[str, Any]:
    record = {
        "type": "basal",
        "id": record_id,
        "time": _iso(start),
        "deliveryType": delivery,
        "duration": int(hours * HOUR_MS),
    }
    if rate is not None:
        record["rate"] = rate
    return record


def _bolus(record_id: str, when: datetime, **fields: Any) -> dict[str, Any]:
    return {"type": "bolus", "id": record_id, "time": _iso(when), **fields}


def _food(record_id: str, when: datetime, **fields: Any) -> dict[str, Any]:
    return {"type": "food", "id": record_id, "time": _iso(when), **fields}


def _carbs(grams: float) -> dict[str, Any]:
    return {"carbohydrate": {"net": grams, "units": "grams"}}


# --- basal_units ---------------------------------------------------------


def test_basal_24h_clips_overlap_and_skips_suspend() -> None:
    segments = [
        # Runs into the next one: only 10 h..8 h ago counts, 2 h x 1.0 = 2.0 U.
        _basal("a", NOW - timedelta(hours=10), 4, "scheduled", 1.0),
        # Not in time order on purpose. Still running: 30 min x 2.0 = 1.0 U.
        _basal("e", NOW - timedelta(minutes=30), 1, "automated", 2.0),
        # 2 h x 0.5 = 1.0 U.
        _basal("b", NOW - timedelta(hours=8), 2, "temp", 0.5),
        # A suspend has no rate and delivers nothing.
        _basal("c", NOW - timedelta(hours=6), 1, "suspend"),
        # 2 h x 1.25 = 2.5 U.
        _basal("d", NOW - timedelta(hours=5), 2, "automated", 1.25),
    ]

    assert basal_units(segments, NOW) == 6.5


def test_basal_24h_window_edges() -> None:
    start = NOW - DAY
    segments = [
        # Over long before the window opens.
        _basal("a", start - timedelta(hours=6), 1, "scheduled", 4.0),
        # Ends exactly when the window opens.
        _basal("b", start - timedelta(hours=2), 2, "scheduled", 4.0),
        # Straddles the window start: 1 h of its 2 h is inside, 1 h x 0.5.
        _basal("c", start - timedelta(hours=1), 2, "scheduled", 0.5),
    ]

    assert basal_units(segments, NOW) == 0.5
    # A shorter window clips the same records too.
    late = _basal("d", NOW - timedelta(hours=3), 3, "scheduled", 1.0)
    assert basal_units([late], NOW, timedelta(hours=1)) == 1.0


def test_basal_24h_ignores_malformed_segments() -> None:
    segments = [
        {"type": "basal", "id": "x", "time": "not a time", "rate": 1.0},
        {"type": "basal", "id": "y", "time": _iso(NOW), "rate": 1.0},
        _basal("z", NOW - timedelta(hours=1), 1, "scheduled", 1.0),
    ]

    assert basal_units(segments, NOW) == 1.0


def test_basal_24h_suspend_clips_the_segment_before_it() -> None:
    segments = [
        # Cut short by the suspend 2 h in: 2 h x 1.0 = 2.0 U.
        _basal("a", NOW - timedelta(hours=4), 3, "scheduled", 1.0),
        _basal("b", NOW - timedelta(hours=2), 1, "suspend"),
    ]

    assert basal_units(segments, NOW) == 2.0


@pytest.mark.parametrize("duration", [-1, None])
def test_basal_24h_skips_a_middle_segment_without_duration(
    duration: int | None,
) -> None:
    middle = _basal("b", NOW - timedelta(hours=4), 0, "temp", 5.0)
    if duration is None:
        del middle["duration"]
    else:
        middle["duration"] = duration
    segments = [
        # A new segment did start 2 h in, so it still ends this one: 2.0 U.
        _basal("a", NOW - timedelta(hours=6), 4, "scheduled", 1.0),
        # Unusable duration: delivers nothing.
        middle,
        # 1 h x 1.0 = 1.0 U.
        _basal("c", NOW - timedelta(hours=2), 1, "scheduled", 1.0),
    ]

    assert basal_units(segments, NOW) == 3.0


def test_basal_24h_ignores_future_segments() -> None:
    segments = [
        # Clipped to now: 1 h x 1.0 = 1.0 U.
        _basal("a", NOW - timedelta(hours=1), 3, "scheduled", 1.0),
        _basal("b", NOW + timedelta(hours=1), 1, "scheduled", 9.0),
    ]

    assert basal_units(segments, NOW) == 1.0


# --- entities ------------------------------------------------------------


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    latest: list[dict] | None = None,
    window: list[dict] | None = None,
) -> MockConfigEntry:
    freezer.move_to(NOW)
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=latest or [], window=window)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _state(hass: HomeAssistant, key: str, platform: str = "sensor") -> State:
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{USER_ID}_{key}"
    )
    assert entity_id is not None, key
    state = hass.states.get(entity_id)
    assert state is not None, key
    return state


async def test_basal_mode_enum_values(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    current = _basal("b1", NOW - timedelta(minutes=20), 0.5, "temp", 0.45)
    await _setup(hass, aioclient_mock, freezer, latest=[current])

    mode = _state(hass, "basal_mode")
    assert mode.state == "temp"
    assert mode.attributes["device_class"] == "enum"
    assert mode.attributes["options"] == ["automated", "scheduled", "suspend", "temp"]
    rate = _state(hass, "basal_rate")
    assert rate.state == "0.45"
    assert rate.attributes[ATTR_UNIT_OF_MEASUREMENT] == "U/h"
    assert rate.attributes["state_class"] == "measurement"
    assert rate.attributes["delivery"] == "temp"


async def test_basal_mode_unknown_for_unlisted_delivery_type(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    current = _basal("b1", NOW - timedelta(minutes=20), 0.5, "something-new", 0.5)
    await _setup(hass, aioclient_mock, freezer, latest=[current])

    assert _state(hass, "basal_mode").state == STATE_UNKNOWN
    assert _state(hass, "basal_rate").state == "0.5"


@pytest.mark.parametrize(
    ("delivery", "rate", "automated", "suspended"),
    [
        ("automated", 0.7, "on", "off"),
        ("suspend", None, "off", "on"),
        ("scheduled", 0.7, "off", "off"),
        ("temp", 0.0, "off", "off"),
    ],
)
async def test_automated_and_suspended_binary_sensors(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    delivery: str,
    rate: float | None,
    automated: str,
    suspended: str,
) -> None:
    current = _basal("b1", NOW - timedelta(minutes=10), 0.5, delivery, rate)
    await _setup(hass, aioclient_mock, freezer, latest=[current])

    assert _state(hass, "automated_delivery_active", "binary_sensor").state == automated
    assert _state(hass, "delivery_suspended", "binary_sensor").state == suspended
    if rate is None:
        # A suspend has no rate: the rate is unknown, not a made-up zero.
        assert _state(hass, "basal_rate").state == STATE_UNKNOWN


async def test_bolus_total_and_last_bolus(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    newest = _bolus(
        "bo2",
        NOW - timedelta(hours=1),
        subType="dual/square",
        deliveryContext="device",
        normal=1.0,
        extended=0.5,
        # Programmed, not delivered: must not count.
        expectedNormal=3.0,
    )
    window = [
        _bolus("bo1", NOW - timedelta(hours=2), subType="normal", normal=2.0),
        newest,
        # Older than 24 h: outside the window.
        _bolus("bo0", NOW - timedelta(hours=30), subType="automated", normal=4.0),
        # 2 h x 1.0 = 2.0 U of basal.
        _basal("ba1", NOW - timedelta(hours=3), 2, "scheduled", 1.0),
    ]
    await _setup(hass, aioclient_mock, freezer, latest=[newest], window=window)

    last = _state(hass, "last_bolus")
    assert last.state == "1.5"
    assert last.attributes[ATTR_UNIT_OF_MEASUREMENT] == "U"
    assert last.attributes["time"] == "2026-11-01T11:00:00+00:00"
    assert last.attributes["sub_type"] == "dual/square"
    assert last.attributes["context"] == "device"
    # 2.0 + (1.0 + 0.5) = 3.5 U of bolus; plus 2.0 U of basal.
    assert _state(hass, "insulin_bolus_24h").state == "3.5"
    assert _state(hass, "insulin_basal_24h").state == "2.0"
    total = _state(hass, "insulin_total_24h")
    assert total.state == "5.5"
    assert total.attributes[ATTR_UNIT_OF_MEASUREMENT] == "U"
    # A rolling total goes down as well as up: not total_increasing.
    assert total.attributes["state_class"] == "measurement"
    assert "device_class" not in total.attributes


async def test_carbs_sum_ignores_missing_nutrition(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    newest = _food("f4", NOW - timedelta(minutes=30), nutrition={"energy": {}})
    window = [
        _food("f1", NOW - timedelta(hours=5), nutrition=_carbs(30)),
        _food("f2", NOW - timedelta(hours=3)),
        _food("f3", NOW - timedelta(hours=1), nutrition=_carbs(12.5)),
        newest,
        _food("f0", NOW - timedelta(hours=25), nutrition=_carbs(80)),
    ]
    await _setup(hass, aioclient_mock, freezer, latest=[newest], window=window)

    total = _state(hass, "carbs_24h")
    assert total.state == "42.5"
    assert total.attributes[ATTR_UNIT_OF_MEASUREMENT] == "g"
    assert total.attributes["state_class"] == "measurement"
    last = _state(hass, "carbs_last")
    assert last.state == "12.5"
    assert last.attributes["time"] == "2026-11-01T11:00:00+00:00"


async def test_iob_labelled_as_of(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    decision = {
        "type": "dosingDecision",
        "id": "dd1",
        "time": _iso(NOW - timedelta(minutes=10)),
        "reason": "loop",
        "insulinOnBoard": {"amount": 1.75},
        "carbsOnBoard": {"amount": 20},
    }
    entry = await _setup(hass, aioclient_mock, freezer, latest=[decision])

    for key, value in (
        ("iob_at_last_decision", "1.75"),
        ("cob_at_last_decision", "20.0"),
    ):
        state = _state(hass, key)
        assert state.state == value
        assert state.attributes["as_of"] == "2026-11-01T11:50:00+00:00"
        assert state.attributes["note"] == (
            "As of the last dosing decision; not a live value."
        )
        assert "state_class" not in state.attributes
    for registered in er.async_entries_for_config_entry(
        er.async_get(hass), entry.entry_id
    ):
        text = " ".join(
            str(part)
            for part in (
                registered.unique_id,
                registered.translation_key,
                registered.original_name,
                hass.states.get(registered.entity_id),
            )
        ).lower()
        assert "recommended" not in text
        assert "correction" not in text


async def test_iob_from_bolus_calculator_without_decision(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Without a dosing decision, the bolus calculator's IOB stands in."""
    wizard = {
        "type": "wizard",
        "id": "w1",
        "time": _iso(NOW - timedelta(hours=2)),
        "insulinOnBoard": 0.8,
        "carbInput": 40,
    }
    older = {
        "type": "dosingDecision",
        "id": "dd0",
        "time": _iso(NOW - timedelta(hours=3)),
        "insulinOnBoard": {"amount": 2.0},
        "carbsOnBoard": {"amount": 15},
    }
    await _setup(hass, aioclient_mock, freezer, latest=[wizard, older])

    iob = _state(hass, "iob_at_last_decision")
    assert iob.state == "0.8"
    assert iob.attributes["as_of"] == "2026-11-01T10:00:00+00:00"
    # The calculator has no carbs on board, so the decision before it stands.
    cob = _state(hass, "cob_at_last_decision")
    assert cob.state == "15.0"
    assert cob.attributes["as_of"] == "2026-11-01T09:00:00+00:00"


async def test_last_injection_and_last_activity(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    latest = [
        {
            "type": "insulin",
            "id": "i1",
            "time": _iso(NOW - timedelta(hours=4)),
            "dose": {"total": 6.0, "units": "Units"},
        },
        {
            "type": "physicalActivity",
            "id": "p1",
            "time": _iso(NOW - timedelta(hours=6)),
        },
    ]
    await _setup(hass, aioclient_mock, freezer, latest=latest)

    injection = _state(hass, "last_injection")
    assert injection.state == "2026-11-01T08:00:00+00:00"
    assert injection.attributes["device_class"] == "timestamp"
    assert injection.attributes["dose"] == 6.0
    activity = _state(hass, "last_activity")
    assert activity.state == "2026-11-01T06:00:00+00:00"
    assert activity.attributes["device_class"] == "timestamp"


BASAL_ENTITIES = (
    ("sensor", "basal_rate"),
    ("sensor", "basal_mode"),
    ("binary_sensor", "automated_delivery_active"),
    ("binary_sensor", "delivery_suspended"),
)


@pytest.mark.parametrize(
    ("start", "hours", "current"),
    [
        # Running now.
        (NOW - timedelta(minutes=10), 0.5, True),
        # Ended 2 h ago: just inside the 2 h grace.
        (NOW - timedelta(hours=3), 1, True),
        # Ended 2 h and 1 s ago: stale.
        (NOW - timedelta(hours=3, seconds=1), 1, False),
        # No duration: ends at its time, so stale 2 h after it started.
        (NOW - timedelta(hours=1), None, True),
        (NOW - timedelta(hours=2, seconds=1), None, False),
    ],
)
async def test_stale_basal_reads_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    start: datetime,
    hours: float | None,
    current: bool,
) -> None:
    basal = _basal("b1", start, hours or 0, "automated", 0.6)
    if hours is None:
        del basal["duration"]
    await _setup(hass, aioclient_mock, freezer, latest=[basal])

    ends = start + timedelta(hours=hours or 0)
    expected = ("0.6", "automated", "on", "off")
    for (platform, key), value in zip(BASAL_ENTITIES, expected, strict=True):
        state = _state(hass, key, platform)
        assert state.state == (value if current else STATE_UNKNOWN), key
        # The times stay visible either way, so a stale value can be explained.
        assert state.attributes["time"] == start.isoformat(), key
        assert state.attributes["ends"] == ends.isoformat(), key


PUMP_SENSORS = (
    "basal_rate",
    "basal_mode",
    "last_bolus",
    "insulin_basal_24h",
    "insulin_bolus_24h",
    "insulin_total_24h",
    "carbs_last",
    "carbs_24h",
    "last_injection",
    "last_activity",
    "iob_at_last_decision",
    "cob_at_last_decision",
)
PUMP_BINARY_SENSORS = ("automated_delivery_active", "delivery_suspended")


async def test_account_without_pump_shows_unknown(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await _setup(hass, aioclient_mock, freezer)

    for key in PUMP_SENSORS:
        state = _state(hass, key)
        # Nothing delivered or eaten in 24 h is not the same as no data.
        assert state.state == STATE_UNKNOWN, key
        assert "as_of" not in state.attributes
    for key in PUMP_BINARY_SENSORS:
        assert _state(hass, key, "binary_sensor").state == STATE_UNKNOWN
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [
        r
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name.startswith("custom_components")
    ]


async def test_entity_names_avoid_dosing_vocabulary() -> None:
    strings = json.loads((COMPONENT / "strings.json").read_text())
    english = json.loads((COMPONENT / "translations" / "en.json").read_text())
    assert strings == english
    names = [
        (platform, key, entity["name"])
        for platform, entities in strings["entity"].items()
        for key, entity in entities.items()
    ]
    for platform in ("sensor", "binary_sensor", "event"):
        assert any(p == platform for p, _, _ in names)
    for _, key, name in names:
        for word in ("recommended", "correction"):
            assert word not in key.lower()
            assert word not in name.lower()


# --- the 24 h window ------------------------------------------------------


def _window_urls(aioclient_mock: AiohttpClientMocker) -> list[URL]:
    """The window request URLs; the slow device event requests name a subType."""
    return [
        url
        for url in data_gets(aioclient_mock, USER_ID)
        if "type" in url.query and "subType" not in url.query
    ]


async def test_window_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)

    windows = _window_urls(aioclient_mock)
    assert len(windows) == 1
    # One request for all four types, at the same refresh as the latest.
    assert windows[0].query["type"] == "basal,bolus,food,deviceEvent"
    # 24 h plus a 12 h lookback for the basal segment running into the window.
    assert windows[0].query["startDate"] == "2026-10-31T00:00:00.000Z"
    assert windows[0].query["endDate"] == "2026-11-01T12:00:00.000Z"
    assert latest_gets(aioclient_mock, USER_ID) == 1


async def test_window_cold_start_counts_straddling_basal(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """After a restart there is no previous window: the lookback must bring
    the segment that began before the 24 h and still runs into it."""
    # 30 h..22 h ago at 1.0 U/h: 2 h of it inside the window.
    straddling = _basal("ba1", NOW - DAY - timedelta(hours=6), 8, "scheduled", 1.0)
    await _setup(hass, aioclient_mock, freezer, window=[straddling])

    (window,) = _window_urls(aioclient_mock)
    assert window.query["startDate"] == "2026-10-31T00:00:00.000Z"
    assert _state(hass, "insulin_basal_24h").state == "2.0"


async def test_window_deduplicates_by_id(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    bolus = _bolus("bo1", NOW - timedelta(hours=2), subType="normal", normal=2.0)
    food = _food("f1", NOW - timedelta(hours=2), nutrition=_carbs(30))
    # The latest records repeat the newest window records; the window itself
    # holds a duplicate.
    entry = await _setup(
        hass,
        aioclient_mock,
        freezer,
        latest=[bolus, food],
        window=[bolus, dict(bolus), food],
    )
    coordinator = entry.runtime_data.fast[USER_ID]
    assert [r["id"] for r in coordinator.data.window["bolus"]] == ["bo1"]
    assert _state(hass, "insulin_bolus_24h").state == "2.0"
    assert _state(hass, "carbs_24h").state == "30.0"

    # The next refresh brings the same records again plus a new one.
    aioclient_mock.clear_requests()
    later = _bolus("bo2", NOW - timedelta(hours=1), subType="normal", normal=1.0)
    mock_latest(aioclient_mock, payload=[later], window=[bolus, later, food])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert [r["id"] for r in coordinator.data.window["bolus"]] == ["bo1", "bo2"]
    assert _state(hass, "insulin_bolus_24h").state == "3.0"
    assert _state(hass, "carbs_24h").state == "30.0"


async def test_window_prunes_to_24_hours(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    window = [
        _bolus("bo1", NOW - timedelta(hours=23), subType="normal", normal=2.0),
        # 23 h..21 h ago at 1.0 U/h.
        _basal("ba1", NOW - timedelta(hours=23), 2, "scheduled", 1.0),
        _food("f1", NOW - timedelta(hours=1), nutrition=_carbs(10)),
    ]
    entry = await _setup(hass, aioclient_mock, freezer, window=window)
    coordinator = entry.runtime_data.fast[USER_ID]
    assert _state(hass, "insulin_basal_24h").state == "2.0"

    # Two hours on, Tidepool no longer returns the bolus or the basal (they
    # started before the new window start); the bolus is now older than 24 h
    # but the basal segment still runs 1 h into the window.
    freezer.tick(timedelta(hours=2))
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, payload=[], window=[window[2]])
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data.window["bolus"] == []
    assert [r["id"] for r in coordinator.data.window["basal"]] == ["ba1"]
    assert [r["id"] for r in coordinator.data.window["food"]] == ["f1"]
    assert _state(hass, "insulin_bolus_24h").state == "0.0"
    assert _state(hass, "insulin_basal_24h").state == "1.0"


async def test_window_failure_keeps_latest_and_previous_window(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    bolus = _bolus("bo1", NOW - timedelta(hours=2), subType="normal", normal=2.0)
    entry = await _setup(hass, aioclient_mock, freezer, window=[bolus])
    coordinator = entry.runtime_data.fast[USER_ID]

    aioclient_mock.clear_requests()
    reading = {"type": "cbg", "id": "c9", "time": _iso(NOW), "value": 6.2}
    mock_latest(aioclient_mock, payload=[reading], window_status=500)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert coordinator.data.latest.cbg is not None
    assert coordinator.data.latest.cbg.id == "c9"
    assert [r["id"] for r in coordinator.data.window["bolus"]] == ["bo1"]
    assert _state(hass, "insulin_bolus_24h").state == "2.0"
    assert window_gets(aioclient_mock, USER_ID) == 1


async def test_window_rate_limited_fails_the_refresh(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    coordinator = entry.runtime_data.fast[USER_ID]
    previous = coordinator.data

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, window_status=429)
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    assert isinstance(coordinator.last_exception.__cause__, RateLimitedError)
    assert coordinator.data is previous


async def test_window_refused_after_relogin_needs_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    coordinator = entry.runtime_data.fast[USER_ID]

    # The latest request works but the window request keeps being refused:
    # one silent re-login, then the entry asks for reauth.
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, window_status=401)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert entry.runtime_data.auth_failed is True
    assert login_posts(aioclient_mock) == 1
    assert window_gets(aioclient_mock, USER_ID) == 2


async def test_one_relogin_per_refresh_across_requests(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A refresh whose latest request already re-logged in does not log in again
    when its window request is refused too."""
    entry = await _setup(hass, aioclient_mock, freezer)
    coordinator = entry.runtime_data.fast[USER_ID]

    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    remaining = [401, 200]

    async def respond(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        return AiohttpClientMockResponse(method, url, status=remaining.pop(0), json=[])

    aioclient_mock.get(latest_url(USER_ID), params=LATEST_QUERY, side_effect=respond)
    mock_latest(aioclient_mock, window_status=401)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert entry.runtime_data.auth_failed is True
    assert login_posts(aioclient_mock) == 1
    assert latest_gets(aioclient_mock, USER_ID) == 2
    assert window_gets(aioclient_mock, USER_ID) == 1
