"""Tests for the Tidepool API client."""

from datetime import UTC, datetime

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.tidepool.api import (
    ApiShapeError,
    AuthError,
    NotFoundError,
    RateLimitedError,
    TidepoolClient,
    TidepoolError,
)
from custom_components.tidepool.const import API_URL

TOKEN = "fake-session-token"
USER_ID = "fake-user-id"


class FakeAuth:
    """Synthetic auth strategy that counts invalidations."""

    user_id: str | None = USER_ID

    def __init__(self) -> None:
        self.invalidations = 0

    async def async_headers(self) -> dict[str, str]:
        return {"X-Tidepool-Session-Token": TOKEN}

    async def async_invalidate(self) -> None:
        self.invalidations += 1


@pytest.fixture
def auth() -> FakeAuth:
    return FakeAuth()


@pytest.fixture
def client(hass: HomeAssistant, auth: FakeAuth) -> TidepoolClient:
    return TidepoolClient(async_get_clientsession(hass), auth)


async def test_latest_sends_token_header_and_latest_param(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", json=[{"type": "cbg"}])

    result = await client.latest(USER_ID)

    assert result == [{"type": "cbg"}]
    assert aioclient_mock.call_count == 1
    _method, url, _data, headers = aioclient_mock.mock_calls[0]
    assert url.path == f"/data/{USER_ID}"
    assert url.query["latest"] == "true"
    assert headers["X-Tidepool-Session-Token"] == TOKEN


async def test_data_query_has_types_and_iso_dates(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", json=[])

    start = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC)
    assert await client.data(USER_ID, ["cbg", "basal"], start, end) == []

    url = aioclient_mock.mock_calls[0][1]
    assert url.query["type"] == "cbg,basal"
    assert url.query["startDate"] == "2026-11-01T00:00:00.000Z"
    assert url.query["endDate"] == "2026-11-01T23:59:59.000Z"


async def test_401_invalidates_auth_and_raises_auth_error(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient, auth: FakeAuth
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", status=401)

    with pytest.raises(AuthError):
        await client.latest(USER_ID)

    assert auth.invalidations == 1
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_error_carries_status_not_from_login(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient, status: int
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", status=status)

    with pytest.raises(AuthError) as excinfo:
        await client.latest(USER_ID)

    assert excinfo.value.status == status
    assert excinfo.value.from_login is False


def test_auth_error_defaults() -> None:
    err = AuthError("rejected")

    assert err.status is None
    assert err.from_login is False


async def test_429_raises_rate_limited_with_retry_after(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(
        f"{API_URL}/data/{USER_ID}", status=429, headers={"Retry-After": "30"}
    )

    with pytest.raises(RateLimitedError) as excinfo:
        await client.latest(USER_ID)

    assert excinfo.value.retry_after == 30.0
    assert aioclient_mock.call_count == 1


async def test_429_without_retry_after_has_none(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", status=429)

    with pytest.raises(RateLimitedError) as excinfo:
        await client.latest(USER_ID)

    assert excinfo.value.retry_after is None


async def test_500_raises_tidepool_error(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient, auth: FakeAuth
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", status=500)

    with pytest.raises(TidepoolError) as excinfo:
        await client.latest(USER_ID)

    assert not isinstance(excinfo.value, AuthError | RateLimitedError)
    assert "500" in str(excinfo.value)
    assert USER_ID not in str(excinfo.value)
    assert auth.invalidations == 0
    assert aioclient_mock.call_count == 1


async def test_404_raises_not_found_error(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient, auth: FakeAuth
) -> None:
    aioclient_mock.get(f"{API_URL}/metadata/{USER_ID}/profile", status=404)

    with pytest.raises(NotFoundError) as excinfo:
        await client.profile(USER_ID)

    assert "404" in str(excinfo.value)
    assert USER_ID not in str(excinfo.value)
    assert auth.invalidations == 0


async def test_non_list_payload_raises_shape_error(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", json={"not": "a list"})

    with pytest.raises(ApiShapeError):
        await client.latest(USER_ID)


async def test_other_endpoints_use_documented_paths(
    aioclient_mock: AiohttpClientMocker, client: TidepoolClient
) -> None:
    aioclient_mock.get(f"{API_URL}/v1/summaries/cgm/{USER_ID}", json={"a": 1})
    aioclient_mock.get(f"{API_URL}/access/groups/{USER_ID}", json={"b": 2})
    aioclient_mock.get(f"{API_URL}/metadata/{USER_ID}/profile", json={"c": 3})
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sources", json=[{"d": 4}])
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sets", json=[{"e": 5}])

    assert await client.summary(USER_ID, "cgm") == {"a": 1}
    assert await client.access_groups(USER_ID) == {"b": 2}
    assert await client.profile(USER_ID) == {"c": 3}
    assert await client.data_sources(USER_ID) == [{"d": 4}]
    assert await client.data_sets(USER_ID) == [{"e": 5}]
