"""Record models and parsing for Tidepool data."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .const import GLUCOSE_FACTOR

GLUCOSE_TYPES = ("cbg", "smbg")


def parse_time(s: str) -> datetime:
    """Parse a Tidepool ISO 8601 timestamp into a timezone-aware UTC datetime."""
    if s.endswith("Z"):
        s = f"{s[:-1]}+00:00"
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def mmol_to_mgdl(v: float) -> float:
    """Convert mmol/L to mg/dL, rounded to one decimal."""
    return round(v * GLUCOSE_FACTOR, 1)


@dataclass(frozen=True)
class Cbg:
    """A single glucose reading (continuous or fingerstick)."""

    value_mmol: float
    time: datetime
    id: str | None


def _record_time(record: Any) -> datetime | None:
    """Return the record's parsed time, or None if it is unusable."""
    if not isinstance(record, dict):
        return None
    if not isinstance(record.get("type"), str):
        return None
    raw = record.get("time")
    if not isinstance(raw, str):
        return None
    try:
        return parse_time(raw)
    except ValueError:
        return None


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


@dataclass
class Latest:
    """The newest record of each type."""

    by_type: dict[str, dict] = field(default_factory=dict)

    def _glucose(self, record_type: str) -> Cbg | None:
        record = self.by_type.get(record_type)
        if record is None:
            return None
        time = _record_time(record)
        value = record.get("value")
        if time is None or not _is_number(value):
            return None
        record_id = record.get("id")
        return Cbg(
            value_mmol=float(value),
            time=time,
            id=record_id if isinstance(record_id, str) else None,
        )

    @property
    def cbg(self) -> Cbg | None:
        """Newest continuous glucose reading."""
        return self._glucose("cbg")

    @property
    def smbg(self) -> Cbg | None:
        """Newest fingerstick glucose reading."""
        return self._glucose("smbg")


def parse_latest(payload: list[dict]) -> Latest:
    """Keep the newest record per type, skipping malformed or sparse records."""
    newest: dict[str, tuple[datetime, dict]] = {}
    for record in payload if isinstance(payload, list) else []:
        time = _record_time(record)
        if time is None:
            continue
        record_type = record["type"]
        if record_type in GLUCOSE_TYPES and not _is_number(record.get("value")):
            continue
        current = newest.get(record_type)
        if current is None or time > current[0]:
            newest[record_type] = (time, record)
    return Latest(by_type={t: rec for t, (_, rec) in newest.items()})
