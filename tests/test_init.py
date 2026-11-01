"""Tests for entry setup, unload and the shared per-entry runtime."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tidepool.api import TidepoolClient
from custom_components.tidepool.auth import LegacyAuth, OAuthAuth
from custom_components.tidepool.const import DOMAIN, OAUTH_TOKEN_HEADER
from custom_components.tidepool.coordinator import (
    TidepoolFastCoordinator,
    TidepoolRuntime,
)

from .entries import (
    EMAIL,
    PASSWORD,
    PATIENT_ID,
    SESSION_TOKEN,
    USER_ID,
    latest_gets,
    latest_url,
    legacy_entry,
    load_fixture,
    login_posts,
    mock_latest,
    mock_login,
    oauth_entry,
)
from .mock_keycloak import MockKeycloak

SECRETS = (EMAIL, PASSWORD, SESSION_TOKEN, USER_ID, PATIENT_ID)


def _assert_no_secrets(caplog: pytest.LogCaptureFixture) -> None:
    formatter = logging.Formatter()
    for record in caplog.records:
        text = formatter.format(record)
        for secret in SECRETS:
            assert secret not in text


def _reauth_flows(hass: HomeAssistant) -> list[dict]:
    return [
        flow
        for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
        if flow["context"]["source"] == SOURCE_REAUTH
    ]


async def test_setup_and_unload_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    runtime = entry.runtime_data
    assert isinstance(runtime, TidepoolRuntime)
    assert isinstance(runtime.client, TidepoolClient)
    assert runtime.auth_failed is False
    assert list(runtime.fast) == [USER_ID, PATIENT_ID]
    for user_id, coordinator in runtime.fast.items():
        assert isinstance(coordinator, TidepoolFastCoordinator)
        assert coordinator.user_id == user_id
        assert coordinator.update_interval == timedelta(minutes=5)
        assert coordinator.last_update_success
        assert coordinator.data.window == {}
        assert latest_gets(aioclient_mock, user_id) == 1
    assert login_posts(aioclient_mock) == 1  # both patients share one session

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")


async def test_options_override_patients_and_interval(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = legacy_entry(hass, patients=[USER_ID])
    hass.config_entries.async_update_entry(
        entry, options={"patients": [PATIENT_ID], "fast_interval": 10}
    )
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, PATIENT_ID)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert list(entry.runtime_data.fast) == [PATIENT_ID]
    coordinator = entry.runtime_data.fast[PATIENT_ID]
    assert coordinator.update_interval == timedelta(minutes=10)
    assert latest_gets(aioclient_mock, USER_ID) == 0


@pytest.fixture
async def credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("fake-client-id-xyz", "")
    )


@pytest.mark.usefixtures("credentials")
async def test_one_auth_strategy_per_entry_oauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    keycloak = MockKeycloak(aioclient_mock)
    entry = oauth_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID)

    with (
        patch(
            "custom_components.tidepool.OAuthAuth", side_effect=OAuthAuth
        ) as oauth_cls,
        patch("custom_components.tidepool.LegacyAuth") as legacy_cls,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert oauth_cls.call_count == 1
    assert legacy_cls.call_count == 0
    _, called_entry, _ = oauth_cls.call_args.args
    assert called_entry is entry
    runtime = entry.runtime_data
    assert len(runtime.fast) == 2
    # Every coordinator shares the entry's one client and its one OAuthAuth.
    for coordinator in runtime.fast.values():
        assert coordinator.client is runtime.client
    assert isinstance(runtime.client._auth, OAuthAuth)
    # The stored token was still valid: no refresh, and it authenticated the API.
    assert keycloak.token_requests == []
    data_calls = [
        call for call in aioclient_mock.mock_calls if "/data/" in str(call[1])
    ]
    assert len(data_calls) == 2
    for call in data_calls:
        assert call[3][OAUTH_TOKEN_HEADER] == entry.data["token"]["access_token"]


async def test_legacy_entry_uses_legacy_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = legacy_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)

    with patch("custom_components.tidepool.OAuthAuth") as oauth_cls:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert oauth_cls.call_count == 0
    assert isinstance(entry.runtime_data.client._auth, LegacyAuth)


async def test_auth_error_triggers_reauth(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock, status=401)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    [flow] = _reauth_flows(hass)
    assert flow["context"]["entry_id"] == entry.entry_id
    # A wrong password at setup: one login attempt for both patients.
    assert login_posts(aioclient_mock) == 1
    assert latest_gets(aioclient_mock, USER_ID) == 0
    assert latest_gets(aioclient_mock, PATIENT_ID) == 0
    _assert_no_secrets(caplog)


async def test_wrong_password_one_login_attempt_for_two_patients(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The first auth failure stops every other coordinator of the entry.

    The entry loads with a working password. The password is then changed on
    Tidepool, which revokes the session: the stored password is now wrong.
    The first coordinator's request is rejected; the second coordinator must
    not try to log in again with the wrong password. (During setup the
    refreshes run one after the other and setup stops at the first auth
    failure, so only a running entry can show the shared flag at work; the
    setup case is covered by ``test_auth_error_triggers_reauth``.)
    """
    caplog.set_level(logging.DEBUG)
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    setup_logins = login_posts(aioclient_mock)
    assert setup_logins == 1

    aioclient_mock.clear_requests()
    mock_login(aioclient_mock, status=401)
    mock_latest(aioclient_mock, USER_ID, status=401)
    mock_latest(aioclient_mock, PATIENT_ID, status=401)

    first, second = entry.runtime_data.fast.values()
    await first.async_refresh()
    await second.async_refresh()
    await hass.async_block_till_done()

    # The refused GET earns one silent re-login, which the changed password
    # rejects; that is the only attempt with the wrong password.
    assert setup_logins + login_posts(aioclient_mock) == 2
    assert login_posts(aioclient_mock) == 1
    assert entry.runtime_data.auth_failed is True
    assert latest_gets(aioclient_mock, USER_ID) == 1
    assert latest_gets(aioclient_mock, PATIENT_ID) == 0
    assert not first.last_update_success
    assert not second.last_update_success
    assert len(_reauth_flows(hass)) == 1
    _assert_no_secrets(caplog)


