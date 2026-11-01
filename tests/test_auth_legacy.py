"""Tests for the legacy email and password auth strategy."""

import asyncio
import base64
import logging

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.api import (
    AuthError,
    RateLimitedError,
    TidepoolError,
)
from custom_components.tidepool.auth import LegacyAuth, LegacyLoginWithdrawnError
from custom_components.tidepool.const import API_URL

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
TOKEN = "tok1"
USER_ID = "u1"
LOGIN_URL = f"{API_URL}/auth/login"
CREDENTIALS = base64.b64encode(f"{EMAIL}:{PASSWORD}".encode()).decode()


@pytest.fixture
def auth(hass: HomeAssistant) -> LegacyAuth:
    return LegacyAuth(async_get_clientsession(hass), EMAIL, PASSWORD)


def _mock_login_ok(aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(
        LOGIN_URL,
        json={"userid": USER_ID},
        headers={"x-tidepool-session-token": TOKEN},
    )


async def test_login_gets_token_and_user_id(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    _mock_login_ok(aioclient_mock)
    assert auth.user_id is None

    headers = await auth.async_headers()

    assert headers == {"X-Tidepool-Session-Token": TOKEN}
    assert auth.user_id == USER_ID
    assert aioclient_mock.call_count == 1
    method, url, _data, request_headers = aioclient_mock.mock_calls[0]
    assert method == "POST"
    assert url.path == "/auth/login"
    assert request_headers["Authorization"] == f"Basic {CREDENTIALS}"


async def test_token_reused_until_invalidated(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    _mock_login_ok(aioclient_mock)

    await auth.async_headers()
    await auth.async_headers()
    assert aioclient_mock.call_count == 1

    await auth.async_invalidate()
    headers = await auth.async_headers()

    assert aioclient_mock.call_count == 2
    assert headers == {"X-Tidepool-Session-Token": TOKEN}


async def test_concurrent_callers_share_one_login(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    _mock_login_ok(aioclient_mock)

    first, second = await asyncio.gather(auth.async_headers(), auth.async_headers())

    assert first == second == {"X-Tidepool-Session-Token": TOKEN}
    assert aioclient_mock.call_count == 1


async def test_wrong_password_raises_auth_error(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(LOGIN_URL, status=401)

    with pytest.raises(AuthError) as excinfo:
        await auth.async_headers()

    assert not isinstance(excinfo.value, LegacyLoginWithdrawnError)
    assert auth.user_id is None


@pytest.mark.parametrize("status", [404, 410])
async def test_withdrawn_endpoint_raises_withdrawn(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth, status: int
) -> None:
    aioclient_mock.post(LOGIN_URL, status=status)

    with pytest.raises(LegacyLoginWithdrawnError):
        await auth.async_headers()


async def test_withdrawn_when_ok_without_token_header(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(LOGIN_URL, json={"userid": USER_ID})

    with pytest.raises(LegacyLoginWithdrawnError):
        await auth.async_headers()


@pytest.mark.parametrize("body", [{}, {"userid": ""}, ["not", "an", "object"]])
async def test_withdrawn_when_ok_without_user_id(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth, body: object
) -> None:
    aioclient_mock.post(
        LOGIN_URL, json=body, headers={"x-tidepool-session-token": TOKEN}
    )

    with pytest.raises(LegacyLoginWithdrawnError):
        await auth.async_headers()

    assert auth.user_id is None


async def test_withdrawn_when_ok_with_non_json_body(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(
        LOGIN_URL,
        text="<html>gone</html>",
        headers={"x-tidepool-session-token": TOKEN},
    )

    with pytest.raises(LegacyLoginWithdrawnError):
        await auth.async_headers()


async def test_rate_limited_and_other_statuses(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(LOGIN_URL, status=429, headers={"Retry-After": "7"})
    with pytest.raises(RateLimitedError) as rate_limited:
        await auth.async_headers()
    assert rate_limited.value.retry_after == 7

    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, status=500)
    with pytest.raises(TidepoolError) as server_error:
        await auth.async_headers()
    assert type(server_error.value) is TidepoolError


async def test_one_login_per_cycle_no_retry_loop(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(LOGIN_URL, status=401)

    for attempt in range(1, 4):
        with pytest.raises(AuthError):
            await auth.async_headers()
        assert aioclient_mock.call_count == attempt


@pytest.mark.parametrize("error", [aiohttp.ClientError(), TimeoutError()])
async def test_network_failure_wrapped_without_secrets(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth, error: Exception
) -> None:
    aioclient_mock.post(LOGIN_URL, exc=error)

    with pytest.raises(TidepoolError) as excinfo:
        await auth.async_headers()

    assert type(excinfo.value) is TidepoolError
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__
    message = str(excinfo.value)
    for secret in (EMAIL, PASSWORD, CREDENTIALS, API_URL, "api.tidepool.org"):
        assert secret not in message
    assert aioclient_mock.call_count == 1


async def test_failed_login_leaves_no_cached_token(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    aioclient_mock.post(LOGIN_URL, status=500)
    with pytest.raises(TidepoolError):
        await auth.async_headers()

    aioclient_mock.clear_requests()
    _mock_login_ok(aioclient_mock)
    assert await auth.async_headers() == {"X-Tidepool-Session-Token": TOKEN}


async def test_password_never_logged(
    aioclient_mock: AiohttpClientMocker,
    auth: LegacyAuth,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    _mock_login_ok(aioclient_mock)
    await auth.async_headers()
    await auth.async_invalidate()

    aioclient_mock.clear_requests()
    aioclient_mock.post(LOGIN_URL, status=401)
    with pytest.raises(AuthError):
        await auth.async_headers()

    for record in caplog.records:
        text = record.getMessage()
        assert PASSWORD not in text
        assert CREDENTIALS not in text
        assert EMAIL not in text
        assert TOKEN not in text


async def test_secrets_not_in_repr_or_error_messages(
    aioclient_mock: AiohttpClientMocker, auth: LegacyAuth
) -> None:
    for text in (repr(auth), str(auth)):
        assert PASSWORD not in text
        assert EMAIL not in text
        assert CREDENTIALS not in text

    for status in (401, 404, 429, 500):
        aioclient_mock.clear_requests()
        aioclient_mock.post(LOGIN_URL, status=status)
        with pytest.raises(TidepoolError) as excinfo:
            await auth.async_headers()
        message = str(excinfo.value)
        assert PASSWORD not in message
        assert EMAIL not in message
        assert CREDENTIALS not in message
