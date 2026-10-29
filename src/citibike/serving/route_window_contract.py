"""Pure identity of a shared, exact serving rollup over immutable Gold months."""

import hashlib
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from citibike.storage import identity

VERSION = "f3-route-window-2"


def window_months(report: dict[str, Any]) -> list[dict[str, Any]]:
    zone = ZoneInfo("America/New_York")
    start = datetime.fromisoformat(report["period_start"]).astimezone(zone)
    end = datetime.fromisoformat(report["period_end"]).astimezone(zone)
    if start.day != 1 or end.day != 1:
        raise ValueError("Serving route window requires complete NYC months")
    return sorted(
        (
            p
            for p in report["months"]
            if start.strftime("%Y-%m") <= p["month"] < end.strftime("%Y-%m")
        ),
        key=lambda p: p["month"],
    )


def window_id(report: dict[str, Any]) -> str:
    return identity(
        [
            VERSION,
            report["period_start"],
            report["period_end"],
            [(p["month"], p["id"]) for p in window_months(report)],
        ]
    )


def window_table(identifier: str) -> str:
    return "a_rw_" + identifier[:32]


def month_table(name: str, identifier: str) -> str:
    return f"a_{hashlib.sha256(name.encode()).hexdigest()[:8]}_{identifier[:32]}"
