"""Tests for the refresh and import_history services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import voluptuous as vol
import yaml
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_metadata
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import (
    METRIC_SYSTEM,
    US_CUSTOMARY_SYSTEM,
    UnitSystem,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    statistics_during_period,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import CONF_PATIENTS, DOMAIN
from custom_components.tidepool.models import Cbg
from custom_components.tidepool.statistics import (
    async_import_cbg_statistics,
    statistic_id,
    statistic_name,
)

from .entries import (
    EMAIL,
    PASSWORD,
    PATIENT_ID,
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
    summary_gets,
    window_gets,
)

# 12:30 UTC; the last complete hour ended at 12:00.
NOW = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)
END = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
STATISTIC_ID = "tidepool:fake_user_0001_glucose"
SERVICES_YAML = (
    Path(__file__).parent.parent / "custom_components" / "tidepool" / "services.yaml"
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_db_url, enable_custom_integrations):
    """Allow the custom integration, with the recorder's database prepared first.

    The recorder fixtures need to run before ``hass`` exists, and the shared
    autouse fixture would create ``hass`` first.
    """
    yield


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _api_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _readings(first_hour: datetime, hours: int, base: float = 5.0) -> list[dict]:
    """Twelve 5-minute cbg records for each of ``hours`` hours.

    Hour k holds the values base+k-1, base+k and base+k+1 four times each, so
    its mean is base+k, its minimum base+k-1 and its maximum base+k+1.
    """
    records = []
    for hour in range(hours):
        for i in range(12):
            time = first_hour + timedelta(hours=hour, minutes=5 * i)
            records.append(
                {
                    "type": "cbg",
                    "id": f"rec-{_iso(time)}",
                    "time": _iso(time),
                    "value": base + hour + (i % 3 - 1),
                }
            )
    return records


def mock_cbg(
    aioclient_mock: AiohttpClientMocker,
    start: datetime,
    end: datetime,
    records: list[dict] | None = None,
    status: int = 200,
    user_id: str = USER_ID,
) -> None:
    """Mock one cbg window request of ``import_history``."""
    params = {
        "type": "cbg",
        "startDate": _api_time(start),
        "endDate": _api_time(end),
    }
    if status == 200:
        aioclient_mock.get(latest_url(user_id), params=params, json=records or [])
    else:
        aioclient_mock.get(latest_url(user_id), params=params, status=status)


def cbg_gets(aioclient_mock: AiohttpClientMocker, user_id: str = USER_ID) -> list[Any]:
    """The cbg history request URLs made, in order."""
    return [
        url
        for url in data_gets(aioclient_mock, user_id)
        if url.query.get("type") == "cbg" and "startDate" in url.query
    ]


async def _setup(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory | None = None,
    units: UnitSystem = METRIC_SYSTEM,
) -> MockConfigEntry:
    if freezer is not None:
        freezer.move_to(NOW)
    hass.config.units = units
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _setup_second(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> MockConfigEntry:
    """Load a second entry that has a different patient."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=PATIENT_ID,
        title="Fake Second",
        data={"email": EMAIL, "password": PASSWORD, CONF_PATIENTS: [PATIENT_ID]},
    )
    entry.add_to_hass(hass)
    mock_latest(aioclient_mock, PATIENT_ID)
    mock_profile(aioclient_mock, PATIENT_ID, full_name="Fake Second")
    mock_slow(aioclient_mock, PATIENT_ID)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _import(hass: HomeAssistant, **data: Any) -> None:
    try:
        await hass.services.async_call(DOMAIN, "import_history", data, blocking=True)
    finally:
        # The recorder writes in its own thread; leave none of it running.
        if "recorder" in hass.config.components:
            await async_wait_recording_done(hass)


async def _stored(
    hass: HomeAssistant, statistic: str = STATISTIC_ID
) -> list[tuple[datetime, float, float, float]]:
    """The stored hourly (start, mean, min, max) of a statistic."""
    await async_wait_recording_done(hass)
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        NOW - timedelta(days=100),
        None,
        {statistic},
    )
    return [
        (
            datetime.fromtimestamp(row["start"], UTC),
            row["mean"],
            row["min"],
            row["max"],
        )
        for row in stats.get(statistic, [])
    ]


