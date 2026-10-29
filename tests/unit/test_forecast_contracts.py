import math
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from citibike.features.contracts import (
    CATALOG,
    MODE,
    calendar_values,
    holiday,
    month_available,
    pit_gate,
    target_hour,
    utc,
    valid_prediction,
)
from citibike.features.dataset import availability_rows
from citibike.forecasting.evaluation_weather import summarize
from citibike.forecasting.metrics import metrics
from citibike.forecasting.verify import weather_metrics


def test_utc_horizons_and_dst() -> None:
    origin = utc("2025-11-02T04:31:00+00:00")
    assert target_hour(origin, 1) == utc("2025-11-02T05:00:00+00:00")
    assert target_hour(origin, 24) == utc("2025-11-03T04:00:00+00:00")
    a, b = (calendar_values(target_hour(origin, h)) for h in (1, 2))
    assert a["local_hour"] == b["local_hour"] == 1
    assert a["dst"] == 1 and b["dst"] == 0
    spring = utc("2025-03-09T06:00:00+00:00")
    assert calendar_values(target_hour(spring, 1))["local_hour"] == 3
    with pytest.raises(ValueError):
        target_hour(origin, 25)
    with pytest.raises(ValueError):
        utc(datetime(2025, 1, 1))


@pytest.mark.parametrize("future", ["availability", "event", "strict"])
def test_point_in_time_rejects_future(future: str) -> None:
    origin = datetime(2025, 6, 1, tzinfo=UTC)
    available = origin + timedelta(seconds=1) if future == "availability" else origin
    event = origin if future == "event" else origin - timedelta(days=1)
    strict = origin + timedelta(seconds=1) if future == "strict" else origin
    with pytest.raises(ValueError):
        pit_gate(origin, available, event, mode="strict", strict_available_at=strict)
    pit_gate(origin, origin, origin - timedelta(hours=1), mode=MODE)
    with pytest.raises(ValueError):
        pit_gate(origin, origin, origin - timedelta(hours=1), mode="strict")


def test_calendar_and_dependent_source_availability() -> None:
    assert holiday(date(2026, 7, 3)) and holiday(date(2026, 7, 4))
    assert holiday(date(2026, 11, 26)) and not holiday(date(2026, 11, 25))
    report = {
        "months": [
            {
                "month": "2024-10",
                "silver": [{"source_period": "2024-10"}, {"source_period": "2024-11"}],
            }
        ]
    }
    assert availability_rows(report) == [("2024-10", month_available("2024-11"))]
    assert month_available("2024-11") > month_available("2024-10")


def test_count_metrics_and_zero_denominator() -> None:
    result = metrics([0, 2], [1, 1])
    assert result["mae"] == result["rmse"] == result["wape"] == 1
    assert result["bias"] == 0
    assert metrics([0, 0], [0, 1])["wape"] is None
    assert metrics([0], [0])["rmse"] == 0
    for invalid in (-1, math.nan, math.inf):
        with pytest.raises(ValueError):
            metrics([1], [invalid])


def test_weather_outcome_labels_dst_and_missing() -> None:
    start = datetime(2025, 3, 9, 5, tzinfo=UTC)
    raw: dict[str, Any] = {
        "hourly_units": {"temperature_2m": "°C", "precipitation": "mm"},
        "hourly": {
            "time": [
                (start + timedelta(hours=h)).replace(tzinfo=None).isoformat()
                for h in range(24)
            ],
            "temperature_2m": [-1.0] * 24,
            "precipitation": [0.5] * 24,
        },
    }
    daily = summarize(raw)
    assert daily["2025-03-09"]["observed_hours"] == 23
    assert daily["2025-03-09"]["cold"] and daily["2025-03-09"]["heavy_rain"]
    assert not any("weather" in feature for feature in CATALOG)
    raw["hourly"]["temperature_2m"][0] = None
    assert "2025-03-09" not in summarize(raw)


def test_weather_precipitation_is_hour_ending_with_exact_threshold() -> None:
    start = datetime(2025, 6, 1, 4, tzinfo=UTC)
    raw: dict[str, Any] = {
        "hourly_units": {"temperature_2m": "°C", "precipitation": "mm"},
        "hourly": {
            "time": [(start + timedelta(hours=h)).isoformat() for h in range(25)],
            "temperature_2m": [20.0] * 25,
            "precipitation": [20.0] + [0.0] * 24,
        },
    }
    assert summarize(raw)["2025-06-01"]["precipitation_mm"] == 0
    raw["hourly"]["precipitation"] = [0.0] + [0.1] * 23 + [7.7]
    assert summarize(raw)["2025-06-01"]["precipitation_mm"] == 10.0
    assert summarize(raw)["2025-06-01"]["heavy_rain"]


def test_posthoc_weather_metrics_use_fixed_predictions(tmp_path: Path) -> None:
    path = tmp_path / "predictions.parquet"
    with duckdb.connect() as db:
        db.execute(
            "COPY (SELECT '2025-06-01 12:00:00+00'::TIMESTAMPTZ target_hour_utc,"
            "2 actual,1 predicted,TRUE eval_weather_heavy_rain) TO ? (FORMAT PARQUET)",
            [str(path)],
        )
    labels = {"2025-06-01": {"cold": False, "heat": False, "heavy_rain": False}}
    result = weather_metrics(path, labels)
    assert set(result) == {"weather:observed"}
    assert result["weather:observed"]["mae"] == 1
    assert result["weather:observed"]["wape"] == 0.5
    labels["2025-06-01"]["heavy_rain"] = True
    assert weather_metrics(path, labels)["weather:heavy_rain"]["n"] == 1


@pytest.mark.parametrize("value", [-1, math.inf, math.nan, None])
def test_invalid_forecast_output(value: float | None) -> None:
    row = {
        "forecast_origin_utc": "2026-10-29T10:15:00+00:00",
        "target_hour_utc": "2026-10-29T11:00:00+00:00",
        "horizon_h": 1,
        "prediction_type": "arrivals",
        "predicted_value": value,
        "status": "ok",
        "reason": None,
    }
    with pytest.raises(ValueError):
        valid_prediction(row)
    row.update(status="unavailable", predicted_value=None, reason="unknown station")
    valid_prediction(row)
