"""Tests for the live check script's value-free output."""

from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)
from yarl import URL

from custom_components.tidepool.const import API_URL

SCRIPT = Path(__file__).parent.parent / "scripts" / "live_check.py"

EMAIL = "fake.person@example.invalid"
PASSWORD = "fake-password-hunter2"
SESSION_TOKEN = "tok-secret-9f8e7d"
USER_ID = "zq7x1k2m9p"

# Distinctive values that must never show up in the output.
NAME = "Zebulon Quillfeather"
RECORD_ID = "rec-7d3f9a1c-uniq"
FLOAT_VALUE = 5.55
INT_VALUE = 1234567
TIME_VALUE = "2031-02-03T04:05:06Z"
KEY_ID = "a1b2c3d4e5"  # an id used as a dict key (as access groups do)


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("live_check", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def live_check() -> ModuleType:
    return _load()


def _payload() -> list[dict]:
    return [
        {
            "type": "cbg",
            "id": RECORD_ID,
            "time": TIME_VALUE,
            "value": FLOAT_VALUE,
            "units": "mmol/L",
            "deviceId": "Dexcom-uniq-device",
            "payload": {"count": INT_VALUE, "owner": NAME},
        },
        {"type": "cbg", "id": "other-id", "time": TIME_VALUE, "value": 6.01},
        {"type": NAME, "id": "x", "time": TIME_VALUE},
    ]


def _all_values() -> list[str]:
    return [
        NAME,
        RECORD_ID,
        str(FLOAT_VALUE),
        "5.55",
        str(INT_VALUE),
        "1234567",
        TIME_VALUE,
        "2031",
        "Dexcom-uniq-device",
        "mmol/L",
        "6.01",
        "other-id",
        KEY_ID,
        "Quillfeather",
    ]


def test_output_has_no_values(live_check: ModuleType) -> None:
    payloads = [
        _payload(),
        {
            "name": NAME,
            "count": INT_VALUE,
            "ratio": FLOAT_VALUE,
            KEY_ID: {"root": {}, "note": "Zebulon Quillfeather"},
            "profile": {"fullName": NAME, "patient": {"birthday": TIME_VALUE}},
            "ids": [RECORD_ID, KEY_ID],
            "flag": True,
            "nothing": None,
        },
        NAME,
        INT_VALUE,
        FLOAT_VALUE,
        [NAME, INT_VALUE, FLOAT_VALUE],
    ]
    for payload in payloads:
        output = live_check.describe(payload)
        assert output
        for value in _all_values():
            assert value not in output, value


def test_describe_reports_record_types_counts_and_sorted_keys(
    live_check: ModuleType,
) -> None:
    output = live_check.describe(
        [
            {"type": "cbg", "value": 1.0, "time": "t", "id": "a"},
            {"type": "cbg", "value": 2.0, "time": "t", "extra": 1},
            {"type": "smbg", "value": 3.0},
        ]
    )
    assert "cbg" in output
    assert "smbg" in output
    assert "2 x cbg" in output
    assert "1 x smbg" in output
    keys_line = next(line for line in output.splitlines() if "cbg" in line)
    for key in ("extra", "id", "time", "type", "value"):
        assert key in keys_line
    names = [k for k in ("extra", "id", "time", "type", "value") if k in keys_line]
    assert [keys_line.index(k) for k in names] == sorted(
        keys_line.index(k) for k in names
    )


def test_describe_handles_scalars_and_empty(live_check: ModuleType) -> None:
    assert live_check.describe([]).strip()
    assert live_check.describe({}).strip()
    assert live_check.describe(None).strip()
    assert NAME not in live_check.describe(NAME)


def _jwt(claims: dict) -> str:
    def part(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig-uniq"


async def test_legacy_check_output_has_no_values(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    live_check: ModuleType,
) -> None:
    aioclient_mock.post(
        f"{API_URL}/auth/login",
        json={"userid": USER_ID, "username": EMAIL},
        headers={"x-tidepool-session-token": SESSION_TOKEN},
    )
    aioclient_mock.get(f"{API_URL}/data/{USER_ID}", json=_payload())
    aioclient_mock.get(
        f"{API_URL}/v1/summaries/cgm/{USER_ID}",
        json={"type": "cgm", "config": {"name": NAME}, "stats": {"avg": FLOAT_VALUE}},
    )
    aioclient_mock.get(
        f"{API_URL}/access/groups/{USER_ID}", json={KEY_ID: {"root": {}}}
    )
    aioclient_mock.get(f"{API_URL}/metadata/{USER_ID}/profile", json={"fullName": NAME})
    aioclient_mock.get(
        f"{API_URL}/v1/users/{USER_ID}/data_sources",
        json=[{"providerName": NAME, "state": "connected"}],
    )
    aioclient_mock.get(f"{API_URL}/v1/users/{USER_ID}/data_sets", status=404)

    lines = await live_check.check_legacy(
        async_get_clientsession(hass), EMAIL, PASSWORD
    )
    output = "\n".join(lines)

    assert "login" in output
    assert "200" in output
    assert "404" in output  # the failing call reports its status, nothing else
    assert "NotFoundError" in output
    for value in [
        *_all_values(),
        EMAIL,
        PASSWORD,
        SESSION_TOKEN,
        USER_ID,
        "fake.person",
    ]:
        assert value not in output, value
    # The raw API call and the strategy-based client saw the same structure.
    assert "same structure as client.latest: yes" in output


async def test_legacy_check_login_failure_prints_class_and_status_only(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    live_check: ModuleType,
) -> None:
    aioclient_mock.post(f"{API_URL}/auth/login", status=401)
    lines = await live_check.check_legacy(
        async_get_clientsession(hass), EMAIL, PASSWORD
    )
    output = "\n".join(lines)
    assert "AuthError" in output
    assert "401" in output
    assert EMAIL not in output
    assert PASSWORD not in output


async def test_oauth_check_reports_header_names_and_claim_names_only(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    live_check: ModuleType,
) -> None:
    token = _jwt({"sub": USER_ID, "email": EMAIL, "preferred_username": NAME})
    statuses = iter([401, 200])

    async def respond(method: str, url: URL, data: object) -> object:
        return AiohttpClientMockResponse(method, url, status=next(statuses), json={})

    aioclient_mock.get(f"{API_URL}/access/groups/{USER_ID}", side_effect=respond)

    lines = await live_check.check_oauth(async_get_clientsession(hass), token)
    output = "\n".join(lines)

    sent = [call[3] for call in aioclient_mock.mock_calls]
    assert "X-Tidepool-Session-Token" in sent[0]
    assert sent[1]["Authorization"] == f"Bearer {token}"
    assert "X-Tidepool-Session-Token: 401" in output
    assert "Authorization Bearer: 200" in output
    for claim in ("sub", "email", "preferred_username"):
        assert claim in output
    for value in (token, USER_ID, EMAIL, NAME, "sig-uniq"):
        assert value not in output, value


async def test_oauth_check_without_user_id_claim_makes_no_request(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    live_check: ModuleType,
) -> None:
    lines = await live_check.check_oauth(
        async_get_clientsession(hass), _jwt({"email": EMAIL})
    )
    assert aioclient_mock.call_count == 0
    assert EMAIL not in "\n".join(lines)


def test_main_without_credentials_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    live_check: ModuleType,
) -> None:
    for name in (
        "TIDEPOOL_EMAIL",
        "TIDEPOOL_PASSWORD",
        "TIDEPOOL_ACCESS_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    assert live_check.main() != 0
    assert "TIDEPOOL_" in capsys.readouterr().err
