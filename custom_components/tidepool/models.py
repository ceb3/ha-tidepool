"""Record models and parsing for Tidepool data."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def is_number(value: Any) -> bool:
    """Whether a JSON value is a finite number (a bool, NaN or infinity is not)."""
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def parse_glucose(record: Any) -> Cbg | None:
    """Return a glucose record as a reading, or None if it is unusable."""
    time = _record_time(record)
    if time is None:
        return None
    value = record.get("value")
    if not is_number(value):
        return None
    record_id = record.get("id")
    return Cbg(
        value_mmol=float(value),
        time=time,
        id=record_id if isinstance(record_id, str) else None,
    )


@dataclass
class Latest:
    """The newest record of each type."""

    by_type: dict[str, dict] = field(default_factory=dict)

    def _glucose(self, record_type: str) -> Cbg | None:
        record = self.by_type.get(record_type)
        return None if record is None else parse_glucose(record)

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
        if record_type in GLUCOSE_TYPES and not is_number(record.get("value")):
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
    return float(value) if is_number(value) else None


def _count(value: Any) -> int | None:
    return int(value) if is_number(value) else None


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

# The record types the fast coordinator keeps for the last 24 hours. Device
# events (alarms among them) have no duration: like boluses, they are kept
# while their time is inside the window.
WINDOW_TYPES = ("basal", "bolus", "food", "deviceEvent")
DAY = timedelta(hours=24)
# How far before the window the window request starts. Tidepool filters on a
# record's start time, so without it the basal segment that began before the
# window and runs into it would be missing after a restart. A single segment
# longer than 24 h + this lookback can still be missed.
BASAL_LOOKBACK = timedelta(hours=12)
# How long after its end the newest basal segment still counts as current;
# after that the pump's state is unknown rather than its last known state.
BASAL_STALE_GRACE = timedelta(hours=2)
# How long after its time a dosing decision's on-board amounts still count as
# current enough to show; after that they read unknown, not as an old value.
DECISION_STALE_AFTER = timedelta(hours=6)

# A record lasting longer than this has no usable duration: it is malformed.
MAX_DURATION = timedelta(days=7)

# Basal delivery types (``deliveryType``) as the Tidepool data spec lists them.
BASAL_DELIVERY_TYPES = ("automated", "scheduled", "suspend", "temp")


def _duration(record: dict) -> timedelta | None:
    """Return a record's ``duration`` (milliseconds), or None if unusable.

    A negative, non-finite or absurd (over ``MAX_DURATION``) one is unusable.
    """
    value = record.get("duration")
    if not is_number(value) or not 0 <= value <= MAX_DURATION / timedelta(
        milliseconds=1
    ):
        return None
    return timedelta(milliseconds=value)


def _record_end(record: dict, time: datetime) -> datetime:
    """When a record stops mattering: a basal segment's end, else its time.

    A basal segment without a usable duration (or one that would end past
    the last representable time) ends at its time.
    """
    if record["type"] == "basal":
        try:
            return time + (_duration(record) or timedelta())
        except OverflowError:
            return time
    return time


def basal_times(record: dict) -> tuple[datetime, datetime] | None:
    """The start and end of a basal segment, or None if it has no usable time."""
    time = _record_time(record)
    return None if time is None else (time, _record_end(record, time))


def basal_period(latest: Latest) -> dict[str, str] | None:
    """The start and end of the newest basal segment, as ISO attributes."""
    basal = latest.by_type.get("basal")
    times = None if basal is None else basal_times(basal)
    if times is None:
        return None
    return {"time": times[0].isoformat(), "ends": times[1].isoformat()}


def current_basal(latest: Latest, now: datetime) -> dict | None:
    """The newest basal segment, unless it ended more than the grace ago."""
    basal = latest.by_type.get("basal")
    times = None if basal is None else basal_times(basal)
    if times is None or now > times[1] + BASAL_STALE_GRACE:
        return None
    return basal


def decision_is_current(time: datetime, now: datetime) -> bool:
    """Whether a decision made at ``time`` is within ``DECISION_STALE_AFTER``."""
    return now <= time + DECISION_STALE_AFTER


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
        if not is_number(rate) or duration is None:
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
    delivered = [float(part) for part in parts if is_number(part)]
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


# Device events: the slow coordinator asks for the newest of each of these
# subtypes, and for the primes of the last PRIME_LOOKBACK (``latest`` would
# give only the newest prime, whatever its target).
LATEST_DEVICE_EVENTS = ("alarm", "reservoirChange", "status")
PRIME_LOOKBACK = timedelta(days=30)
PRIME_TARGETS = ("cannula", "tubing")

# Alarm types (``alarmType``) as the Tidepool data spec lists them; ``other``
# is its catch-all and stands in for any type it does not list.
ALARM_TYPES = (
    "auto_off",
    "low_insulin",
    "low_power",
    "no_delivery",
    "no_insulin",
    "no_power",
    "occlusion",
    "over_limit",
    "other",
)


def alarm_type(record: dict) -> str:
    """The alarm type of an alarm record, ``other`` if the spec does not list it."""
    value = record.get("alarmType")
    return value if value in ALARM_TYPES else "other"


def _device_event_key(record: dict) -> str | None:
    """A device event's subType; a prime's is split by target (``prime_cannula``)."""
    sub_type = record.get("subType")
    if sub_type in LATEST_DEVICE_EVENTS:
        return sub_type
    if sub_type == "prime" and record.get("primeTarget") in PRIME_TARGETS:
        return f"prime_{record['primeTarget']}"
    return None


def newest_device_events(records: Iterable[Any]) -> dict[str, dict]:
    """The newest device event of each kind, keyed by ``_device_event_key``."""
    newest: dict[str, tuple[datetime, dict]] = {}
    for record in records:
        time = _record_time(record)
        if time is None or record["type"] != "deviceEvent":
            continue
        key = _device_event_key(record)
        if key is not None and (key not in newest or time > newest[key][0]):
            newest[key] = (time, record)
    return {key: record for key, (_, record) in newest.items()}


# Settings: the pump's and CGM's configuration as last uploaded. Nothing here
# computes with a setting; a schedule entry is selected and a glucose value is
# converted to another unit, no more.

MMOL = "mmol/L"
MGDL = "mg/dL"
# Records spell the units both ways (``mg/dL`` and ``mg/dl``).
_GLUCOSE_UNITS = {MMOL.casefold(): MMOL, MGDL.casefold(): MGDL}


def dig(value: Any, *keys: str) -> Any:
    """Follow nested dict keys; None if any step is missing or not a dict."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def glucose_unit(record: dict) -> str | None:
    """The glucose unit a settings or wizard record is written in, or None.

    A pump's settings keep it in ``units.bg``; CGM settings and the bolus
    calculator in ``units``. A unit the spec does not list is None: a number
    in an unknown unit is not shown.
    """
    if record.get("type") == "pumpSettings":
        raw = dig(record, "units", "bg")
    else:
        raw = record.get("units")
    return _GLUCOSE_UNITS.get(raw.casefold()) if isinstance(raw, str) else None


