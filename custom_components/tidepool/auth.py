"""Authentication strategies for the Tidepool API.

Legacy email and password login. The password, the email and the encoded
credential string are never logged and never appear in error messages or reprs;
network errors are replaced by messages that carry no URL, identifier or
credential.
"""

from __future__ import annotations

import asyncio
import base64

import aiohttp

from .api import (
    AuthError,
    RateLimitedError,
    TidepoolError,
    _parse_retry_after,
)
from .const import API_URL

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
