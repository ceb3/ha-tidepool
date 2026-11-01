"""Tests for the repair issue raised when Tidepool withdraws the sign-in."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import DOMAIN

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

COMPONENT = Path(__file__).parent.parent / "custom_components" / "tidepool"
OTHER_USER_ID = "fake-other-0009"
LEARN_MORE = "https://github.com/ceb3/ha-tidepool#sign-in-and-limitations"
SECRETS = (EMAIL, PASSWORD, SESSION_TOKEN, USER_ID, PATIENT_ID, "Fake Owner")


def _issue_id(entry: MockConfigEntry) -> str:
    return f"login_withdrawn_{entry.entry_id}"


def _issues(hass: HomeAssistant) -> dict[tuple[str, str], ir.IssueEntry]:
    return {
        key: issue
        for key, issue in ir.async_get(hass).issues.items()
        if key[0] == DOMAIN
    }


async def _load(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> MockConfigEntry:
    entry = password_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    mock_profile(aioclient_mock)
    mock_slow(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    aioclient_mock.clear_requests()
    return entry


async def _withdraw(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    entry: MockConfigEntry,
    user_id: str = USER_ID,
) -> None:
    """The session expires and signing in again finds the login withdrawn."""
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, user_id, status=401)
    mock_login(aioclient_mock, status=410)
    await entry.runtime_data.fast[user_id].async_refresh()
    await hass.async_block_till_done()


async def test_withdrawn_login_creates_repair_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load(hass, aioclient_mock)
    assert _issues(hass) == {}

    await _withdraw(hass, aioclient_mock, entry)

    coordinator = entry.runtime_data.fast[USER_ID]
    assert not coordinator.last_update_success
    assert isinstance(coordinator.last_exception, UpdateFailed)
    # Not a credentials problem: no reauth, the entry stays loaded.
    assert entry.runtime_data.auth_failed is False
    assert entry.state is ConfigEntryState.LOADED
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.severity is ir.IssueSeverity.ERROR
    assert issue.translation_key == "login_withdrawn"
    assert issue.learn_more_url == LEARN_MORE


async def test_withdrawn_login_at_setup_creates_repair_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = password_entry(hass)
    mock_login(aioclient_mock, status=404)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert list(_issues(hass)) == [(DOMAIN, _issue_id(entry))]


async def test_repair_cleared_after_successful_login(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load(hass, aioclient_mock)
    await _withdraw(hass, aioclient_mock, entry)
    assert (DOMAIN, _issue_id(entry)) in _issues(hass)

    # Tidepool brings the login back.
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    coordinator = entry.runtime_data.fast[USER_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert _issues(hass) == {}


async def test_repeated_failures_keep_one_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load(hass, aioclient_mock)

    await _withdraw(hass, aioclient_mock, entry)
    first = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    await _withdraw(hass, aioclient_mock, entry)
    await _withdraw(hass, aioclient_mock, entry)

    assert list(_issues(hass)) == [(DOMAIN, _issue_id(entry))]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    assert first is not None and issue is not None
    assert issue.created == first.created


async def test_issue_is_per_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Two logins: only the entry whose sign-in fails gets an issue."""
    first = password_entry(hass)
    second = MockConfigEntry(
        domain=DOMAIN,
        unique_id=OTHER_USER_ID,
        title="Fake Other",
        data={
            "email": "fake.other@example.invalid",
            "password": "fake-password-other",
            "patients": [PATIENT_ID],
        },
    )
    second.add_to_hass(hass)
    mock_login(aioclient_mock)
    for user_id in (USER_ID, PATIENT_ID):
        mock_latest(aioclient_mock, user_id)
        mock_profile(aioclient_mock, user_id)
        mock_slow(aioclient_mock, user_id)
    # Setting up the integration sets up both entries.
    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()
    assert second.state is ConfigEntryState.LOADED

    await _withdraw(hass, aioclient_mock, second, PATIENT_ID)
    # The first entry's session still works.
    aioclient_mock.clear_requests()
    mock_latest(aioclient_mock, USER_ID)
    await first.runtime_data.fast[USER_ID].async_refresh()
    await hass.async_block_till_done()

    assert first.runtime_data.fast[USER_ID].last_update_success
    assert list(_issues(hass)) == [(DOMAIN, _issue_id(second))]


async def test_issue_deleted_on_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load(hass, aioclient_mock)
    await _withdraw(hass, aioclient_mock, entry)
    assert _issues(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert _issues(hass) == {}


@pytest.mark.parametrize("how", ["unload", "disable"])
async def test_issue_deleted_when_a_retrying_entry_stops(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, how: str
) -> None:
    """An entry retrying setup (login withdrawn) is unloaded or disabled."""
    entry = password_entry(hass)
    mock_login(aioclient_mock, status=410)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert list(_issues(hass)) == [(DOMAIN, _issue_id(entry))]

    if how == "unload":
        assert await hass.config_entries.async_unload(entry.entry_id)
    else:
        assert await hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _issues(hass) == {}


async def test_issue_deleted_on_removal(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """An entry that never loaded (login withdrawn at setup) is removed."""
    entry = password_entry(hass)
    mock_login(aioclient_mock, status=410)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _issues(hass)

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert _issues(hass) == {}


async def test_issue_text_and_placeholders_leak_nothing(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load(hass, aioclient_mock)
    await _withdraw(hass, aioclient_mock, entry)
    issue = ir.async_get(hass).async_get_issue(DOMAIN, _issue_id(entry))
    assert issue is not None

    placeholders = issue.translation_placeholders or {}
    strings = json.loads((COMPONENT / "strings.json").read_text())
    assert strings == json.loads((COMPONENT / "translations" / "en.json").read_text())
    texts = strings["issues"]["login_withdrawn"]
    # Every placeholder the text uses is filled, and nothing else is passed.
    used = set(re.findall(r"{(\w+)}", texts["title"] + texts["description"]))
    assert used == set(placeholders)
    rendered = json.dumps(
        {
            "issue": issue.to_json(),
            "placeholders": placeholders,
            "learn_more_url": issue.learn_more_url,
            "title": texts["title"].format(**placeholders),
            "description": texts["description"].format(**placeholders),
        }
    )
    for secret in SECRETS:
        assert secret not in rendered


async def test_withdrawn_login_on_optional_request_raises_issue(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A withdrawn sign-in is not an optional request's failure to shrug off."""
    entry = await _load(hass, aioclient_mock)
    # The latest records still come with the cached session; the 24 h window
    # request finds it expired, and signing in again finds the login withdrawn.
    mock_latest(aioclient_mock, window_status=401)
    mock_login(aioclient_mock, status=410)
    coordinator = entry.runtime_data.fast[USER_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert list(_issues(hass)) == [(DOMAIN, _issue_id(entry))]
