"""Tests for the OAuth2 PKCE auth strategy and application credentials hooks."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import pytest
from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import OAuth2TokenRequestReauthError
from homeassistant.helpers.config_entry_oauth2_flow import (
    MY_AUTH_CALLBACK_PATH,
    LocalOAuth2ImplementationWithPkce,
)
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from yarl import URL

from custom_components.tidepool.api import AuthError, TidepoolError
from custom_components.tidepool.application_credentials import (
    async_get_auth_implementation,
    async_get_authorization_server,
)
from custom_components.tidepool.auth import OAuthAuth, extract_user_id
from custom_components.tidepool.const import AUTH_URL, DOMAIN, OAUTH_TOKEN_HEADER

from .mock_keycloak import (
    DISCOVERY_FAILURES,
    DOCUMENTED_AUTHORIZE_URL,
    DOCUMENTED_TOKEN_URL,
    FAKE_USER_ID,
    MockKeycloak,
    b64url,
    make_unsigned_jwt,
)

CLIENT_ID = "fake-client-id-xyz"
CLIENT_SECRET = "fake-client-secret-xyz"


@pytest.fixture
def keycloak(aioclient_mock: AiohttpClientMocker) -> MockKeycloak:
    return MockKeycloak(aioclient_mock)


@pytest.fixture
async def implementation(
    hass: HomeAssistant, keycloak: MockKeycloak
) -> LocalOAuth2ImplementationWithPkce:
    impl = await async_get_auth_implementation(
        hass, DOMAIN, ClientCredential(CLIENT_ID, "")
    )
    assert isinstance(impl, LocalOAuth2ImplementationWithPkce)
    return impl


def _entry(hass: HomeAssistant, token: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"auth_implementation": DOMAIN, "token": token}
    )
    entry.add_to_hass(hass)
    return entry


def _expired_token(refresh_token: str) -> dict[str, Any]:
    return {
        "access_token": make_unsigned_jwt({"sub": FAKE_USER_ID, "jti": "stale"}),
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_in": 600,
        "expires_at": time.time() - 60,
    }


async def _sign_in(
    keycloak: MockKeycloak, implementation: LocalOAuth2ImplementationWithPkce
) -> dict[str, Any]:
    """Run the authorize URL plus code exchange the way HA's flow does."""
    authorize = URL(await implementation.async_generate_authorize_url("flow-1"))
    code = keycloak.issue_code(authorize.query["code_challenge"])
    token = await implementation.async_resolve_external_data(
        {"code": code, "state": {"redirect_uri": MY_AUTH_CALLBACK_PATH}}
    )
    token["expires_at"] = time.time() + token["expires_in"]
    return token


@pytest.mark.parametrize("my_loaded", [False, True])
async def test_authorize_url_has_pkce_and_scopes(
    hass: HomeAssistant,
    implementation: LocalOAuth2ImplementationWithPkce,
    my_loaded: bool,
) -> None:
    # The redirect must be my.home-assistant.io whether or not HA's "my"
    # integration is loaded (HA's default would otherwise use the local URL).
    if my_loaded:
        hass.config.components.add("my")
    assert ("my" in hass.config.components) is my_loaded

    url = URL(await implementation.async_generate_authorize_url("flow-1"))

    assert str(url.with_query(None)) == DOCUMENTED_AUTHORIZE_URL
    query = url.query
    assert query["response_type"] == "code"
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == "https://my.home-assistant.io/redirect/oauth"
    assert query["code_challenge_method"] == "S256"
    expected_challenge = b64url(
        hashlib.sha256(implementation.code_verifier.encode("ascii")).digest()
    )
    assert query["code_challenge"] == expected_challenge
    assert query["scope"].split() == ["openid", "email", "offline_access"]
    assert "state" in query


