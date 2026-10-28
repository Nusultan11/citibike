"""The deliberately small F1 modern-source contract."""

import hashlib
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

PIPELINE_VERSION = "f1.0.0"
SCHEMA_VERSION = "citibike-modern-v1"
NY = ZoneInfo("America/New_York")
FIELDS = (
    "ride_id",
    "rideable_type",
    "started_at",
    "ended_at",
    "start_station_name",
    "start_station_id",
    "end_station_name",
    "end_station_id",
    "start_lat",
    "start_lng",
    "end_lat",
    "end_lng",
    "member_casual",
)


def period_bounds(period: str) -> tuple[datetime, datetime]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise ValueError("Period must be YYYY-MM")
    year, month = map(int, period.split("-"))
    start = datetime(year, month, 1, tzinfo=NY)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=NY)
    return start.astimezone(UTC), end.astimezone(UTC)


def version_id(source_checksum: str) -> str:
    return hashlib.sha256(f"{source_checksum}:{PIPELINE_VERSION}".encode()).hexdigest()
