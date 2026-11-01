"""Long-term statistics of a patient's continuous glucose.

``import_history`` turns cbg records into hourly mean, minimum and maximum
external statistics, ``tidepool:<user id>_glucose``. A statistics row is keyed
by its start hour, so importing an hour again replaces it: a re-run of the
same window adds nothing twice.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import BloodGlucoseConcentrationConverter

from .const import DOMAIN
from .models import MGDL, Cbg, mmol_to_mgdl

STATISTIC_NAME = "Tidepool glucose"
HOUR = timedelta(hours=1)


def statistic_id(user_id: str) -> str:
    """Return the statistic id of a patient's glucose.

    Statistic ids allow lowercase letters, digits and single underscores only,
    so anything else in the user id becomes an underscore.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", user_id.lower()).strip("_")
    return f"{DOMAIN}:{slug}_glucose"


def _in_unit(value_mmol: float, unit: str) -> float:
    """Return a mmol/L value in ``unit``, rounded as the glucose sensor is."""
    return mmol_to_mgdl(value_mmol) if unit == MGDL else round(value_mmol, 2)


def hourly_statistics(
    records: Iterable[Cbg], unit: str, now: datetime
) -> list[StatisticData]:
    """Aggregate readings to hourly rows, oldest first.

    An hour is the UTC hour a reading falls in. Hours without readings have no
    row, and neither has the hour ``now`` is in: its mean would still change.
    """
    by_hour: dict[datetime, list[float]] = defaultdict(list)
    for record in records:
        hour = record.time.replace(minute=0, second=0, microsecond=0)
        if hour + HOUR <= now:
            by_hour[hour].append(record.value_mmol)
    return [
        StatisticData(
            start=hour,
            mean=_in_unit(sum(values) / len(values), unit),
            min=_in_unit(min(values), unit),
            max=_in_unit(max(values), unit),
        )
        for hour, values in sorted(by_hour.items())
    ]


@callback
def async_import_cbg_statistics(
    hass: HomeAssistant, user_id: str, records: Iterable[Cbg], unit: str
) -> int:
    """Write the hourly glucose statistics of ``records``; return the hours.

    ``unit`` is mg/dL or mmol/L; the values are stored converted. The unit is
    the statistic's unit of measurement and Home Assistant keeps one per
    statistic: after Home Assistant's unit system changes, hours imported
    before stay in the old unit while an import of the same hours replaces
    them in the new one, so re-import the whole window to make them alike.
    """
    rows = hourly_statistics(records, unit, dt_util.utcnow())
    if not rows:
        return 0
    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=STATISTIC_NAME,
        source=DOMAIN,
        statistic_id=statistic_id(user_id),
        unit_class=BloodGlucoseConcentrationConverter.UNIT_CLASS,
        unit_of_measurement=unit,
    )
    async_add_external_statistics(hass, metadata, rows)
    return len(rows)
