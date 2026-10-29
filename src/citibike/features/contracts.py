"""F4 point-in-time schema and executable calendar / availability contracts."""

import calendar
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from citibike.ingestion.archive import checksum
from citibike.storage import identity

VERSION = "f4.0.0"
MODE = "approximate retrospective"
TYPES = ("departures", "arrivals")
NY = ZoneInfo("America/New_York")
SEED = 42
DELAY_DAYS = 45
MAX_AGE_DAYS = 62
CATALOG = {
    "station_id": "string; historical ID known by first eligible source month",
    "horizon_h": "int; 1..24 next complete UTC hour buckets",
    "utc_hour": "int; target UTC hour, known calendar",
    "local_hour": "int; target America/New_York hour, known calendar",
    "weekday": "int; NY Monday=0, known calendar",
    "month": "int; NY month 1..12, known calendar",
    "weekend": "int; weekday>=5, known calendar",
    "holiday": "int; us-federal-observed-1, OPM rules, not station closure",
    "dst": "int; target NY UTC offset=-4h, known calendar",
    "seasonal_naive": "float; last available same station/weekday/local-hour count",
    "profile_mean": "float; expanding mean of available station/weekday/hour labels",
    "station_mean": "float; expanding station mean, available source months only",
    "profile_count": "int; observed eligible history count, not imputed hours",
}
NUMERIC = [k for k in CATALOG if k != "station_id"]
SCHEMA = identity([VERSION, CATALOG, DELAY_DAYS])


def code_hash(folder: Path) -> str:
    return identity({p.name: checksum(p) for p in sorted(folder.glob("*.py"))})


def utc(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Timezone-aware UTC timestamp required")
    return result.astimezone(UTC)


def target_hour(origin: datetime, horizon: int) -> datetime:
    if not 1 <= horizon <= 24:
        raise ValueError("Horizon must be 1..24")
    return utc(origin).replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=horizon
    )


def month_available(month: str) -> datetime:
    year, number = map(int, month.split("-"))
    following = date(year + (number == 12), number % 12 + 1, 1)
    return datetime.combine(following, datetime.min.time(), NY).astimezone(
        UTC
    ) + timedelta(days=DELAY_DAYS)


def holiday(day: date) -> bool:
    def nth(year: int, month: int, weekday: int, occurrence: int) -> date:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * occurrence)

    days: set[date] = set()
    for year in (day.year - 1, day.year, day.year + 1):
        for month, number in ((1, 1), (6, 19), (7, 4), (11, 11), (12, 25)):
            value = date(year, month, number)
            days.add(value)
            days.add(
                value + timedelta(days=-1 if value.weekday() == 5 else 1)
                if value.weekday() >= 5
                else value
            )
        days.update(
            [
                nth(year, 1, 0, 2),
                nth(year, 2, 0, 2),
                nth(year, 9, 0, 0),
                nth(year, 10, 0, 1),
                nth(year, 11, 3, 3),
            ]
        )
        last = date(year, 5, calendar.monthrange(year, 5)[1])
        days.add(last - timedelta(days=last.weekday()))
    return day in days


def calendar_values(hour: datetime) -> dict[str, int]:
    local = utc(hour).astimezone(NY)
    return {
        "utc_hour": hour.hour,
        "local_hour": local.hour,
        "weekday": local.weekday(),
        "month": local.month,
        "weekend": int(local.weekday() >= 5),
        "holiday": int(holiday(local.date())),
        "dst": int(bool(local.dst())),
    }


def pit_gate(
    origin: datetime,
    available_at: datetime,
    max_event_at: datetime,
    *,
    mode: str,
    strict_available_at: datetime | None = None,
) -> None:
    origin = utc(origin)
    if mode not in (MODE, "strict"):
        raise ValueError("Explicit availability mode required")
    if utc(available_at) > origin or utc(max_event_at) >= origin:
        raise ValueError("Future feature / unavailable source rejected")
    if mode == "strict" and (
        strict_available_at is None or utc(strict_available_at) > origin
    ):
        raise ValueError("Strict immutable input not available at origin")


def valid_prediction(row: dict[str, Any]) -> None:
    import math

    if row["prediction_type"] not in TYPES:
        raise ValueError("Unknown prediction type")
    if utc(row["target_hour_utc"]) != target_hour(
        utc(row["forecast_origin_utc"]), int(row["horizon_h"])
    ):
        raise ValueError("Forecast target/hour mismatch")
    value = row["predicted_value"]
    if row["status"] == "ok":
        if value is None or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid finite/nonnegative forecast")
    elif row["status"] != "unavailable" or value is not None or not row["reason"]:
        raise ValueError("Invalid unavailable forecast")
