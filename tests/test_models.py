"""Tests for record models and parsing."""

import json
from datetime import UTC
from pathlib import Path

from custom_components.tidepool.models import (
    mmol_to_mgdl,
    parse_latest,
    parse_time,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


def test_parse_time_is_utc_aware() -> None:
    parsed = parse_time("2026-11-01T12:00:00Z")
    assert parsed.tzinfo == UTC
    assert parsed.hour == 12


def test_mmol_to_mgdl() -> None:
    assert mmol_to_mgdl(5.5) == 99.1
    assert mmol_to_mgdl(3.9) == 70.3


def test_parse_latest_keeps_newest_per_type() -> None:
    latest = parse_latest(_load("latest.json"))
    assert latest.cbg is not None
    assert latest.cbg.id == "rec-2"
    assert latest.cbg.value_mmol == 3.9
    assert latest.cbg.time == parse_time("2026-11-01T12:05:00Z")
    assert latest.smbg is not None
    assert latest.smbg.id == "rec-3"
    assert set(latest.by_type) == {"cbg", "smbg", "basal"}


def test_parse_latest_keeps_newest_regardless_of_order() -> None:
    records = list(reversed(_load("latest.json")))
    latest = parse_latest(records)
    assert latest.cbg is not None
    assert latest.cbg.id == "rec-2"


def test_parse_latest_tolerates_missing_fields() -> None:
    records = [
        {"type": "cbg", "id": "rec-9", "value": 5.0},
        {"id": "rec-8", "time": "2026-11-01T12:00:00Z", "value": 5.0},
        {"type": "cbg", "id": "rec-7", "time": "not-a-time", "value": 5.0},
        {"type": "cbg", "id": "rec-6", "time": "2026-11-01T12:00:00Z"},
    ]
    latest = parse_latest(records)
    assert latest.cbg is None
    assert parse_latest(_load("latest_empty.json")).cbg is None
    assert parse_latest([]).cbg is None
