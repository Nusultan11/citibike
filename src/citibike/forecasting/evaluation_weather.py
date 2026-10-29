"""ERA5 outcome labels used AFTER prediction; never imported by features."""

import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from citibike.forecasting.artifacts import active, checked, create
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json

POLICY = "era5-evaluation-labels-2"


def summarize(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    hourly = raw["hourly"]
    if (
        raw["hourly_units"]["temperature_2m"] != "°C"
        or raw["hourly_units"]["precipitation"] != "mm"
    ):
        raise ValueError("Unexpected evaluation weather units")
    temperatures: dict[str, list[float]] = {}
    rainfall: dict[str, list[Decimal]] = {}
    zone = ZoneInfo("America/New_York")
    seen = set()
    for timestamp, temp, rain in zip(
        hourly["time"], hourly["temperature_2m"], hourly["precipitation"], strict=True
    ):
        hour = datetime.fromisoformat(timestamp).replace(tzinfo=UTC)
        if hour in seen:
            raise ValueError("Duplicate weather hour")
        seen.add(hour)
        if temp is not None:
            if not math.isfinite(temp):
                raise ValueError("Invalid evaluation weather value")
            key = hour.astimezone(zone).date().isoformat()
            temperatures.setdefault(key, []).append(float(temp))
        if rain is not None:
            if not math.isfinite(rain) or rain < 0:
                raise ValueError("Invalid evaluation weather value")
            # ERA5 precipitation is the preceding hour's sum (hour-ending).
            key = (hour - timedelta(microseconds=1)).astimezone(zone).date().isoformat()
            rainfall.setdefault(key, []).append(Decimal(str(rain)))
    result = {}
    for key, values in temperatures.items():
        day = date.fromisoformat(key)
        start = datetime.combine(day, datetime.min.time(), zone).astimezone(UTC)
        end = datetime.combine(
            day + timedelta(days=1), datetime.min.time(), zone
        ).astimezone(UTC)
        hours = int((end - start).total_seconds() / 3600)
        rain_values = rainfall.get(key, [])
        if len(values) != hours or len(rain_values) != hours:
            continue
        minimum = min(values)
        maximum = max(values)
        precipitation = sum(rain_values, Decimal(0))
        result[key] = {
            "min_c": minimum,
            "max_c": maximum,
            "precipitation_mm": float(precipitation),
            "cold": minimum <= 0,
            "heat": maximum >= 32,
            "heavy_rain": precipitation >= 10,
            "observed_hours": len(values),
        }
    return result


def prepare(root: Path, dataset: Path, source: Path | None = None) -> Path:
    data = checked(dataset)
    first = date.fromisoformat(data["config"]["validation"][0][0]) - timedelta(days=1)
    last = date.fromisoformat(data["config"]["final"][1]) + timedelta(days=1)
    params: dict[str, str | float] = {
        "latitude": 40.75,
        "longitude": -73.98,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "hourly": "temperature_2m,precipitation",
        "timezone": "UTC",
        "models": "era5",
    }
    request = httpx.Request(
        "GET", "https://archive-api.open-meteo.com/v1/archive", params=params
    )
    if source is None:
        with httpx.Client(timeout=90) as client:
            response = client.send(request)
            response.raise_for_status()
            raw = response.json()
    else:
        raw = read_json(source)
    daily = summarize(raw)
    needed = set()
    for start, end in [*data["config"]["validation"], data["config"]["final"]]:
        cursor = date.fromisoformat(start) - timedelta(days=1)
        while cursor <= date.fromisoformat(end):
            needed.add(cursor.isoformat())
            cursor += timedelta(days=1)
    missing = sorted(needed - set(daily))
    if missing:
        raise ValueError(
            "Incomplete evaluation weather dates: " + ",".join(missing[:5])
        )
    identifier = identity([POLICY, str(request.url), identity(raw)])

    def build(stage: Path) -> dict[str, Any]:
        write_json(stage / "source.json", raw)
        write_json(stage / "daily.json", daily)
        return {
            "kind": "evaluation_weather",
            "policy": POLICY,
            "source_url": str(request.url),
            "source": (
                "Open-Meteo ERA5 reanalysis, city grid proxy, not station measurement"
            ),
            "retrieved_at": datetime.now(UTC).isoformat(),
            "hourly_rows": len(raw["hourly"]["time"]),
            "daily_rows": len(daily),
            "coverage_required_days": len(needed),
            "missing_days": missing,
            "thresholds": {"cold_min_c": 0, "heat_max_c": 32, "rain_mm": 10},
            "use": "evaluation labels only AFTER prediction; never training features",
            "source_file_checksum": checksum(source) if source else None,
        }

    return create(root, "evaluation_weather", identifier, build)


def labels(
    root: Path, data: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if data["data_mode"] == "controlled_fixture":
        return {}, {"status": "not requested for controlled fixture; no network"}
    path = active(root, "evaluation_weather")
    report = checked(path)
    return read_json(path / "daily.json"), report


def attach(frame: Any, daily: dict[str, dict[str, Any]]) -> Any:
    output = frame.copy()
    keys = (
        output["target_hour_utc"].dt.tz_convert("America/New_York").dt.date.astype(str)
    )
    for name in ("cold", "heat", "heavy_rain"):
        output["eval_weather_" + name] = keys.map(
            lambda key, name=name: bool(daily.get(key, {}).get(name, False))
        )
    output["eval_weather_observed"] = keys.map(lambda key: key in daily)
    return output