async def test_account_with_no_data_loads(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    caplog: pytest.LogCaptureFixture,
) -> None:
    entry = legacy_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, payload=[])

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    coordinator = entry.runtime_data.fast[USER_ID]
    assert coordinator.last_update_success
    assert coordinator.last_exception is None
    assert coordinator.data.latest.cbg is None
    assert coordinator.data.latest.smbg is None
    # No error from anyone; not even a warning from the integration itself.
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert not [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name.startswith("custom_components.tidepool")
    ]


async def _load_two_patients(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> MockConfigEntry:
    entry = legacy_entry(hass, patients=[USER_ID, PATIENT_ID])
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert login_posts(aioclient_mock) == 1
    aioclient_mock.clear_requests()
    return entry


def _mock_latest_sequence(
    aioclient_mock: AiohttpClientMocker, user_id: str, statuses: list[int]
) -> None:
    """Answer the latest GETs for ``user_id`` with ``statuses`` in turn."""
    remaining = list(statuses)

    async def respond(method: str, url: URL, data: Any) -> AiohttpClientMockResponse:
        status = remaining.pop(0)
        if status == 200:
            return AiohttpClientMockResponse(
                method, url, json=load_fixture("latest.json")
            )
        return AiohttpClientMockResponse(method, url, status=status)

    aioclient_mock.get(latest_url(user_id), side_effect=respond)


async def test_expired_session_relogs_in_silently(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = legacy_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    setup_logins = login_posts(aioclient_mock)
    assert setup_logins == 1

    # The session token expired; the password is still right.
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    _mock_latest_sequence(aioclient_mock, USER_ID, [401, 200])
    coordinator = entry.runtime_data.fast[USER_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert coordinator.last_update_success
    assert coordinator.data.latest.cbg is not None
    assert entry.runtime_data.auth_failed is False
    assert _reauth_flows(hass) == []
    assert setup_logins + login_posts(aioclient_mock) == 2
    assert latest_gets(aioclient_mock, USER_ID) == 2


async def test_get_401_twice_triggers_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = legacy_entry(hass)
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Login works, but the API keeps refusing the fresh session.
    aioclient_mock.clear_requests()
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, status=401)
    coordinator = entry.runtime_data.fast[USER_ID]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not coordinator.last_update_success
    assert entry.runtime_data.auth_failed is True
    assert len(_reauth_flows(hass)) == 1
    assert login_posts(aioclient_mock) == 1  # one silent re-login, no loop
    assert latest_gets(aioclient_mock, USER_ID) == 2


async def test_data_403_on_other_patient_does_not_reauth_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = await _load_two_patients(hass, aioclient_mock)
    own, other = entry.runtime_data.fast.values()
    own_data = own.data

    # The other patient's share was revoked.
    mock_login(aioclient_mock)
    mock_latest(aioclient_mock, USER_ID)
    mock_latest(aioclient_mock, PATIENT_ID, status=403)
    await other.async_refresh()
    await own.async_refresh()
    await hass.async_block_till_done()

    assert not other.last_update_success
    assert isinstance(other.last_exception, UpdateFailed)
    assert latest_gets(aioclient_mock, PATIENT_ID) == 2
    assert own.last_update_success
    assert own.data.latest.cbg == own_data.latest.cbg
    assert entry.runtime_data.auth_failed is False
    assert _reauth_flows(hass) == []
    assert entry.state is ConfigEntryState.LOADED