async def test_token_exchange_stores_tokens(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
) -> None:
    token = await _sign_in(keycloak, implementation)

    assert token["access_token"] == keycloak.issued_access_tokens[-1]
    assert token["refresh_token"] == keycloak.issued_refresh_tokens[-1]
    [request] = keycloak.token_requests
    assert request["grant_type"] == "authorization_code"
    assert request["code_verifier"] == implementation.code_verifier
    assert request["client_id"] == CLIENT_ID
    assert request["redirect_uri"] == MY_AUTH_CALLBACK_PATH
    assert "client_secret" not in request  # public client

    entry = _entry(hass, token)
    auth = OAuthAuth(hass, entry, implementation)

    assert await auth.async_headers() == {OAUTH_TOKEN_HEADER: token["access_token"]}
    assert auth.user_id == FAKE_USER_ID
    assert len(keycloak.token_requests) == 1  # fresh token, no refresh


async def test_code_exchange_with_wrong_verifier_is_rejected(
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
) -> None:
    code = keycloak.issue_code("not-the-right-challenge")
    with pytest.raises(OAuth2TokenRequestReauthError):
        await implementation.async_resolve_external_data(
            {"code": code, "state": {"redirect_uri": MY_AUTH_CALLBACK_PATH}}
        )


async def test_refresh_persists_new_refresh_token(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
) -> None:
    old_refresh = keycloak.issue_refresh_token()
    entry = _entry(hass, _expired_token(old_refresh))
    auth = OAuthAuth(hass, entry, implementation)

    headers = await auth.async_headers()

    stored = entry.data["token"]
    assert keycloak.token_requests[-1]["grant_type"] == "refresh_token"
    assert keycloak.token_requests[-1]["refresh_token"] == old_refresh
    assert stored["refresh_token"] == keycloak.issued_refresh_tokens[-1]
    assert stored["refresh_token"] != old_refresh
    assert stored["access_token"] == keycloak.issued_access_tokens[-1]
    assert stored["expires_at"] > time.time() + 500
    assert headers == {OAUTH_TOKEN_HEADER: stored["access_token"]}

    # The server rotated: the old refresh token is now rejected.
    assert not keycloak.is_refresh_token_valid(old_refresh)
    with pytest.raises(OAuth2TokenRequestReauthError):
        await implementation.async_refresh_token({"refresh_token": old_refresh})

    # The next refresh uses the newest refresh token and persists the next one.
    newest_refresh = stored["refresh_token"]
    await auth.async_invalidate()
    await auth.async_headers()
    assert keycloak.token_requests[-1]["refresh_token"] == newest_refresh
    assert entry.data["token"]["refresh_token"] == keycloak.issued_refresh_tokens[-1]
    assert entry.data["token"]["refresh_token"] != newest_refresh


async def test_concurrent_refresh_single_request(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
) -> None:
    entry = _entry(hass, _expired_token(keycloak.issue_refresh_token()))
    auth = OAuthAuth(hass, entry, implementation)
    keycloak.token_gate = asyncio.Event()

    first = asyncio.create_task(auth.async_headers())
    await asyncio.wait_for(keycloak.token_request_seen.wait(), 1)
    # The first refresh is now suspended inside the token endpoint. Start the
    # second call and give it ample chances to run while the first is pending.
    second = asyncio.create_task(auth.async_headers())
    for _ in range(20):
        await asyncio.sleep(0)
    requests_while_pending = len(keycloak.token_requests)
    keycloak.token_gate.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert requests_while_pending == 1
    assert len(keycloak.token_requests) == 1
    expected = {OAUTH_TOKEN_HEADER: keycloak.issued_access_tokens[-1]}
    assert results == [expected, expected]