# refresh


async def test_refresh_service_refreshes_both_coordinators(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.mock_calls.clear()

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)

    # The fast coordinator asks for the latest records and the 24 h window,
    # the slow one for the cgm summary.
    assert latest_gets(aioclient_mock, USER_ID) == 1
    assert window_gets(aioclient_mock, USER_ID) == 1
    assert summary_gets(aioclient_mock, USER_ID) == 1
    assert login_posts(aioclient_mock) == 0


async def test_refresh_service_covers_every_loaded_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    await _setup_second(hass, aioclient_mock)
    aioclient_mock.mock_calls.clear()

    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)

    for user_id in (USER_ID, PATIENT_ID):
        assert latest_gets(aioclient_mock, user_id) == 1
        assert summary_gets(aioclient_mock, user_id) == 1


async def test_refresh_service_targets_one_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    second = await _setup_second(hass, aioclient_mock)
    aioclient_mock.mock_calls.clear()

    await hass.services.async_call(
        DOMAIN, "refresh", {"entry": second.entry_id}, blocking=True
    )

    assert latest_gets(aioclient_mock, PATIENT_ID) == 1
    assert summary_gets(aioclient_mock, PATIENT_ID) == 1
    assert latest_gets(aioclient_mock, USER_ID) == 0
    assert summary_gets(aioclient_mock, USER_ID) == 0


@pytest.mark.parametrize("service", ["refresh", "import_history"])
async def test_services_reject_an_unknown_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, service: str
) -> None:
    await _setup(hass, aioclient_mock)
    other = MockConfigEntry(domain="other_domain")
    other.add_to_hass(hass)

    for entry_id in ("no-such-entry", other.entry_id):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN, service, {"entry": entry_id}, blocking=True
            )
        assert err.value.translation_key == "entry_not_found"


@pytest.mark.parametrize("service", ["refresh", "import_history"])
async def test_services_reject_an_entry_that_is_not_loaded(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, service: str
) -> None:
    entry = await _setup(hass, aioclient_mock)
    assert await hass.config_entries.async_unload(entry.entry_id)

    for data in ({"entry": entry.entry_id}, {}):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(DOMAIN, service, data, blocking=True)
        assert err.value.translation_key in {"entry_not_loaded", "no_entries"}


async def test_refresh_service_skips_an_entry_that_needs_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    first = await _setup(hass, aioclient_mock)
    await _setup_second(hass, aioclient_mock)
    first.runtime_data.auth_failed = True
    aioclient_mock.mock_calls.clear()

    # Named, the entry is an error; unnamed, it is left out without a call.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "refresh", {"entry": first.entry_id}, blocking=True
        )
    assert err.value.translation_key == "entry_auth_failed"
    await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)

    assert latest_gets(aioclient_mock, USER_ID) == 0
    assert summary_gets(aioclient_mock, USER_ID) == 0
    assert latest_gets(aioclient_mock, PATIENT_ID) == 1
    assert login_posts(aioclient_mock) == 0


async def test_refresh_service_fails_when_a_refresh_fails(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, status=500)
    mock_slow(aioclient_mock)

    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(DOMAIN, "refresh", {}, blocking=True)

    assert err.value.translation_key == "refresh_failed"
    # The slow coordinator was still refreshed.
    assert summary_gets(aioclient_mock, USER_ID) == 1


# import_history


