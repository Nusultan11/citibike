"""Experiment metrics and predefined slices; independent oracle lives in verify."""

import math
from typing import Any

from citibike.features.contracts import MODE


def metrics(actual: list[float], predicted: list[float]) -> dict[str, Any]:
    if len(actual) != len(predicted) or not actual:
        raise ValueError("Nonempty matched actual/prediction arrays required")
    if any(not math.isfinite(v) or v < 0 for v in actual + predicted):
        raise ValueError("Invalid count metric input")
    n = len(actual)
    error = [p - y for p, y in zip(predicted, actual, strict=True)]
    absolute = math.fsum(abs(v) for v in error)
    total = math.fsum(actual)
    return {
        "n": n,
        "actual_sum": total,
        "mae": absolute / n,
        "wape": absolute / total if total else None,
        "rmse": math.sqrt(math.fsum(v * v for v in error) / n),
        "bias": math.fsum(error) / n,
        "availability_mode": MODE,
    }


def segmented(frame: Any, values: list[float]) -> dict[str, dict[str, Any]]:
    data = frame.copy()
    data["predicted"] = values
    masks: dict[str, Any] = {
        "all": data["horizon_h"] > 0,
        "peak_hour": (data["weekday"] < 5)
        & (data["local_hour"].isin([7, 8, 9, 16, 17, 18, 19])),
    }
    for h in range(1, 25):
        masks[f"horizon:{h}"] = data["horizon_h"] == h
    for group in ("low", "middle", "high", "new"):
        masks[f"activity:{group}"] = data["activity"] == group
    masks["new_known"] = data["new_known"]
    masks["weekday"] = data["weekday"] < 5
    masks["weekend"] = data["weekday"] >= 5
    masks["holiday"] = data["holiday"] == 1
    for name in ("cold", "heat", "heavy_rain", "observed"):
        field = "eval_weather_" + name
        if field in data:
            masks["weather:" + name] = data[field]
    masks["dst_transition"] = (
        data["target_hour_utc"]
        .dt.tz_convert("America/New_York")
        .dt.date.astype(str)
        .isin(["2025-03-09", "2025-11-02", "2026-03-08", "2026-11-01"])
    )
    for season, months in {
        "winter": [12, 1, 2],
        "spring": [3, 4, 5],
        "summer": [6, 7, 8],
        "autumn": [9, 10, 11],
    }.items():
        masks[f"season:{season}"] = data["month"].isin(months)
    return {
        name: metrics(
            data.loc[mask, "actual"].astype(float).tolist(),
            data.loc[mask, "predicted"].tolist(),
        )
        for name, mask in masks.items()
        if bool(mask.any())
    }
