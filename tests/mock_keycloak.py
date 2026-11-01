"""A small mock of Tidepool's Keycloak login server on top of ``aioclient_mock``.

Serves the OpenID discovery document and the token endpoint. The token endpoint
supports the ``authorization_code`` grant (with a PKCE ``S256`` check against
the challenge given to ``issue_code``) and the ``refresh_token`` grant with
rotating refresh tokens: every refresh returns a NEW refresh token and the old
one is rejected afterwards with HTTP 400 ``{"error": "invalid_grant"}``.

Every token and code is a synthetic fake. Tokens are unsigned JWT-shaped
strings so code that reads their claims can be exercised.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from http import HTTPStatus
from typing import Any

from aiohttp import ClientConnectionError
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tidepool.const import AUTH_URL

DISCOVERY_URL = f"{AUTH_URL}/.well-known/openid-configuration"
DOCUMENTED_AUTHORIZE_URL = f"{AUTH_URL}/protocol/openid-connect/auth"
DOCUMENTED_TOKEN_URL = f"{AUTH_URL}/protocol/openid-connect/token"

FAKE_USER_ID = "fake-user-0001"
ACCESS_TOKEN_LIFETIME = 600

DISCOVERY_FAILURES = (
    "http_500",
    "connection",
    "not_json",
    "missing_endpoints",
    "insecure_endpoints",
)


def b64url(data: bytes) -> str:
    """Encode bytes as unpadded base64url."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def make_unsigned_jwt(claims: dict[str, Any]) -> str:
    """Build a synthetic, unsigned (``alg: none``) JWT carrying ``claims``."""
    header = b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = b64url(json.dumps(claims).encode())
    return f"{header}.{payload}.fake-signature"


class MockKeycloak:
    """Fake login server state plus its ``aioclient_mock`` registrations."""

    def __init__(
        self,
        aioclient_mock: AiohttpClientMocker,
        *,
        authorize_url: str = DOCUMENTED_AUTHORIZE_URL,
        token_url: str = DOCUMENTED_TOKEN_URL,
        user_id: str = FAKE_USER_ID,
    ) -> None:
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.user_id = user_id
        # One of DISCOVERY_FAILURES, or None for a working discovery document.
        self.discovery_failure: str | None = None
        # When True, the token endpoint fails as if the network were down.
        self.token_connection_error = False
        # When set, the token endpoint suspends until the event is set.
        self.token_gate: asyncio.Event | None = None
        # Set whenever a token request has arrived at the endpoint.
        self.token_request_seen = asyncio.Event()
        self.token_requests: list[dict[str, Any]] = []
        self.issued_access_tokens: list[str] = []
        self.issued_refresh_tokens: list[str] = []
        self._codes: dict[str, str] = {}
        self._valid_refresh_tokens: set[str] = set()
        self._serial = 0
        aioclient_mock.get(DISCOVERY_URL, side_effect=self._discovery)
        aioclient_mock.post(token_url, side_effect=self._token)

    # -- helpers for tests -------------------------------------------------

    def issue_code(self, code_challenge: str) -> str:
        """Pretend the user signed in; return a code bound to the challenge."""
        self._serial += 1
        code = f"fake-code-{self._serial}"
        self._codes[code] = code_challenge
        return code

    def issue_refresh_token(self) -> str:
        """Create a valid refresh token, as if from an earlier sign-in."""
        self._serial += 1
        token = f"fake-refresh-{self._serial}"
        self._valid_refresh_tokens.add(token)
        self.issued_refresh_tokens.append(token)
        return token

    def is_refresh_token_valid(self, token: str) -> bool:
        """Return whether the server would accept this refresh token now."""
        return token in self._valid_refresh_tokens

    # -- endpoints ---------------------------------------------------------

    async def _discovery(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        failure = self.discovery_failure
        if failure == "http_500":
            return _response(method, url, HTTPStatus.INTERNAL_SERVER_ERROR, {})
        if failure == "connection":
            return AiohttpClientMockResponse(
                method, url, exc=ClientConnectionError("fake connection failure")
            )
        if failure == "not_json":
            return AiohttpClientMockResponse(method, url, text="<html>oops</html>")
        if failure == "missing_endpoints":
            return _response(method, url, HTTPStatus.OK, {"issuer": AUTH_URL})
        if failure == "insecure_endpoints":
            return _response(
                method,
                url,
                HTTPStatus.OK,
                {
                    "issuer": AUTH_URL,
                    "authorization_endpoint": "http://evil.invalid/auth",
                    "token_endpoint": "http://evil.invalid/token",
                },
            )
        return _response(
            method,
            url,
            HTTPStatus.OK,
            {
                "issuer": AUTH_URL,
                "authorization_endpoint": self.authorize_url,
                "token_endpoint": self.token_url,
                "code_challenge_methods_supported": ["plain", "S256"],
            },
        )

    async def _token(
        self, method: str, url: URL, data: Any
    ) -> AiohttpClientMockResponse:
        form = dict(data or {})
        self.token_requests.append(form)
        self.token_request_seen.set()
        if self.token_gate is not None:
            await self.token_gate.wait()
        if self.token_connection_error:
            return AiohttpClientMockResponse(
                method, url, exc=ClientConnectionError("fake connection failure")
            )

        grant = form.get("grant_type")
        if grant == "authorization_code":
            challenge = self._codes.pop(form.get("code", ""), None)
            verifier = form.get("code_verifier", "")
            if challenge is None or _s256(verifier) != challenge:
                return _invalid_grant(method, url)
            return _response(method, url, HTTPStatus.OK, self._new_tokens())
        if grant == "refresh_token":
            old = form.get("refresh_token", "")
            if old not in self._valid_refresh_tokens:
                return _invalid_grant(method, url)
            self._valid_refresh_tokens.discard(old)
            return _response(method, url, HTTPStatus.OK, self._new_tokens())
        return _response(
            method, url, HTTPStatus.BAD_REQUEST, {"error": "unsupported_grant_type"}
        )

    def _new_tokens(self) -> dict[str, Any]:
        self._serial += 1
        claims = {"sub": self.user_id, "jti": f"fake-{self._serial}"}
        access = make_unsigned_jwt({**claims, "typ": "Bearer"})
        refresh = f"fake-refresh-{self._serial}"
        self._valid_refresh_tokens.add(refresh)
        self.issued_access_tokens.append(access)
        self.issued_refresh_tokens.append(refresh)
        return {
            "access_token": access,
            "id_token": make_unsigned_jwt({**claims, "typ": "ID"}),
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_LIFETIME,
            "scope": "openid email offline_access",
        }


def _s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def _response(
    method: str, url: URL, status: int, body: dict[str, Any]
) -> AiohttpClientMockResponse:
    return AiohttpClientMockResponse(
        method,
        url,
        status=status,
        json=body,
        headers={"Content-Type": "application/json"},
    )


def _invalid_grant(method: str, url: URL) -> AiohttpClientMockResponse:
    return _response(method, url, HTTPStatus.BAD_REQUEST, {"error": "invalid_grant"})
