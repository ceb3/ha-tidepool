"""Synthetic config entries and API mocks shared by the setup and coordinator tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)
from yarl import URL

from custom_components.tidepool.const import (
    API_URL,
    CONF_PATIENTS,
    DOMAIN,
)

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
SESSION_TOKEN = "fake-session-token-1"
USER_ID = "fake-user-0001"
PATIENT_ID = "fake-patient-0002"
LOGIN_URL = f"{API_URL}/auth/login"

FIXTURES = Path(__file__).parent / "fixtures"

# The query that tells the latest request from the 24 h window request; both
# go to ``/data/{user_id}``, as do the slow coordinator's device event requests.
LATEST_QUERY = {"latest": "true"}
WINDOW_QUERY = {"type": "basal,bolus,food,deviceEvent"}
# The device event subtypes the slow coordinator asks for with ``latest``; a
# prime is asked for over a time range instead.
LATEST_DEVICE_EVENTS = ("alarm", "reservoirChange", "status")


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
    window: list[dict] | None = None,
    window_status: int = 200,
) -> None:
    """Mock a user's latest records and, by default, an empty 24 h window."""
    url = latest_url(user_id)
    if status == 200:
        aioclient_mock.get(
            url,
            params=LATEST_QUERY,
            json=load_fixture("latest.json") if payload is None else payload,
        )
    else:
        aioclient_mock.get(url, params=LATEST_QUERY, status=status, headers=headers)
    mock_window(aioclient_mock, user_id, window, window_status)


def mock_window(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    payload: list[dict] | None = None,
    status: int = 200,
) -> None:
    """Mock the 24 h window request (basal, bolus and food) of a user."""
    url = latest_url(user_id)
    if status == 200:
        aioclient_mock.get(url, params=WINDOW_QUERY, json=payload or [])
    else:
        aioclient_mock.get(url, params=WINDOW_QUERY, status=status)


def mock_profile(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    full_name: str | None = "Fake Owner",
    status: int = 200,
    exc: Exception | None = None,
) -> None:
    """Mock a user's profile; a ``None`` name means the user has no profile."""
    url = f"{API_URL}/metadata/{user_id}/profile"
    if exc is not None:
        aioclient_mock.get(url, exc=exc)
    elif status != 200:
        aioclient_mock.get(url, status=status)
    elif full_name is None:
        aioclient_mock.get(url, status=404)
    else:
        aioclient_mock.get(url, json={"fullName": full_name})


def mock_slow(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    cgm: dict | None = None,
    bgm_status: int = 404,
    data_sources_status: int = 200,
    data_sets_status: int = 200,
    data_sources: list[dict] | None = None,
    data_sets: list[dict] | None = None,
    device_events: dict[str, list[dict]] | None = None,
    device_events_status: int = 200,
) -> None:
    """Mock a user's slow endpoints: a synthetic cgm summary, no bgm summary.

    A ``None`` ``cgm`` means the fixture; an empty dict is an empty summary.
    Data sources and sets are empty unless given, and so are the device
    events (records by subType). Profile is mocked by ``mock_profile``.
    """
    mock_device_events(aioclient_mock, user_id, device_events, device_events_status)
    base = f"{API_URL}/v1/summaries"
    aioclient_mock.get(
        f"{base}/cgm/{user_id}",
        json=load_fixture("summary_cgm.json") if cgm is None else cgm,
    )
    if bgm_status == 200:
        aioclient_mock.get(f"{base}/bgm/{user_id}", json={"periods": {}})
    else:
        aioclient_mock.get(f"{base}/bgm/{user_id}", status=bgm_status)
    for name, status, payload in (
        ("data_sources", data_sources_status, data_sources),
        ("data_sets", data_sets_status, data_sets),
    ):
        url = f"{API_URL}/v1/users/{user_id}/{name}"
        if status == 200:
            aioclient_mock.get(url, json=payload or [])
        else:
            aioclient_mock.get(url, status=status)


def mock_device_events(
    aioclient_mock: AiohttpClientMocker,
    user_id: str = USER_ID,
    events: dict[str, list[dict]] | None = None,
    status: int = 200,
) -> None:
    """Mock the slow coordinator's device event requests, by subType.

    The mocker answers with the first mock whose query is a subset of the
    request's, so the latest mock (``latest=true``) would also answer these
    requests: they are moved to the front of the mocker's list.
    """
    events = events or {}
    url = latest_url(user_id)
    queries = [{**LATEST_QUERY, "subType": s} for s in LATEST_DEVICE_EVENTS]
    queries.append({"type": "deviceEvent", "subType": "prime"})
    for query in queries:
        if status == 200:
            aioclient_mock.get(url, params=query, json=events.get(query["subType"], []))
        else:
            aioclient_mock.get(url, params=query, status=status)
        mocks = aioclient_mock._mocks
        mocks.insert(0, mocks.pop())


def summary_gets(
    aioclient_mock: AiohttpClientMocker, user_id: str, kind: str = "cgm"
) -> int:
    return sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "get" and url.path == f"/v1/summaries/{kind}/{user_id}"
    )


def login_posts(aioclient_mock: AiohttpClientMocker) -> int:
    return sum(
        1
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "post" and str(url) == LOGIN_URL
    )


def data_gets(aioclient_mock: AiohttpClientMocker, user_id: str) -> list[URL]:
    """The ``/data/{user_id}`` request URLs made, in order."""
    return [
        url
        for method, url, _, _ in aioclient_mock.mock_calls
        if method.lower() == "get" and url.path == f"/data/{user_id}"
    ]


def device_event_gets(aioclient_mock: AiohttpClientMocker, user_id: str) -> list[URL]:
    """The slow coordinator's device event request URLs (they name a subType)."""
    return [url for url in data_gets(aioclient_mock, user_id) if "subType" in url.query]


def _data_gets(aioclient_mock: AiohttpClientMocker, user_id: str, key: str) -> int:
    return sum(
        1
        for url in data_gets(aioclient_mock, user_id)
        if key in url.query and "subType" not in url.query
    )


def latest_gets(aioclient_mock: AiohttpClientMocker, user_id: str) -> int:
    return _data_gets(aioclient_mock, user_id, "latest")


def window_gets(aioclient_mock: AiohttpClientMocker, user_id: str) -> int:
    return _data_gets(aioclient_mock, user_id, "type")


def password_entry(
    hass: HomeAssistant,
    patients: list[str] | None = None,
    **options: Any,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=USER_ID,
        title="Fake Owner",
        data={
            "email": EMAIL,
            "password": PASSWORD,
            CONF_PATIENTS: patients or [USER_ID],
        },
        options=options,
    )
    entry.add_to_hass(hass)
    return entry
