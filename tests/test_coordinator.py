"""Tests for the fast coordinator's refresh and failure handling."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.api import RateLimitedError
from custom_components.tidepool.coordinator import TidepoolFastCoordinator
from custom_components.tidepool.models import Cbg

from .entries import (
    USER_ID,
    latest_gets,
    mock_latest,
    mock_login,
    mock_profile,
    mock_slow,
    password_entry,
    window_gets,
)


async def _loaded_coordinator(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, **options
) -> tuple[MockConfigEntry, TidepoolFastCoordinator]:
    entry = password_entry(hass, **options)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry, entry.runtime_data.fast[USER_ID]


async def test_fast_refresh_parses_latest(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _, coordinator = await _loaded_coordinator(hass, aioclient_mock)

    # The newest cbg record of tests/fixtures/latest.json.
    assert coordinator.data.latest.cbg == Cbg(
        value_mmol=3.9,
        time=datetime(2026, 11, 1, 12, 5, tzinfo=UTC),
        id="rec-2",
    )
    assert coordinator.data.latest.smbg is not None
    assert coordinator.data.latest.smbg.id == "rec-3"
    assert set(coordinator.data.window) == {"basal", "bolus", "food"}

    aioclient_mock.clear_requests()
    mock_latest(
        aioclient_mock,
        payload=[
            {"type": "cbg", "id": "rec-9", "time": "2026-11-01T12:10:00Z", "value": 7.2}
        ],
    )
    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data.latest.cbg is not None
    assert coordinator.data.latest.cbg.id == "rec-9"
    assert coordinator.data.latest.cbg.value_mmol == 7.2
    # Two requests per refresh: the latest records and the 24 h window.
    assert aioclient_mock.call_count == 2
    assert latest_gets(aioclient_mock, USER_ID) == 1
    assert window_gets(aioclient_mock, USER_ID) == 1


@pytest.mark.parametrize("status", [429, 500])
async def test_rate_limited_keeps_stale_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, status: int
) -> None:
    _, coordinator = await _loaded_coordinator(hass, aioclient_mock)
    previous = coordinator.data
    assert previous.latest.cbg is not None

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, status=status)
    await coordinator.async_refresh()

    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    if status == 429:
        assert isinstance(coordinator.last_exception.__cause__, RateLimitedError)
    assert coordinator.data is previous
    assert coordinator.data.latest.cbg is not None


async def test_rate_limited_retry_after_delays_next_refresh(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _, coordinator = await _loaded_coordinator(hass, aioclient_mock)
    unsub = coordinator.async_add_listener(lambda: None)  # enables polling

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, status=429, headers={"Retry-After": "900"})
    await coordinator.async_refresh()
    assert not coordinator.last_update_success

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock)
    now = dt_util.utcnow()

    # The normal 5 minute interval has passed, but Retry-After has not.
    async_fire_time_changed(hass, now + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0

    async_fire_time_changed(hass, now + timedelta(seconds=901))
    await hass.async_block_till_done()
    assert latest_gets(aioclient_mock, USER_ID) == 1
    assert coordinator.last_update_success
    unsub()


async def test_short_retry_after_keeps_the_normal_interval(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _, coordinator = await _loaded_coordinator(hass, aioclient_mock)
    unsub = coordinator.async_add_listener(lambda: None)

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, status=429, headers={"Retry-After": "30"})
    await coordinator.async_refresh()

    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock)
    now = dt_util.utcnow()

    async_fire_time_changed(hass, now + timedelta(minutes=2))
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 0

    async_fire_time_changed(hass, now + timedelta(minutes=5, seconds=1))
    await hass.async_block_till_done()
    assert latest_gets(aioclient_mock, USER_ID) == 1
    unsub()
