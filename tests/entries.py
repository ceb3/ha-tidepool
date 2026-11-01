"""Synthetic config entries and API mocks shared by the setup and coordinator tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.const import (
    API_URL,
    CONF_AUTH_METHOD,
    CONF_PATIENTS,
    DOMAIN,
)

from .mock_keycloak import FAKE_USER_ID, make_unsigned_jwt

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
SESSION_TOKEN = "fake-session-token-1"
USER_ID = FAKE_USER_ID
PATIENT_ID = "fake-patient-0002"
LOGIN_URL = f"{API_URL}/auth/login"

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def latest_url(user_id: str) -> str:
    return f"{API_URL}/data/{user_id}"


def mock_login(aioclient_mock: AiohttpClientMocker, status: int = 200) -> None:
    if status == 200:
        aioclient_mock.post(
            LOGIN_URL,
            json={"userid": USER_ID},
            headers={"x-tidepool-session-token": SESSION_TOKEN},
        )
    else:
        aioclient_mock.post(LOGIN_URL, status=status)


def mock_latest(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    payload: list[dict] | None = None,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    if status == 200:
        aioclient_mock.get(
            latest_url(user_id),
            json=load_fixture("latest.json") if payload is None else payload,
        )
    else:
        aioclient_mock.get(latest_url(user_id), status=status, headers=headers)


def mock_profile(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    full_name: str | None = "Fake Owner",
) -> None:
    """Mock a user's profile; a ``None`` name means the user has no profile."""
    url = f"{API_URL}/metadata/{user_id}/profile"
    if full_name is None:
        aioclient_mock.get(url, status=404)
    else:
        aioclient_mock.get(url, json={"fullName": full_name})


def login_posts(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url) == LOGIN_URL
    )


def latest_gets(aioclient_mock: AiohttpClientMocker, user_id: str) -> int:
    return sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "get" and url.path == f"/data/{user_id}"
    )


def legacy_entry(
    hass: HomeAssistant,
    patients: list[str] | None = None,
    **options: Any,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="Fake Owner",
        data={
            CONF_AUTH_METHOD: "legacy",
            "email": EMAIL,
            "password": PASSWORD,
            CONF_PATIENTS: patients or [USER_ID],
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry


def oauth_entry(
    hass: HomeAssistant, patients: list[str] | None = None
) -> MockConfigEntry:
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
            CONF_PATIENTS: patients or [USER_ID],
        },
    )
    entry.add_to_hass(hass)
    return entry