def convert_glucose(value: float, source: str, target: str) -> float:
    """Convert a glucose value between mg/dL and mmol/L with the shared factor.

    Equal units pass through untouched; mg/dL is rounded to one decimal and
    mmol/L to two, as a pump shows them.
    """
    if source == target:
        return value
    if target == MGDL:
        return mmol_to_mgdl(value)
    return round(value / GLUCOSE_FACTOR, 2)


# How the pump's clock was found (the ``zone_basis`` attribute).
ZONE_NAMED = "named_zone"
ZONE_HOME = "home_zone"
ZONE_FIXED = "fixed_offset"


def _settings_time(settings: dict) -> datetime | None:
    raw = settings.get("time")
    if not isinstance(raw, str):
        return None
    try:
        return parse_time(raw)
    except ValueError:
        return None


def pump_zone(settings: dict, home: tzinfo) -> tuple[tzinfo, str]:
    """The time zone a pump's schedules are read in, and how it was found.

    From the settings record: its ``timezone`` name if it holds a known one
    (this follows daylight saving time), else ``scheduleTimeZoneOffset`` (the
    offset the schedules were made in; real records carry it though the spec
    does not list it for settings), else the record's ``timezoneOffset``, both
    in minutes from UTC, else ``home`` (Home Assistant's zone).

    An offset alone is the offset when the pump uploaded. If ``home`` had that
    same offset at the record's own time, the pump was set to the home zone
    then, and the home zone is used, so daylight saving time is followed
    (R21). Otherwise the offset is kept fixed.
    """
    name = settings.get("timezone")
    if isinstance(name, str):
        try:
            return ZoneInfo(name), ZONE_NAMED
        except ZoneInfoNotFoundError, ValueError, OSError:
            pass
    for key in ("scheduleTimeZoneOffset", "timezoneOffset"):
        offset = settings.get(key)
        if is_number(offset) and abs(offset) < DAY / timedelta(minutes=1):
            fixed = timedelta(minutes=offset)
            uploaded = _settings_time(settings)
            if uploaded is not None and uploaded.astimezone(home).utcoffset() == fixed:
                return home, ZONE_HOME
            return timezone(fixed), ZONE_FIXED
    return home, ZONE_HOME


