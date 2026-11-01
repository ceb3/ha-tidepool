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


# The periods Tidepool computes summaries for.
SUMMARY_PERIODS = ("1d", "7d", "14d", "30d")

# Tidepool sends ``percent`` and ``coefficientOfVariation`` as 0-1 fractions
# (the API spec's example for ``percent`` is 0.33); the model holds percentages.
_PERCENT = 100


def _number(value: Any) -> float | None:
    return float(value) if _is_number(value) else None


def _count(value: Any) -> int | None:
    return int(value) if _is_number(value) else None


def _percent(value: Any) -> float | None:
    number = _number(value)
    return None if number is None else round(number * _PERCENT, 1)


@dataclass(frozen=True)
class Summary:
    """One period of a Tidepool summary; a missing or malformed field is None.

    ``low`` and ``high`` are all time below and above range, including the
    deeper bands; ``very_low``, ``very_high`` and ``extreme_high`` are those
    bands alone, so they are parts of ``low`` and ``high``. Time in each range
    and ``cv`` are percentages (0-100), ``gmi`` is a
    percentage as Tidepool sends it, glucose values stay in mmol/L as
    delivered. ``delta`` is the change against the previous period of the
    same length, in the same units.
    """

    time_in_range: float | None
    low: float | None
    very_low: float | None
    high: float | None
    very_high: float | None
    extreme_high: float | None
    average_mmol: float | None
    gmi: float | None
    cv: float | None
    sd_mmol: float | None
    min_mmol: float | None
    max_mmol: float | None
    hours_with_data: int | None
    days_with_data: int | None
    delta: Summary | None = None

    @property
    def average_mgdl(self) -> float | None:
        """The average glucose in mg/dL."""
        if self.average_mmol is None:
            return None
        return mmol_to_mgdl(self.average_mmol)


def _range_percent(period: dict, key: str) -> float | None:
    stat = period.get(key)
    return _percent(stat.get("percent")) if isinstance(stat, dict) else None


def _parse_period(period: dict, *, with_delta: bool = True) -> Summary:
    delta = period.get("delta")
    return Summary(
        time_in_range=_range_percent(period, "inTarget"),
        low=_range_percent(period, "inAnyLow"),
        very_low=_range_percent(period, "inVeryLow"),
        high=_range_percent(period, "inAnyHigh"),
        very_high=_range_percent(period, "inVeryHigh"),
        extreme_high=_range_percent(period, "inExtremeHigh"),
        average_mmol=_number(period.get("averageGlucoseMmol")),
        gmi=_number(period.get("glucoseManagementIndicator")),
        cv=_percent(period.get("coefficientOfVariation")),
        sd_mmol=_number(period.get("standardDeviation")),
        min_mmol=_number(period.get("min")),
        max_mmol=_number(period.get("max")),
        hours_with_data=_count(period.get("hoursWithData")),
        days_with_data=_count(period.get("daysWithData")),
        delta=(
            _parse_period(delta, with_delta=False)
            if with_delta and isinstance(delta, dict)
            else None
        ),
    )


def parse_summary(payload: Any) -> dict[str, Summary]:
    """Parse the periods of a summary payload, keyed by period (``14d``)."""
    periods = payload.get("periods") if isinstance(payload, dict) else None
    if not isinstance(periods, dict):
        return {}
    return {
        name: _parse_period(period)
        for name, period in periods.items()
        if isinstance(period, dict)
    }
