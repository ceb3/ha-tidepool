"""Authentication strategies for the Tidepool API.

``OAuthAuth`` uses OAuth2 tokens stored on the config entry and refreshes them
with rotation. Tokens, codes and client credentials are never logged and never
appear in error messages.

Legacy email and password login. The password, the email and the encoded
credential string are never logged and never appear in error messages or reprs;
network errors are replaced by messages that carry no URL, identifier or
credential.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestBaseError,
    OAuth2TokenRequestReauthError,
)
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2Implementation,
    OAuth2Session,
)

from .api import (
    AuthError,
    RateLimitedError,
    TidepoolError,
    _parse_retry_after,
)
from .const import API_URL, OAUTH_TOKEN_HEADER

_LOGIN_PATH = "/auth/login"
_TOKEN_RESPONSE_HEADER = "x-tidepool-session-token"
_TOKEN_REQUEST_HEADER = "X-Tidepool-Session-Token"


class LegacyLoginWithdrawnError(TidepoolError):
    """The legacy email and password login no longer works as before.

    Raised for HTTP 404 and 410 and for a successful response that lacks the
    session token header or the user id.
    """


class LegacyAuth:
    """Session token auth using the legacy email and password login.

    Performs at most one login request per call and never retries. The token is
    cached until ``async_invalidate`` clears it.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        base_url: str = API_URL,
    ) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._lock = asyncio.Lock()
        self.user_id: str | None = None

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    async def async_headers(self) -> dict[str, str]:
        """Return the session token header, logging in first if needed."""
        async with self._lock:
            if self._token is None:
                self._token = await self._async_login()
            return {_TOKEN_REQUEST_HEADER: self._token}

    async def async_invalidate(self) -> None:
        """Discard the cached token so the next call logs in again."""
        async with self._lock:
            self._token = None

    async def _async_login(self) -> str:
        """Log in once, store the user id and return the session token."""
        credentials = base64.b64encode(
            f"{self._email}:{self._password}".encode()
        ).decode()
        try:
            async with self._session.post(
                f"{self._base_url}{_LOGIN_PATH}",
                headers={"Authorization": f"Basic {credentials}"},
            ) as response:
                status = response.status
                if status == 401:
                    raise AuthError("legacy login rejected credentials (401)")
                if status in (404, 410):
                    raise LegacyLoginWithdrawnError(
                        f"legacy login endpoint withdrawn ({status})"
                    )
                if status == 429:
                    raise RateLimitedError(
                        f"legacy login rate limited ({status})",
                        _parse_retry_after(response.headers.get("Retry-After")),
                    )
                if status != 200:
                    raise TidepoolError(f"legacy login failed ({status})")
                token = response.headers.get(_TOKEN_RESPONSE_HEADER)
                try:
                    body = await response.json(content_type=None)
                except ValueError, aiohttp.ContentTypeError:
                    body = None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TidepoolError(
                f"legacy login request failed ({type(err).__name__})"
            ) from None

        user_id = body.get("userid") if isinstance(body, dict) else None
        if not token or not isinstance(user_id, str) or not user_id:
            raise LegacyLoginWithdrawnError(
                "legacy login response lacks a session token or user id"
            )
        self.user_id = user_id
        return token


def oauth_token_headers(access_token: str) -> dict[str, str]:
    """Return the headers that carry an OAuth access token to the API.

    The only place that decides how the access token is sent (see
    ``const.OAUTH_TOKEN_HEADER``; Bearer vs. session token header is unverified).
    """
    return {OAUTH_TOKEN_HEADER: access_token}


def extract_user_id(claims: dict[str, Any]) -> str | None:
    """Pick the Tidepool user id from token claims.

    Which claim carries it is unverified; try ``userid``, then ``user_id``,
    then ``sub``. Override this function if a live check shows otherwise.
    """
    for key in ("userid", "user_id", "sub"):
        value = claims.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _unverified_jwt_claims(token: Any) -> dict[str, Any] | None:
    """Decode a JWT payload WITHOUT checking its signature (for ids only)."""
    if not isinstance(token, str) or token.count(".") != 2:
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        )
    except binascii.Error, ValueError:
        return None
    return claims if isinstance(claims, dict) else None


def token_user_id(token: dict[str, Any]) -> str | None:
    """Return the user id from an OAuth token response's id or access token."""
    for key in ("id_token", "access_token"):
        claims = _unverified_jwt_claims(token.get(key))
        if claims is not None and (user_id := extract_user_id(claims)):
            return user_id
    return None


class OAuthAuth:
    """Auth from OAuth2 tokens on a config entry, refreshed with rotation.

    Refreshes are serialised by a lock and the expiry is re-checked once the
    lock is held, so concurrent callers share one token request. Every refresh
    persists the token response, including the rotated refresh token, on the
    entry (via ``OAuth2Session``).
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        implementation: AbstractOAuth2Implementation,
    ) -> None:
        self._hass = hass
        self._entry = entry
        self._session = OAuth2Session(hass, entry, implementation)
        # OAuth2Session also locks internally today; this lock keeps the
        # one-request guarantee (and orders invalidate vs. refresh) without
        # relying on that HA internal.
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    @property
    def user_id(self) -> str | None:
        """Return the user id read from the id token or the access token."""
        return token_user_id(self._session.token)

    async def async_headers(self) -> dict[str, str]:
        """Return the access token header, refreshing the token first if needed."""
        async with self._lock:
            if not self._session.valid_token:
                await self._async_refresh()
            return oauth_token_headers(self._session.token["access_token"])

    async def async_invalidate(self) -> None:
        """Mark the access token expired so the next call refreshes it."""
        async with self._lock:
            token = {**self._session.token, "expires_at": 0}
            self._hass.config_entries.async_update_entry(
                self._entry, data={**self._entry.data, "token": token}
            )

    async def _async_refresh(self) -> None:
        """Refresh once; map failures to errors without URLs or tokens."""
        try:
            await self._session.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError:
            raise AuthError("OAuth token refresh was rejected") from None
        except OAuth2TokenRequestBaseError as err:
            raise TidepoolError(
                f"OAuth token refresh failed ({type(err).__name__})"
            ) from None
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TidepoolError(
                f"OAuth token refresh request failed ({type(err).__name__})"
            ) from None
