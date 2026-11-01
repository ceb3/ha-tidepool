"""Record models and parsing for Tidepool data."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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


# Insulin and carbs: only records Tidepool already holds are summed or clipped.
# Nothing here estimates, predicts or suggests a dose.

# The record types the fast coordinator keeps for the last 24 hours.
WINDOW_TYPES = ("basal", "bolus", "food")
DAY = timedelta(hours=24)

# Basal delivery types (``deliveryType``) as the Tidepool data spec lists them.
BASAL_DELIVERY_TYPES = ("automated", "scheduled", "suspend", "temp")


def _duration(record: dict) -> timedelta | None:
    """Return a record's ``duration`` (milliseconds), or None if unusable."""
    value = record.get("duration")
    if not _is_number(value) or value < 0:
        return None
    return timedelta(milliseconds=value)


def _record_end(record: dict, time: datetime) -> datetime:
    """When a record stops mattering: a basal segment's end, else its time."""
    if record["type"] == "basal":
        return time + (_duration(record) or timedelta())
    return time


def merge_window(
    previous: dict[str, list[dict]],
    records: Iterable[Any],
    now: datetime,
    window: timedelta = DAY,
) -> dict[str, list[dict]]:
    """Merge fetched records into the previous window and drop the expired.

    Records are de-duplicated by type and ``id`` (the fetched copy wins);
    records without an id or a usable time are skipped. A record is kept while
    it ends inside the window, so a basal segment that started before the
    window start stays as long as it runs into the window. Each type's list
    is ordered by time.
    """
    start = now - window
    merged: dict[tuple[str, str], tuple[datetime, dict]] = {}
    for record in [r for rs in previous.values() for r in rs] + list(records):
        time = _record_time(record)
        if time is None or record["type"] not in WINDOW_TYPES:
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str):
            continue
        merged[(record["type"], record_id)] = (time, record)
    result: dict[str, list[dict]] = {t: [] for t in WINDOW_TYPES}
    for time, record in sorted(merged.values(), key=lambda item: item[0]):
        if _record_end(record, time) >= start:
            result[record["type"]].append(record)
    return result


def basal_units(segments: list[dict], now: datetime, window: timedelta = DAY) -> float:
    """Sum the basal insulin delivered in ``[now - window, now]``.

    Each segment delivers ``rate`` U/h for ``duration`` milliseconds, clipped
    to the window and to the start of the next segment (a later segment
    supersedes an overlapping one). A ``suspend`` segment has no rate and
    delivers nothing; a segment without a duration is skipped.
    """
    start = now - window
    timed = sorted(
        ((time, s) for s in segments if (time := _record_time(s)) is not None),
        key=lambda item: item[0],
    )
    total = 0.0
    for index, (begin, segment) in enumerate(timed):
        rate = segment.get("rate")
        duration = _duration(segment)
        if not _is_number(rate) or duration is None:
            continue
        end = min(begin + duration, now)
        if index + 1 < len(timed):
            end = min(end, timed[index + 1][0])
        begin = max(begin, start)
        if end > begin:
            total += rate * ((end - begin) / timedelta(hours=1))
    return total


def delivered_bolus(record: dict) -> float | None:
    """Units a bolus delivered: ``normal`` plus ``extended``.

    ``expectedNormal`` and ``expectedExtended`` (what was programmed before a
    bolus was cancelled) are not delivered insulin and are ignored.
    """
    parts = [record.get(key) for key in ("normal", "extended")]
    delivered = [float(part) for part in parts if _is_number(part)]
    return sum(delivered) if delivered else None


def net_carbs(record: dict) -> float | None:
    """Net carbohydrate grams of a food record, or None if it has none."""
    nutrition = record.get("nutrition")
    if not isinstance(nutrition, dict):
        return None
    carbohydrate = nutrition.get("carbohydrate")
    if not isinstance(carbohydrate, dict):
        return None
    if carbohydrate.get("units", "grams") != "grams":
        return None
    return _number(carbohydrate.get("net"))


def window_sum(
    records: list[dict],
    value_fn: Callable[[dict], float | None],
    now: datetime,
    window: timedelta = DAY,
) -> float:
    """Sum ``value_fn`` over the records timed in ``[now - window, now]``."""
    start = now - window
    return math.fsum(
        value
        for record in records
        if (time := _record_time(record)) is not None
        and start <= time <= now
        and (value := value_fn(record)) is not None
    )


def newest_with[T](
    records: Iterable[dict], value_fn: Callable[[dict], T | None]
) -> tuple[T, datetime, dict] | None:
    """Return the value, time and record of the newest record with a value."""
    best: tuple[T, datetime, dict] | None = None
    for record in records:
        time = _record_time(record)
        if time is None or (best is not None and time <= best[1]):
            continue
        value = value_fn(record)
        if value is not None:
            best = (value, time, record)
    return best


def _amount(value: Any) -> float | None:
    """The ``amount`` of a dosing decision's on-board object."""
    return _number(value.get("amount")) if isinstance(value, dict) else None


def insulin_on_board(record: dict) -> float | None:
    """Insulin on board as recorded by a dosing decision or bolus calculator.

    A ``dosingDecision`` holds ``insulinOnBoard.amount``; a ``wizard`` (the
    pump's bolus calculator) holds ``insulinOnBoard`` as a number.
    """
    if record.get("type") == "dosingDecision":
        return _amount(record.get("insulinOnBoard"))
    if record.get("type") == "wizard":
        return _number(record.get("insulinOnBoard"))
    return None


def carbs_on_board(record: dict) -> float | None:
    """Carbs on board as recorded by a dosing decision (``carbsOnBoard``)."""
    if record.get("type") == "dosingDecision":
        return _amount(record.get("carbsOnBoard"))
    return None
