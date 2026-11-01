"""Async client for the Tidepool REST API.

The client performs no retries and no login. Authentication is delegated to an
``AuthStrategy`` that supplies the request headers. Error messages carry only
the HTTP status and a path pattern; tokens, user ids and bodies are never
logged or included.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import aiohttp

from .const import API_URL

_TOKEN_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.000Z"


class TidepoolError(Exception):
    """Base error for any failed Tidepool API call."""


class AuthError(TidepoolError):
    """The API rejected the credentials (HTTP 401 or 403).

    ``status`` is the HTTP status that caused it, when there was one.
    ``from_login`` is True when the login itself was rejected
    (wrong or revoked credentials), not a request made with a session.
    """

    def __init__(
        self, message: str, *, status: int | None = None, from_login: bool = False
    ) -> None:
        super().__init__(message)
        self.status = status
        self.from_login = from_login


class RateLimitedError(TidepoolError):
    """The API rate limit was hit (HTTP 429)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class NotFoundError(TidepoolError):
    """The API has nothing at the requested path (HTTP 404)."""


class ApiShapeError(TidepoolError):
    """The API returned a payload of an unexpected shape."""


class AuthStrategy(Protocol):
    """Supplies authentication headers for API requests."""

    user_id: str | None

    async def async_headers(self) -> dict[str, str]:
        """Return the full header dict that authenticates a request."""
        ...

    async def async_invalidate(self) -> None:
        """Discard cached credentials after the API rejected them."""
        ...


def _format_time(value: datetime) -> str:
    """Format a datetime as ``YYYY-MM-DDTHH:MM:SS.000Z`` in UTC."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.strftime(_TOKEN_TIME_FORMAT)


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header given in seconds."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class TidepoolClient:
    """Thin typed wrapper over the Tidepool REST API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        auth: AuthStrategy,
        base_url: str = API_URL,
    ) -> None:
        self._session = session
        self._auth = auth
        self._base_url = base_url.rstrip("/")

    @property
    def user_id(self) -> str | None:
        """Return the id of the signed-in account, once known."""
        return self._auth.user_id

    async def latest(
        self,
        user_id: str,
        record_type: str | None = None,
        sub_type: str | None = None,
    ) -> list[dict]:
        """Return the latest record of each type for a user.

        With a type and a subType, only the latest record of that subType.
        """
        params = {"latest": "true"}
        if record_type is not None:
            params["type"] = record_type
        if sub_type is not None:
            params["subType"] = sub_type
        result = await self._get(f"/data/{user_id}", "/data/{user_id}", params)
        return _expect_list(result)

    async def data(
        self,
        user_id: str,
        types: list[str],
        start: datetime,
        end: datetime,
        sub_type: str | None = None,
    ) -> list[dict]:
        """Return records of the given types between ``start`` and ``end``."""
        params = {
            "type": ",".join(types),
            "startDate": _format_time(start),
            "endDate": _format_time(end),
        }
        if sub_type is not None:
            params["subType"] = sub_type
        result = await self._get(f"/data/{user_id}", "/data/{user_id}", params)
        return _expect_list(result)

    async def summary(self, user_id: str, kind: str) -> dict:
        """Return a summary (for example ``cgm``) for a user."""
        result = await self._get(
            f"/v1/summaries/{kind}/{user_id}", "/v1/summaries/{kind}/{user_id}"
        )
        return _expect_dict(result)

    async def access_groups(self, user_id: str) -> dict:
        """Return the access groups visible to a user."""
        result = await self._get(
            f"/access/groups/{user_id}", "/access/groups/{user_id}"
        )
        return _expect_dict(result)

    async def profile(self, user_id: str) -> dict:
        """Return the profile metadata of a user."""
        result = await self._get(
            f"/metadata/{user_id}/profile", "/metadata/{user_id}/profile"
        )
        return _expect_dict(result)

    async def data_sources(self, user_id: str) -> list[dict]:
        """Return the data sources of a user."""
        result = await self._get(
            f"/v1/users/{user_id}/data_sources", "/v1/users/{user_id}/data_sources"
        )
        return _expect_list(result)

    async def data_sets(self, user_id: str) -> list[dict]:
        """Return the data sets of a user."""
        result = await self._get(
            f"/v1/users/{user_id}/data_sets", "/v1/users/{user_id}/data_sets"
        )
        return _expect_list(result)

    async def _get(
        self,
        path: str,
        pattern: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        """GET ``path`` and return parsed JSON, mapping failures to errors.

        ``pattern`` is the path with placeholders instead of identifiers and is
        the only form of the path that may appear in error messages.
        """
        headers = await self._auth.async_headers()
        try:
            async with self._session.get(
                f"{self._base_url}{path}", params=params, headers=headers
            ) as response:
                status = response.status
                if status in (401, 403):
                    await self._auth.async_invalidate()
                    raise AuthError(
                        f"GET {pattern} rejected credentials ({status})", status=status
                    )
                if status == 429:
                    raise RateLimitedError(
                        f"GET {pattern} rate limited ({status})",
                        parse_retry_after(response.headers.get("Retry-After")),
                    )
                if status == 404:
                    raise NotFoundError(f"GET {pattern} found nothing ({status})")
                if status != 200:
                    raise TidepoolError(f"GET {pattern} failed ({status})")
                try:
                    return await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as err:
                    raise ApiShapeError(
                        f"GET {pattern} returned a non-JSON body"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise TidepoolError(
                f"GET {pattern} failed ({type(err).__name__})"
            ) from None


def _expect_list(payload: Any) -> list[dict]:
    if not isinstance(payload, list):
        raise ApiShapeError("expected a JSON list")
    return payload


def _expect_dict(payload: Any) -> dict:
    if not isinstance(payload, dict):
        raise ApiShapeError("expected a JSON object")
    return payload