def pump_local_time(settings: dict, now: datetime, default: tzinfo) -> datetime:
    """``now`` on the pump's clock (see ``pump_zone``)."""
    return now.astimezone(pump_zone(settings, default)[0])


def _time_of_day(local_time: time | timedelta | datetime) -> timedelta:
    """The offset from midnight of a time, a datetime's time or an offset."""
    if isinstance(local_time, datetime):
        local_time = local_time.time()
    if isinstance(local_time, time):
        local_time = timedelta(
            hours=local_time.hour,
            minutes=local_time.minute,
            seconds=local_time.second,
            microseconds=local_time.microsecond,
        )
    return local_time % DAY


def value_at(schedule: Any, local_time: time | timedelta | datetime) -> dict | None:
    """The entry of a pump schedule in force at a time of day.

    A schedule is a list of entries whose ``start`` is milliseconds since
    midnight on the pump's clock; an entry holds until the next one starts. The
    entry with the greatest ``start`` that is at or before the time applies
    (at exactly an entry's start, that entry). Schedules wrap: before the first
    entry, which then starts after midnight, the previous day's last entry
    still applies. Entries are taken in any order; one without a numeric
    ``start`` is skipped. Only an empty or unusable schedule gives None.
    """
    entries = sorted(
        (
            entry
            for entry in (schedule if isinstance(schedule, list) else [])
            if isinstance(entry, dict) and is_number(entry.get("start"))
        ),
        key=lambda entry: entry["start"],
    )
    if not entries:
        return None
    now = _time_of_day(local_time) / timedelta(milliseconds=1)
    return ([e for e in entries if e["start"] <= now] or entries)[-1]


def active_entry(
    settings: dict, key: str, local_time: time | timedelta | datetime, *, sole: bool
) -> dict | None:
    """The entry now in force of a set of named schedules of a settings record.

    ``key`` names the set (``basalSchedules``, ``carbRatios``...) and the
    schedule used is the one named by ``activeSchedule``. With ``sole`` a set
    with a single schedule of another name is used too: carb ratios, targets
    and sensitivities are often not named like the basal schedule. Otherwise
    (or with several schedules, none active) there is no answer.
    """
    schedules = settings.get(key)
    if not isinstance(schedules, dict):
        return None
    name = settings.get("activeSchedule")
    schedule = schedules.get(name) if isinstance(name, str) else None
    if schedule is None and sole and len(schedules) == 1:
        (schedule,) = schedules.values()
    return value_at(schedule, local_time)


# The states a reported-state record may hold, as the Tidepool data spec lists
# them; ``other`` stands in for any it does not list. Its free text is not read.
REPORTED_STATES = (
    "alcohol",
    "cycle",
    "hyperglycemiaSymptoms",
    "hypoglycemiaSymptoms",
    "illness",
    "other",
    "stress",
)


def reported_states(record: dict) -> list[str]:
    """The state names of a reported-state record, once each in record order."""
    states = record.get("states")
    names = [
        state["state"]
        for state in (states if isinstance(states, list) else [])
        if isinstance(state, dict) and isinstance(state.get("state"), str)
    ]
    return list(
        dict.fromkeys(name if name in REPORTED_STATES else "other" for name in names)
    )
