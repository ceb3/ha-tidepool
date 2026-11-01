"""Tests for the config flow (OAuth and legacy), reauth and the options flow."""

from __future__ import annotations

import logging
import time
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.selector import NumberSelector, SelectSelector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator
from yarl import URL

from custom_components.tidepool.const import (
    API_URL,
    CONF_AUTH_METHOD,
    CONF_FAST_INTERVAL,
    CONF_PATIENTS,
    DOMAIN,
    LEGACY_DISCLAIMER,
    OAUTH_TOKEN_HEADER,
)

from .mock_keycloak import FAKE_USER_ID, MockKeycloak, make_unsigned_jwt

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
NEW_PASSWORD = "fake-password-correct-horse"
SESSION_TOKEN = "fake-session-token-1"
USER_ID = FAKE_USER_ID
PATIENT_ID = "fake-patient-0002"
NAMELESS_ID = "fake-patient-0003"
OTHER_USER_ID = "fake-other-0009"
CLIENT_ID = "fake-client-id-xyz"
LOGIN_URL = f"{API_URL}/auth/login"

EXPECTED_DISCLAIMER = (
    "This method uses a Tidepool endpoint that Tidepool does not document for "
    "third parties. Tidepool recommends OAuth, and says password login is "
    "limited to trusted partners. It works today but may be changed or removed "
    "at any time without notice, and it will not work with accounts that "
    "require two-factor authentication. Use OAuth when you can."
)


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep entry setup out of the flow tests."""
    with patch(
        "custom_components.tidepool.async_setup_entry",
        new_callable=AsyncMock,
        return_value=True,
        create=True,
    ) as mock:
        yield mock


@pytest.fixture
def keycloak(aioclient_mock: AiohttpClientMocker) -> MockKeycloak:
    return MockKeycloak(aioclient_mock)


@pytest.fixture
async def credentials(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(hass, DOMAIN, ClientCredential(CLIENT_ID, ""))


def _mock_login(
    aioclient_mock: AiohttpClientMocker, user_id: str = USER_ID, status: int = 200
) -> None:
    if status == 200:
        aioclient_mock.post(
            LOGIN_URL,
            json={"userid": user_id},
            headers={"x-tidepool-session-token": SESSION_TOKEN},
        )
    else:
        aioclient_mock.post(LOGIN_URL, status=status)


def _mock_account(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    shared: dict[str, str | None] | None = None,
) -> None:
    """Mock access groups and profiles; a ``None`` name means no profile."""
    names: dict[str, str | None] = {user_id: "Fake Owner", **(shared or {})}
    aioclient_mock.get(
        f"{API_URL}/access/groups/{user_id}",
        json={
            account: ({"root": {}} if account == user_id else {"view": {}})
            for account in names
        },
    )
    for account, name in names.items():
        url = f"{API_URL}/metadata/{account}/profile"
        if name is None:
            aioclient_mock.get(url, status=404)
        else:
            aioclient_mock.get(url, json={"fullName": name, "patient": {}})


def _select_options(result: dict[str, Any], key: str) -> list[dict[str, str]]:
    selector = _schema_value(result, key)
    assert isinstance(selector, SelectSelector)
    return selector.config["options"]


def _schema_value(result: dict[str, Any], key: str) -> Any:
    for marker, value in result["data_schema"].schema.items():
        if marker == key:
            return value
    raise AssertionError(f"{key} not in schema")


def _schema_default(result: dict[str, Any], key: str) -> Any:
    for marker in result["data_schema"].schema:
        if marker == key:
            return marker.default()
    raise AssertionError(f"{key} not in schema")


async def _start_legacy(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "legacy"}
    )


async def _submit_legacy(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow_id, {"email": EMAIL, "password": PASSWORD}
    )


async def _complete_oauth(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    keycloak: MockKeycloak,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Sign in at the mock login server and return the step after the code."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    authorize = URL(result["url"])
    assert str(authorize.with_query(None)) == keycloak.authorize_url
    code = keycloak.issue_code(authorize.query["code_challenge"])

    client = await hass_client_no_auth()
    response = await client.get(
        "/auth/external/callback",
        params={"code": code, "state": authorize.query["state"]},
    )
    assert response.status == 200

    return await hass.config_entries.flow.async_configure(result["flow_id"])


def _legacy_entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="Fake Owner",
        data={
            CONF_AUTH_METHOD: "legacy",
            "email": EMAIL,
            "password": PASSWORD,
            CONF_PATIENTS: [USER_ID],
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def _oauth_entry(hass: HomeAssistant) -> MockConfigEntry:
    token = {
        "access_token": make_unsigned_jwt({"sub": USER_ID, "jti": "old"}),
        "refresh_token": "fake-refresh-old",
        "token_type": "Bearer",
        "expires_in": 600,
        "expires_at": time.time() + 600,
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="Fake Owner",
        data={
            CONF_AUTH_METHOD: "oauth",
            "auth_implementation": DOMAIN,
            "token": token,
            CONF_PATIENTS: [USER_ID],
        },
    )
    entry.add_to_hass(hass)
    return entry


# -- menu and legacy -------------------------------------------------------


async def test_menu_offers_both_methods(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "user"
    assert list(result["menu_options"]) == ["oauth", "legacy"]


async def test_legacy_form_shows_disclaimer(hass: HomeAssistant) -> None:
    result = await _start_legacy(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "legacy"
    assert result["description_placeholders"]["disclaimer"] == EXPECTED_DISCLAIMER
    assert LEGACY_DISCLAIMER == EXPECTED_DISCLAIMER
    assert result["errors"] in (None, {})


async def test_legacy_success_creates_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "patients"
    assert _schema_default(result, CONF_PATIENTS) == [USER_ID]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PATIENTS: [USER_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Fake Owner"
    entry = result["result"]
    assert entry.unique_id == USER_ID
    assert dict(entry.data) == {
        CONF_AUTH_METHOD: "legacy",
        "email": EMAIL,
        "password": PASSWORD,
        CONF_PATIENTS: [USER_ID],
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_legacy_wrong_password_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock, status=401)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "legacy"
    assert result["errors"] == {"base": "invalid_auth"}
    assert result["description_placeholders"]["disclaimer"] == EXPECTED_DISCLAIMER


async def test_legacy_withdrawn_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock, status=410)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "legacy_withdrawn"}


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, "cannot_connect"), (500, "cannot_connect")],
)
async def test_legacy_server_errors_show_cannot_connect(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: str,
) -> None:
    _mock_login(aioclient_mock, status=status)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["errors"] == {"base": error}


async def test_legacy_access_groups_failure_shows_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_URL}/access/groups/{USER_ID}", status=503)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["errors"] == {"base": "cannot_connect"}


async def test_legacy_unexpected_error_shows_unknown(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await _start_legacy(hass)

    with patch(
        "custom_components.tidepool.config_flow.LegacyAuth.async_headers",
        side_effect=RuntimeError(f"boom {EMAIL} {PASSWORD}"),
    ):
        result = await _submit_legacy(hass, result["flow_id"])

    assert result["errors"] == {"base": "unknown"}
    formatter = logging.Formatter()
    for record in caplog.records:
        text = formatter.format(record)
        assert PASSWORD not in text
        assert EMAIL not in text


async def test_patient_selection_lists_shared_accounts(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    _mock_login(aioclient_mock)
    _mock_account(
        aioclient_mock, shared={PATIENT_ID: "Fake Patient", NAMELESS_ID: None}
    )
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["step_id"] == "patients"
    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": "Fake Owner"},
        {"value": PATIENT_ID, "label": "Fake Patient"},
        {"value": NAMELESS_ID, "label": NAMELESS_ID},
    ]
    assert _schema_default(result, CONF_PATIENTS) == [USER_ID]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PATIENTS: [USER_ID, PATIENT_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PATIENTS] == [USER_ID, PATIENT_ID]


async def test_patient_selection_requires_one(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await _start_legacy(hass)
    result = await _submit_legacy(hass, result["flow_id"])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PATIENTS: []}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_patients"}


async def test_duplicate_login_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _legacy_entry(hass)
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await _start_legacy(hass)

    result = await _submit_legacy(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- OAuth -----------------------------------------------------------------


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_oauth_flow_creates_entry(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    keycloak: MockKeycloak,
    mock_setup_entry: AsyncMock,
) -> None:
    _mock_account(aioclient_mock, shared={PATIENT_ID: "Fake Patient"})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )
    authorize = URL(result["url"])
    assert authorize.query["redirect_uri"] == (
        "https://my.home-assistant.io/redirect/oauth"
    )
    assert authorize.query["code_challenge_method"] == "S256"

    result = await _complete_oauth(hass, hass_client_no_auth, keycloak, result)

    # The code was exchanged once, with the PKCE verifier.
    [request] = keycloak.token_requests
    assert request["grant_type"] == "authorization_code"
    assert request["code_verifier"]
    # The patient list was read with the new access token.
    groups_call = next(
        call for call in aioclient_mock.mock_calls if "/access/groups/" in str(call[1])
    )
    assert groups_call[3][OAUTH_TOKEN_HEADER] == keycloak.issued_access_tokens[-1]

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "patients"
    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": "Fake Owner"},
        {"value": PATIENT_ID, "label": "Fake Patient"},
    ]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PATIENTS: [USER_ID]}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == USER_ID
    assert entry.title == "Fake Owner"
    assert entry.data[CONF_AUTH_METHOD] == "oauth"
    assert entry.data["auth_implementation"] == DOMAIN
    assert entry.data[CONF_PATIENTS] == [USER_ID]
    token = entry.data["token"]
    assert token["access_token"] == keycloak.issued_access_tokens[-1]
    assert token["refresh_token"] == keycloak.issued_refresh_tokens[-1]
    assert token["expires_at"] > time.time()
    assert "email" not in entry.data
    assert "password" not in entry.data
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_oauth_duplicate_login_aborts(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    keycloak: MockKeycloak,
) -> None:
    _oauth_entry(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "oauth"}
    )

    result = await _complete_oauth(hass, hass_client_no_auth, keycloak, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- reauth ----------------------------------------------------------------


async def test_reauth_legacy_updates_password(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    entry = _legacy_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    # A wrong password keeps the form open.
    _mock_login(aioclient_mock, status=401)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": "fake-still-wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    _mock_login(aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": NEW_PASSWORD}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == NEW_PASSWORD
    assert entry.data["email"] == EMAIL
    assert entry.data[CONF_PATIENTS] == [USER_ID]


async def test_reauth_legacy_wrong_account_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _legacy_entry(hass)
    result = await entry.start_reauth_flow(hass)
    _mock_login(aioclient_mock, user_id=OTHER_USER_ID)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["password"] == PASSWORD


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_reauth_oauth_updates_token(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    keycloak: MockKeycloak,
    mock_setup_entry: AsyncMock,
) -> None:
    entry = _oauth_entry(hass)
    old_token = entry.data["token"]
    result = await entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _complete_oauth(hass, hass_client_no_auth, keycloak, result)
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.unique_id == USER_ID
    token = entry.data["token"]
    assert token != old_token
    assert token["refresh_token"] == keycloak.issued_refresh_tokens[-1]
    assert entry.data["auth_implementation"] == DOMAIN
    assert entry.data[CONF_PATIENTS] == [USER_ID]


@pytest.mark.usefixtures("current_request_with_host", "credentials")
async def test_reauth_oauth_wrong_account_aborts(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    keycloak: MockKeycloak,
) -> None:
    entry = _oauth_entry(hass)
    old_token = entry.data["token"]
    keycloak.user_id = OTHER_USER_ID
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})

    result = await _complete_oauth(hass, hass_client_no_auth, keycloak, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["token"] == old_token


# -- options ---------------------------------------------------------------


async def test_options_change_interval_and_patients(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    entry = _legacy_entry(hass)
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock, shared={PATIENT_ID: "Fake Patient"})

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": "Fake Owner"},
        {"value": PATIENT_ID, "label": "Fake Patient"},
    ]
    assert _schema_default(result, CONF_PATIENTS) == [USER_ID]
    assert _schema_default(result, CONF_FAST_INTERVAL) == 5
    interval = _schema_value(result, CONF_FAST_INTERVAL)
    assert isinstance(interval, NumberSelector)
    assert interval.config["min"] == 2
    assert interval.config["max"] == 30

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_PATIENTS: [USER_ID, PATIENT_ID], CONF_FAST_INTERVAL: 10},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == {
        CONF_PATIENTS: [USER_ID, PATIENT_ID],
        CONF_FAST_INTERVAL: 10,
    }
    # The standard options flow reload set the entry up again.
    assert len(mock_setup_entry.mock_calls) == 1


async def test_options_reject_out_of_range_interval(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _legacy_entry(hass)
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await hass.config_entries.options.async_init(entry.entry_id)

    with pytest.raises(InvalidData, match="fast_interval"):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_PATIENTS: [USER_ID], CONF_FAST_INTERVAL: 31}
        )


async def test_options_fall_back_to_selected_patients_when_offline(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _legacy_entry(hass, patients=[USER_ID, PATIENT_ID], fast_interval=7)
    _mock_login(aioclient_mock, status=503)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": USER_ID},
        {"value": PATIENT_ID, "label": PATIENT_ID},
    ]
    assert _schema_default(result, CONF_PATIENTS) == [USER_ID, PATIENT_ID]
    assert _schema_default(result, CONF_FAST_INTERVAL) == 7
