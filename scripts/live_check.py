#!/usr/bin/env python3
"""Check the Tidepool integration's API layer against the real service.

Run from the repository root with the project venv::

    TIDEPOOL_EMAIL=... TIDEPOOL_PASSWORD=... .venv/bin/python scripts/live_check.py
    TIDEPOOL_ACCESS_TOKEN=... .venv/bin/python scripts/live_check.py

The output describes structure only: record types, counts, key names, HTTP
status codes and which header an OAuth access token was accepted in. It never
prints a value (no numbers, ids, names, emails, timestamps or tokens) and never
prints an exception message, only the exception class and HTTP status.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.tidepool.api import (
    AuthError,
    NotFoundError,
    RateLimitedError,
    TidepoolClient,
)
from custom_components.tidepool.auth import (
    LegacyAuth,
    _unverified_jwt_claims,
    extract_user_id,
)
from custom_components.tidepool.const import API_URL

FAILED = "FAILED"

# Record types whose names may be printed; anything else is reported as "other".
_KNOWN_TYPES = frozenset(
    {
        "basal",
        "bolus",
        "cbg",
        "cgm",
        "cgmSettings",
        "deviceEvent",
        "food",
        "insulin",
        "physicalActivity",
        "pumpSettings",
        "reportedState",
        "smbg",
        "upload",
        "water",
        "wizard",
    }
)
_KEY_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")
_HEX_ID = re.compile(r"[0-9a-f]{8,}")
_MAX_DEPTH = 6
_OTHER = "<other>"
_TOKEN_HEADER = "X-Tidepool-Session-Token"


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    for kind, name in (
        (bool, "bool"),
        (int, "int"),
        (float, "float"),
        (str, "str"),
        (list, "list"),
        (dict, "object"),
    ):
        if isinstance(value, kind):
            return name
    return "other"


def _is_key_name(key: Any) -> bool:
    """Tell a field name from data that is used as a key (an id, a name)."""
    return (
        isinstance(key, str)
        and _KEY_NAME.fullmatch(key) is not None
        and not _HEX_ID.fullmatch(key)
        and sum(c.isdigit() for c in key) <= 2
    )


def _type_label(record: dict) -> str:
    record_type = record.get("type")
    if not isinstance(record_type, str):
        return "untyped"
    return record_type if record_type in _KNOWN_TYPES else "other"


def _keys_summary(dicts: list[dict]) -> str:
    """Return the sorted key names with their value kinds and a count of the rest."""
    kinds: dict[str, set[str]] = {}
    others = 0
    for item in dicts:
        for key, value in item.items():
            if _is_key_name(key):
                kinds.setdefault(key, set()).add(_kind(value))
            else:
                others += 1
    parts = [f"{key}:{'|'.join(sorted(kinds[key]))}" for key in sorted(kinds)]
    if others:
        parts.append(f"(+{others} other keys)")
    return ", ".join(parts) if parts else "none"


def _describe_dicts(
    dicts: list[dict], path: str, label: str, lines: list[str], depth: int
) -> None:
    lines.append(f"{path}: {len(dicts)} x {label}; keys: {_keys_summary(dicts)}")
    if depth >= _MAX_DEPTH:
        return
    named = sorted({k for item in dicts for k in item if _is_key_name(k)})
    for key in [*named, _OTHER]:
        values = [
            value
            for item in dicts
            for name, value in item.items()
            if (name == key if key != _OTHER else not _is_key_name(name))
        ]
        _describe_children(values, f"{path}.{key}", lines, depth + 1)


def _describe_children(
    values: list[Any], path: str, lines: list[str], depth: int
) -> None:
    objects = [v for v in values if isinstance(v, dict)]
    if objects:
        _describe_dicts(objects, path, "object", lines, depth)
    items = [i for v in values if isinstance(v, list) for i in v]
    item_objects = [i for i in items if isinstance(i, dict)]
    if item_objects:
        _describe_dicts(item_objects, f"{path}[]", "object", lines, depth)
    elif any(isinstance(v, list) for v in values):
        lines.append(f"{path}[]: {len(items)} scalar items")


def describe(payload: Any, path: str = "payload") -> str:
    """Describe the structure of ``payload`` without any of its values.

    Prints record types (only well-known type names), counts, the sorted names
    of fields and the kinds of their values. Keys that look like data (ids,
    names, anything that is not a plain identifier) are only counted.
    """
    lines: list[str] = []
    if isinstance(payload, dict):
        _describe_dicts([payload], path, "object", lines, 0)
    elif isinstance(payload, list):
        records = [item for item in payload if isinstance(item, dict)]
        if records:
            groups: dict[str, list[dict]] = {}
            for record in records:
                groups.setdefault(_type_label(record), []).append(record)
            for label in sorted(groups):
                _describe_dicts(groups[label], path, label, lines, 0)
        scalars = len(payload) - len(records)
        if scalars or not payload:
            lines.append(f"{path}: {scalars} scalar items")
    else:
        lines.append(f"{path}: {_kind(payload)}")
    return "\n".join(lines)


def _failure(err: BaseException) -> str:
    """Return the exception class and HTTP status only, never its message."""
    status: int | None = None
    if isinstance(err, AuthError):
        status = err.status
    elif isinstance(err, NotFoundError):
        status = 404
    elif isinstance(err, RateLimitedError):
        status = 429
    suffix = f" status={status}" if status is not None else ""
    return f"{FAILED} {type(err).__name__}{suffix}"


async def _raw_get(
    session: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    params: dict[str, str] | None = None,
) -> tuple[int | None, Any]:
    """GET once and return the status (or None on network failure) and body."""
    try:
        async with session.get(url, headers=headers, params=params) as response:
            body = None
            if response.status == 200:
                body = await response.json(content_type=None)
            return response.status, body
    except aiohttp.ClientError, TimeoutError, ValueError:
        return None, None


async def check_legacy(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    base_url: str = API_URL,
) -> list[str]:
    """Log in with the project's own LegacyAuth and read through the client."""
    lines: list[str] = []
    auth = LegacyAuth(session, email, password, base_url)
    client = TidepoolClient(session, auth, base_url)

    try:
        headers = await auth.async_headers()
    except Exception as err:  # report the class only, never the message
        return [f"login: {_failure(err)}"]
    user_id = auth.user_id
    if user_id is None:
        return [f"login: {FAILED} no user id"]
    lines.append("login: 200")

    reads: list[tuple[str, Callable[[], Awaitable[Any]]]] = [
        ("latest", lambda: client.latest(user_id)),
        ("summary cgm", lambda: client.summary(user_id, "cgm")),
        ("access_groups", lambda: client.access_groups(user_id)),
        ("profile", lambda: client.profile(user_id)),
        ("data_sources", lambda: client.data_sources(user_id)),
        ("data_sets", lambda: client.data_sets(user_id)),
    ]
    latest: Any = None
    for name, read in reads:
        try:
            payload = await read()
        except Exception as err:  # report the class only, never the message
            lines.append(f"{name}: {_failure(err)}")
            continue
        if name == "latest":
            latest = payload
        lines.append(f"{name}: 200")
        lines.extend(f"  {line}" for line in describe(payload, name).splitlines())

    status, raw = await _raw_get(
        session, f"{base_url}/data/{user_id}", headers, {"latest": "true"}
    )
    if status == 200 and latest is not None:
        same = describe(raw) == describe(latest)
        lines.append(
            f"raw GET /data/{{user_id}}: 200; same structure as client.latest: "
            f"{'yes' if same else 'no'}"
        )
    else:
        lines.append(f"raw GET /data/{{user_id}}: {status if status else FAILED}")
    return lines