async def test_import_history_writes_hourly_stats(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    first = END - timedelta(hours=4)
    mock_cbg(aioclient_mock, END - timedelta(days=1), END, _readings(first, 3))

    await _import(hass, days=1)

    assert await _stored(hass) == [
        (first, 5.0, 4.0, 6.0),
        (first + timedelta(hours=1), 6.0, 5.0, 7.0),
        (first + timedelta(hours=2), 7.0, 6.0, 8.0),
    ]


async def test_import_history_is_idempotent(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    mock_cbg(
        aioclient_mock,
        END - timedelta(days=1),
        END,
        _readings(END - timedelta(hours=4), 3),
    )

    await _import(hass, days=1)
    once = await _stored(hass)
    await _import(hass, days=1)

    assert len(once) == 3
    assert await _stored(hass) == once


@pytest.mark.parametrize("days", [91, 0, -1, 1.5, "7", None])
async def test_import_history_rejects_over_90_days(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, days: Any
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.mock_calls.clear()

    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "import_history", {"days": days}, blocking=True
        )

    assert aioclient_mock.call_count == 0


async def test_import_history_defaults_to_14_days_in_two_chunks(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    start = END - timedelta(days=14)
    middle = start + timedelta(days=7)
    # A reading exactly on the boundary is in both responses (the end of a
    # window is inclusive) but belongs to the later window only.
    boundary = {
        "type": "cbg",
        "id": "rec-boundary",
        "time": _iso(middle),
        "value": 10.0,
    }
    early = _readings(middle - timedelta(hours=2), 1, base=5.0)
    late = _readings(middle, 1, base=7.0)
    mock_cbg(aioclient_mock, start, middle, [*early, boundary])
    mock_cbg(aioclient_mock, middle, END, [boundary, *late])

    await _import(hass)

    urls = cbg_gets(aioclient_mock)
    assert [(u.query["startDate"], u.query["endDate"]) for u in urls] == [
        (_api_time(start), _api_time(middle)),
        (_api_time(middle), _api_time(END)),
    ]
    stored = await _stored(hass)
    assert [row[0] for row in stored] == [middle - timedelta(hours=2), middle]
    # The hour of the boundary: 12 readings (6, 7, 8) plus the boundary one, 10,
    # counted once; the mean is stored to two decimals.
    assert stored[1][1] == pytest.approx((4 * (6 + 7 + 8) + 10) / 13, abs=0.01)
    assert stored[1][3] == 10.0


async def test_import_history_uneven_window_is_chunked_oldest_first(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    start = END - timedelta(days=10)
    middle = start + timedelta(days=7)
    mock_cbg(aioclient_mock, start, middle)
    mock_cbg(aioclient_mock, middle, END)

    await _import(hass, days=10)

    assert [u.query["startDate"] for u in cbg_gets(aioclient_mock)] == [
        _api_time(start),
        _api_time(middle),
    ]
    assert await _stored(hass) == []


async def test_import_history_skips_empty_and_current_hours(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    hour_8 = END - timedelta(hours=4)
    hour_10 = END - timedelta(hours=2)
    # Nothing at 09:00, and a reading in the current partial hour (12:20) that
    # the API returned anyway.
    records = [
        *_readings(hour_8, 1),
        *_readings(hour_10, 1),
        {"type": "cbg", "id": "rec-now", "time": "2026-11-01T12:20:00Z", "value": 9.0},
        {"type": "cbg", "id": "rec-bad-1", "time": "garbage", "value": 9.0},
        {"type": "cbg", "id": "rec-bad-2", "time": "2026-11-01T10:20:00Z"},
        {"type": "smbg", "id": "rec-smbg", "time": "2026-11-01T10:21:00Z", "value": 9},
    ]
    mock_cbg(aioclient_mock, END - timedelta(days=1), END, records)

    await _import(hass, days=1)

    assert [row[0] for row in await _stored(hass)] == [hour_8, hour_10]


@pytest.mark.parametrize(
    ("units", "unit", "expected"),
    [
        (METRIC_SYSTEM, "mmol/L", [(5.0, 4.0, 6.0), (6.0, 5.0, 7.0)]),
        (
            US_CUSTOMARY_SYSTEM,
            "mg/dL",
            [(90.1, 72.1, 108.1), (108.1, 90.1, 126.1)],
        ),
    ],
)
async def test_import_history_converts_to_the_unit_system(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
    units: UnitSystem,
    unit: str,
    expected: list[tuple[float, float, float]],
) -> None:
    await _setup(hass, aioclient_mock, freezer, units)
    first = END - timedelta(hours=3)
    mock_cbg(aioclient_mock, END - timedelta(days=1), END, _readings(first, 2))

    await _import(hass, days=1)

    assert [row[1:] for row in await _stored(hass)] == expected
    await async_wait_recording_done(hass)
    metadata = await get_instance(hass).async_add_executor_job(
        _read_metadata, hass, STATISTIC_ID
    )
    assert metadata["unit_of_measurement"] == unit
    assert metadata["name"] == "Tidepool glucose (Fake Owner)"
    assert metadata["source"] == "tidepool"
    assert metadata["unit_class"] == "blood_glucose_concentration"
    assert metadata["has_sum"] is False


def _read_metadata(hass: HomeAssistant, statistic: str) -> dict:
    return get_metadata(hass, statistic_ids={statistic})[statistic][1]


async def test_import_history_covers_every_patient_of_every_entry(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    second = await _setup_second(hass, aioclient_mock)
    start = END - timedelta(days=1)
    mock_cbg(aioclient_mock, start, END, _readings(END - timedelta(hours=3), 1))
    mock_cbg(
        aioclient_mock,
        start,
        END,
        _readings(END - timedelta(hours=3), 1, base=8.0),
        user_id=PATIENT_ID,
    )

    await _import(hass, days=1)
    both = (
        await _stored(hass),
        await _stored(hass, "tidepool:fake_patient_0002_glucose"),
    )
    assert [rows[0][1] for rows in both] == [5.0, 8.0]
    # Each statistic is named after its patient; the ids stay id-based.
    await async_wait_recording_done(hass)
    names = [
        (
            await get_instance(hass).async_add_executor_job(
                _read_metadata, hass, statistic
            )
        )["name"]
        for statistic in (STATISTIC_ID, "tidepool:fake_patient_0002_glucose")
    ]
    assert names == ["Tidepool glucose (Fake Owner)", "Tidepool glucose (Fake Second)"]

    # Naming an entry leaves the other's patient alone.
    aioclient_mock.mock_calls.clear()
    await _import(hass, days=1, entry=second.entry_id)
    assert len(cbg_gets(aioclient_mock, PATIENT_ID)) == 1
    assert cbg_gets(aioclient_mock, USER_ID) == []


async def test_import_history_keeps_earlier_chunks_when_rate_limited(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    start = END - timedelta(days=14)
    middle = start + timedelta(days=7)
    first = middle - timedelta(hours=3)
    mock_cbg(aioclient_mock, start, middle, _readings(first, 2))
    mock_cbg(aioclient_mock, middle, END, status=429)

    with pytest.raises(HomeAssistantError) as err:
        await _import(hass)

    assert err.value.translation_key == "import_rate_limited"
    # The two hours of the first window were written, and no more was asked for.
    assert err.value.translation_placeholders == {"hours": "2"}
    assert [row[0] for row in await _stored(hass)] == [
        first,
        first + timedelta(hours=1),
    ]
    assert len(cbg_gets(aioclient_mock)) == 2


async def test_import_history_reports_other_failures(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    mock_cbg(aioclient_mock, END - timedelta(days=1), END, status=500)

    with pytest.raises(HomeAssistantError) as err:
        await _import(hass, days=1)

    assert err.value.translation_key == "import_failed"


async def test_import_history_signs_in_once_after_a_refused_request(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    entry = await _setup(hass, aioclient_mock, freezer)
    assert login_posts(aioclient_mock) == 1
    start = END - timedelta(days=14)
    mock_cbg(aioclient_mock, start, start + timedelta(days=7), status=401)

    with pytest.raises(HomeAssistantError) as err:
        await _import(hass)
    await hass.async_block_till_done()

    # One retry (one more login), then the entry needs reauth: no loop.
    assert err.value.translation_key == "auth_failed"
    assert login_posts(aioclient_mock) == 2
    assert len(cbg_gets(aioclient_mock)) == 2
    assert entry.runtime_data.auth_failed
    assert any(
        flow["context"]["source"] == "reauth"
        for flow in hass.config_entries.flow.async_progress()
    )

    # The entry is now left alone.
    with pytest.raises(ServiceValidationError) as err_again:
        await _import(hass, entry=entry.entry_id)
    assert err_again.value.translation_key == "entry_auth_failed"
    assert login_posts(aioclient_mock) == 2


async def test_import_history_needs_the_recorder(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    await _setup(hass, aioclient_mock)
    aioclient_mock.mock_calls.clear()

    with pytest.raises(ServiceValidationError) as err:
        await _import(hass, days=1)

    assert err.value.translation_key == "recorder_unavailable"
    assert aioclient_mock.call_count == 0


async def test_import_history_logs_a_summary_without_ids(
    recorder_mock,
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    freezer: FrozenDateTimeFactory,
) -> None:
    await _setup(hass, aioclient_mock, freezer)
    mock_cbg(
        aioclient_mock,
        END - timedelta(days=2),
        END,
        _readings(END - timedelta(hours=4), 3),
    )

    # The recorder thread logs after the test, so the logger is replaced
    # rather than captured.
    with patch("custom_components.tidepool.services._LOGGER") as logger:
        await _import(hass, days=2)

    messages = [
        call.args[0] % call.args[1:] for call in logger.method_calls if call.args
    ]
    assert messages == ["Imported 3 hourly glucose statistics for the last 2 days"]


# statistics module


def test_statistic_id_is_sanitised() -> None:
    assert statistic_id("fake-user-0001") == STATISTIC_ID
    assert statistic_id("Ab1.c d") == "tidepool:ab1_c_d_glucose"
    assert statistic_id("_-x--y-_") == "tidepool:x_y_glucose"


async def test_import_cbg_statistics_returns_the_hours_written(
    recorder_mock, hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    hour = END - timedelta(hours=2)
    records = [
        Cbg(value_mmol=5.0, time=hour + timedelta(minutes=5), id="a"),
        Cbg(value_mmol=7.0, time=hour + timedelta(minutes=35), id=None),
        # The current partial hour is left out.
        Cbg(value_mmol=9.0, time=END + timedelta(minutes=5), id="c"),
    ]

    assert async_import_cbg_statistics(hass, USER_ID, records, "mmol/L") == 1
    assert async_import_cbg_statistics(hass, USER_ID, [], "mmol/L") == 0

    assert await _stored(hass) == [(hour, 6.0, 5.0, 7.0)]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Fake Owner", "Tidepool glucose (Fake Owner)"),
        (None, "Tidepool glucose"),
        ("  ", "Tidepool glucose"),
    ],
)
def test_statistic_name_per_patient(name: str | None, expected: str) -> None:
    assert statistic_name(name) == expected


# services.yaml and strings


def test_services_yaml_documents_the_registered_services() -> None:
    services = yaml.safe_load(SERVICES_YAML.read_text())
    assert set(services) == {"refresh", "import_history"}
    for service in services.values():
        selector = service["fields"]["entry"]["selector"]["config_entry"]
        assert selector == {"integration": DOMAIN}
    days = services["import_history"]["fields"]["days"]
    assert days["default"] == 14
    assert days["selector"]["number"]["min"] == 1
    assert days["selector"]["number"]["max"] == 90


def test_strings_describe_the_services_and_their_errors() -> None:
    strings = yaml.safe_load((SERVICES_YAML.parent / "strings.json").read_text())
    for service, fields in (
        ("refresh", {"entry"}),
        ("import_history", {"entry", "days"}),
    ):
        text = strings["services"][service]
        assert text["name"]
        assert text["description"]
        assert set(text["fields"]) == fields
    assert set(strings["exceptions"]) >= {
        "entry_not_found",
        "entry_not_loaded",
        "entry_auth_failed",
        "no_entries",
        "refresh_failed",
        "recorder_unavailable",
        "invalid_days",
        "import_rate_limited",
        "import_failed",
        "auth_failed",
        "patient_access_refused",
    }