async def test_invalid_grant_raises_auth_error(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    revoked = "fake-refresh-revoked"
    entry = _entry(hass, _expired_token(revoked))
    auth = OAuthAuth(hass, entry, implementation)

    with pytest.raises(AuthError) as err:
        await auth.async_headers()

    assert entry.data["token"]["refresh_token"] == revoked
    assert err.value.from_login is True
    message = str(err.value)
    assert revoked not in message
    assert "http" not in message
    assert revoked not in caplog.text


async def test_network_failure_raises_tidepool_error(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    implementation: LocalOAuth2ImplementationWithPkce,
) -> None:
    refresh = keycloak.issue_refresh_token()
    entry = _entry(hass, _expired_token(refresh))
    auth = OAuthAuth(hass, entry, implementation)
    keycloak.token_connection_error = True

    with pytest.raises(TidepoolError) as err:
        await auth.async_headers()

    assert not isinstance(err.value, AuthError)
    message = str(err.value)
    assert refresh not in message
    assert "http" not in message
    assert entry.data["token"]["refresh_token"] == refresh


async def test_discovery_endpoints_are_used(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    MockKeycloak(
        aioclient_mock,
        authorize_url=f"{AUTH_URL}/discovered/auth",
        token_url=f"{AUTH_URL}/discovered/token",
    )

    server = await async_get_authorization_server(hass)

    assert server == AuthorizationServer(
        authorize_url=f"{AUTH_URL}/discovered/auth",
        token_url=f"{AUTH_URL}/discovered/token",
    )


@pytest.mark.parametrize("failure", DISCOVERY_FAILURES)
async def test_discovery_failure_falls_back_to_documented_urls(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    failure: str,
) -> None:
    keycloak = MockKeycloak(
        aioclient_mock,
        authorize_url=f"{AUTH_URL}/discovered/auth",
        token_url=f"{AUTH_URL}/discovered/token",
    )
    keycloak.discovery_failure = failure

    server = await async_get_authorization_server(hass)

    assert server == AuthorizationServer(
        authorize_url=DOCUMENTED_AUTHORIZE_URL, token_url=DOCUMENTED_TOKEN_URL
    )
    assert aioclient_mock.call_count == 1  # discovery was attempted first


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"userid": "a1", "user_id": "b2", "sub": "c3"}, "a1"),
        ({"user_id": "b2", "sub": "c3"}, "b2"),
        ({"sub": "c3"}, "c3"),
        ({"userid": "", "sub": "c3"}, "c3"),
        ({"userid": 42, "sub": "c3"}, "c3"),
        ({}, None),
    ],
)
def test_extract_user_id(claims: dict[str, Any], expected: str | None) -> None:
    assert extract_user_id(claims) == expected


async def test_user_id_from_unsigned_jwt(
    hass: HomeAssistant, implementation: LocalOAuth2ImplementationWithPkce
) -> None:
    token = _expired_token("fake-refresh-unused")
    token["id_token"] = make_unsigned_jwt({"sub": "from-id-token"})
    token["access_token"] = make_unsigned_jwt({"userid": "from-access-token"})
    assert OAuthAuth(hass, _entry(hass, token), implementation).user_id == (
        "from-id-token"
    )

    del token["id_token"]
    assert OAuthAuth(hass, _entry(hass, token), implementation).user_id == (
        "from-access-token"
    )

    token["access_token"] = "not-a-jwt"
    assert OAuthAuth(hass, _entry(hass, token), implementation).user_id is None


async def test_no_secrets_in_logs(
    hass: HomeAssistant,
    keycloak: MockKeycloak,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    implementation = await async_get_auth_implementation(
        hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET)
    )

    token = await _sign_in(keycloak, implementation)
    assert keycloak.token_requests[0]["client_secret"] == CLIENT_SECRET
    entry = _entry(hass, token)
    auth = OAuthAuth(hass, entry, implementation)
    await auth.async_invalidate()
    await auth.async_headers()  # rotating refresh
    old_refresh = token["refresh_token"]
    entry2 = _entry(hass, _expired_token(old_refresh))
    with pytest.raises(AuthError):
        await OAuthAuth(hass, entry2, implementation).async_headers()

    secrets = [
        *keycloak.issued_access_tokens,
        *keycloak.issued_refresh_tokens,
        *(r["code"] for r in keycloak.token_requests if "code" in r),
        implementation.code_verifier,
        CLIENT_ID,
        CLIENT_SECRET,
        FAKE_USER_ID,
    ]
    for secret in secrets:
        assert secret not in caplog.text
    assert repr(auth) == "<OAuthAuth>"
