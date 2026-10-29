from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from citibike.features.contracts import TYPES, target_hour
from citibike.forecasting.inference import rows_valid


def test_two_targets_24_hours_unique_grain() -> None:
    origin = datetime(2026, 10, 29, 10, 15, tzinfo=UTC)
    rows: list[dict[str, Any]] = [
        {
            "station_id": station,
            "forecast_origin_utc": origin,
            "target_hour_utc": target_hour(origin, h),
            "horizon_h": h,
            "prediction_type": kind,
            "predicted_value": 0.0,
            "status": "ok",
            "reason": None,
        }
        for station in ("001", "002")
        for kind in TYPES
        for h in range(1, 25)
    ]
    rows_valid(rows, ["001", "002"])
    with pytest.raises(ValueError, match="Duplicate"):
        rows_valid(rows + [rows[0]], ["001", "002"])
    with pytest.raises(ValueError, match="24 hours"):
        rows_valid(rows[:-1], ["001", "002"])
    wrong = deepcopy(rows)
    wrong[0]["target_hour_utc"] = origin
    with pytest.raises(ValueError, match="target/hour"):
        rows_valid(wrong, ["001", "002"])
