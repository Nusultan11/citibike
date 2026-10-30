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


def segmented(
    frame: Any, values: list[float], *, research: bool = False
) -> dict[str, dict[str, Any]]:
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
    if research:
        masks.update(
            {
                "actual:zero": data["actual"] == 0,
                "actual:nonzero": data["actual"] > 0,
                "actual:rare_1_4": data["actual"].between(1, 4),
                "actual:middle_5_19": data["actual"].between(5, 19),
                "actual:peak_ge20": data["actual"] >= 20,
            }
        )
        months = (
            data["target_hour_utc"]
            .dt.tz_convert("America/New_York")
            .dt.strftime("%Y-%m")
        )
        for month in sorted(months.unique()):
            masks["target_month:" + month] = months == month
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


def error_analysis(frame: Any, values: list[float]) -> dict[str, Any]:
    """Validation-only diagnostics; observed count thresholds predate this run."""
    data = frame.copy()
    data["predicted"] = values
    data["residual"] = data["predicted"] - data["actual"]
    data["absolute"] = data["residual"].abs()
    result: dict[str, Any] = {}
    for field in ("station_id", "local_hour"):
        groups = []
        for name, group in data.groupby(field, sort=True):
            groups.append(
                {
                    "key": str(name),
                    **metrics(
                        group["actual"].astype(float).tolist(),
                        group["predicted"].tolist(),
                    ),
                }
            )
        result[field] = sorted(groups, key=lambda r: (-r["mae"], r["key"]))[:10]
    result["largest_residuals"] = (
        data.sort_values(
            ["absolute", "station_id", "target_hour_utc", "horizon_h"],
            ascending=[False, True, True, True],
        )
        .head(10)[
            [
                "station_id",
                "forecast_origin_utc",
                "target_hour_utc",
                "horizon_h",
                "actual",
                "predicted",
                "residual",
            ]
        ]
        .astype(str)
        .to_dict("records")
    )
    result["actual_histogram"] = {
        "0": int((data["actual"] == 0).sum()),
        "1": int((data["actual"] == 1).sum()),
        "2-4": int(data["actual"].between(2, 4).sum()),
        "5-9": int(data["actual"].between(5, 9).sum()),
        "10-19": int(data["actual"].between(10, 19).sum()),
        "20+": int((data["actual"] >= 20).sum()),
    }
    return result