async def check_oauth(
    session: aiohttp.ClientSession, token: str, base_url: str = API_URL
) -> list[str]:
    """Find out which header the API accepts an OAuth access token in."""
    claims = _unverified_jwt_claims(token)
    if claims is None:
        return [f"access token: {FAILED} not a decodable JWT"]
    names = sorted(name for name in claims if _is_key_name(name))
    lines = [f"token claims: {', '.join(names) if names else 'none'}"]
    user_id = extract_user_id(claims)
    if user_id is None:
        lines.append(f"user id claim: {FAILED} none of userid, user_id, sub")
        return lines
    id_claim = next(c for c in ("userid", "user_id", "sub") if claims.get(c))
    lines.append(f"user id claim: {id_claim}")

    probes = {
        _TOKEN_HEADER: {_TOKEN_HEADER: token},
        "Authorization Bearer": {"Authorization": f"Bearer {token}"},
    }
    accepted = []
    for name, headers in probes.items():
        status, _ = await _raw_get(
            session, f"{base_url}/access/groups/{user_id}", headers
        )
        lines.append(f"{name}: {status if status else FAILED}")
        if status is not None and 200 <= status < 300:
            accepted.append(name)
    lines.append(f"accepted in: {', '.join(accepted) if accepted else 'neither'}")
    return lines


async def _run(email: str | None, password: str | None, token: str | None) -> int:
    timeout = aiohttp.ClientTimeout(total=30)
    lines: list[str] = []
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if email and password:
            lines.append("== legacy login ==")
            lines.extend(await check_legacy(session, email, password))
        if token:
            lines.append("== oauth access token ==")
            lines.extend(await check_oauth(session, token.strip()))
    print("\n".join(lines))
    return 1 if any(FAILED in line for line in lines) else 0


def main() -> int:
    """Run the checks the environment has credentials for."""
    email = os.environ.get("TIDEPOOL_EMAIL")
    password = os.environ.get("TIDEPOOL_PASSWORD")
    token = os.environ.get("TIDEPOOL_ACCESS_TOKEN")
    if not ((email and password) or token):
        print(
            "Set TIDEPOOL_EMAIL and TIDEPOOL_PASSWORD (legacy login) or "
            "TIDEPOOL_ACCESS_TOKEN (OAuth spike).",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_run(email, password, token))


if __name__ == "__main__":
    sys.exit(main())
