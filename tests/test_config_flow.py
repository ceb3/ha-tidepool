"""Tests for the config flow (email and password), reauth and the options flow."""

from __future__ import annotations

import logging
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers.selector import NumberSelector, SelectSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import (
    API_URL,
    CONF_FAST_INTERVAL,
    CONF_PATIENTS,
    DOMAIN,
    PASSWORD_DISCLAIMER,
)

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
NEW_PASSWORD = "fake-password-correct-horse"
SESSION_TOKEN = "fake-session-token-1"
USER_ID = "fake-user-0001"
PATIENT_ID = "fake-patient-0002"
NAMELESS_ID = "fake-patient-0003"
OTHER_USER_ID = "fake-other-0009"
LOGIN_URL = f"{API_URL}/auth/login"


EXPECTED_DISCLAIMER = (
    "This method signs in with your Tidepool email and password using the login "
    "that Tidepool documents for developers. Tidepool recommends OAuth for apps, "
    "may change or restrict this login at any time without notice, and it will "
    "not work with accounts that require two-factor authentication. Your "
    "password is stored in Home Assistant's config storage so the integration "
    "can sign in again."
)


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep entry setup out of the flow tests."""
    with patch(
        "custom_components.tidepool.async_setup_entry",
        new_callable=AsyncMock,
        return_value=True,
    ) as mock:
        yield mock


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


async def _start_user(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _submit_user(hass: HomeAssistant, flow_id: str) -> dict[str, Any]:
    return await hass.config_entries.flow.async_configure(
        flow_id, {"email": EMAIL, "password": PASSWORD}
    )


def _entry(hass: HomeAssistant, **options: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="Fake Owner",
        data={
            "email": EMAIL,
            "password": PASSWORD,
            CONF_PATIENTS: [USER_ID],
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


# -- sign-in ---------------------------------------------------------------


async def test_user_form_shows_disclaimer(hass: HomeAssistant) -> None:
    result = await _start_user(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["description_placeholders"]["disclaimer"] == EXPECTED_DISCLAIMER
    assert PASSWORD_DISCLAIMER == EXPECTED_DISCLAIMER
    assert result["errors"] in (None, {})


async def test_user_success_creates_entry(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])
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
        "email": EMAIL,
        "password": PASSWORD,
        CONF_PATIENTS: [USER_ID],
    }
    assert len(mock_setup_entry.mock_calls) == 1


async def test_user_wrong_password_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock, status=401)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}
    assert result["description_placeholders"]["disclaimer"] == EXPECTED_DISCLAIMER


async def test_user_withdrawn_shows_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock, status=410)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "login_withdrawn"}


@pytest.mark.parametrize(
    ("status", "error"),
    [(429, "cannot_connect"), (500, "cannot_connect")],
)
async def test_user_server_errors_show_cannot_connect(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    status: int,
    error: str,
) -> None:
    _mock_login(aioclient_mock, status=status)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

    assert result["errors"] == {"base": error}


async def test_user_access_groups_failure_shows_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _mock_login(aioclient_mock)
    aioclient_mock.get(f"{API_URL}/access/groups/{USER_ID}", status=503)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_unexpected_error_shows_unknown(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    result = await _start_user(hass)

    with patch(
        "custom_components.tidepool.config_flow.PasswordAuth.async_headers",
        side_effect=RuntimeError(f"boom {EMAIL} {PASSWORD}"),
    ):
        result = await _submit_user(hass, result["flow_id"])

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
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

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
    result = await _start_user(hass)
    result = await _submit_user(hass, result["flow_id"])

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PATIENTS: []}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "no_patients"}


async def test_duplicate_login_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    _entry(hass)
    _mock_login(aioclient_mock)
    _mock_account(aioclient_mock)
    result = await _start_user(hass)

    result = await _submit_user(hass, result["flow_id"])

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


# -- reauth ----------------------------------------------------------------


async def test_reauth_updates_password(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    entry = _entry(hass)
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


async def test_reauth_wrong_account_aborts(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = _entry(hass)
    result = await entry.start_reauth_flow(hass)
    _mock_login(aioclient_mock, user_id=OTHER_USER_ID)

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"password": NEW_PASSWORD}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data["password"] == PASSWORD


# -- options ---------------------------------------------------------------


async def test_options_change_interval_and_patients(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_setup_entry: AsyncMock,
) -> None:
    entry = _entry(hass)
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
    entry = _entry(hass)
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
    entry = _entry(hass, patients=[USER_ID, PATIENT_ID], fast_interval=7)
    _mock_login(aioclient_mock, status=503)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": USER_ID},
        {"value": PATIENT_ID, "label": PATIENT_ID},
    ]
    assert _schema_default(result, CONF_PATIENTS) == [USER_ID, PATIENT_ID]
    assert _schema_default(result, CONF_FAST_INTERVAL) == 7


class _StubClient:
    """Stands in for the loaded entry's shared client (``runtime_data.client``)."""

    def __init__(self, groups: dict[str, Any], names: dict[str, str]) -> None:
        self._groups = groups
        self._names = names
        self.calls: list[tuple[str, str]] = []

    async def access_groups(self, user_id: str) -> dict[str, Any]:
        self.calls.append(("access_groups", user_id))
        return self._groups

    async def profile(self, user_id: str) -> dict[str, Any]:
        self.calls.append(("profile", user_id))
        return {"fullName": self._names[user_id]}


async def test_options_loaded_prefers_shared_client(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        state=ConfigEntryState.LOADED,
        data={
            "email": EMAIL,
            "password": PASSWORD,
            CONF_PATIENTS: [USER_ID],
        },
    )
    entry.add_to_hass(hass)
    stub = _StubClient({USER_ID: {"root": {}}}, {USER_ID: "Fake Owner"})
    entry.runtime_data = SimpleNamespace(client=stub)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert _select_options(result, CONF_PATIENTS) == [
        {"value": USER_ID, "label": "Fake Owner"},
    ]
    assert stub.calls
    assert aioclient_mock.call_count == 0  # no second login
