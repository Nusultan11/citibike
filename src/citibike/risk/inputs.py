"""Read-only published F4 + checksummed GBFS, joined at one explicit as-of."""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import psycopg

from citibike.features.contracts import SCHEMA, TYPES, utc
from citibike.forecasting.artifacts import checked
from citibike.forecasting.inference import rows_valid
from citibike.ingestion.archive import checksum
from citibike.ingestion.gbfs import GBFS_PIPELINE, envelope, normalize, safe_url
from citibike.risk import Config, Scenario, Station
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import identity, read_json


def parquet_rows(path: Path) -> list[dict[str, Any]]:
    with duckdb.connect() as db:
        db.execute("SET threads=2; SET memory_limit='256MB'; SET TimeZone='UTC'")
        cursor = db.execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [c[0] for c in cursor.description]
        return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def adapt(
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    published_at: datetime,
    gbfs: list[dict[str, Any]],
    feeds: dict[str, Any],
    as_of: datetime,
    config: Config,
    provenance: dict[str, Any],
    requested: list[str] | None = None,
) -> tuple[Scenario | None, dict[str, Any]]:
    as_of = utc(as_of)
    reasons: list[str] = []
    origin = utc(report["forecast_origin_utc"])
    if report.get("availability_mode") != "strict" or report.get("status") != "ok":
        reasons.append("forecast_not_valid_strict")
    if report.get("feature_schema") != SCHEMA:
        reasons.append("forecast_schema_mismatch")
    if not origin <= utc(report["issued_at"]) <= utc(published_at) <= as_of:
        reasons.append("forecast_not_published_as_of")
    if utc(report["strict_source_available_at"]) > origin:
        reasons.append("forecast_future_source")
    if utc(report["valid_until"]) <= as_of:
        reasons.append("forecast_expired")
    ids = requested if requested is not None else report["station_ids"]
    if not ids or len(ids) > 64 or len(ids) != len(set(ids)):
        raise ValueError("Invalid/duplicate requested station IDs")
    rows_valid(rows, report["station_ids"])
    if any(utc(r["forecast_origin_utc"]) != origin for r in rows):
        reasons.append("mixed_forecast_origins")
    if any(utc(r["feature_available_at"]) > origin for r in rows):
        reasons.append("future_forecast_features")
    start = as_of.replace(minute=0, second=0, microsecond=0)
    if start < as_of:
        start += timedelta(hours=1)
    start = max(
        start, origin.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    )
    end = start + timedelta(hours=config.hours)
    if end > utc(report["valid_until"]):
        reasons.append("forecast_horizon_insufficient")
    if len({r["station_id"] for r in gbfs}) != len(gbfs):
        reasons.append("duplicate_gbfs_station_id")
    for name in ("station_information", "station_status"):
        feed = feeds[name]
        received, updated = utc(feed["received_at_utc"]), utc(feed["last_updated"])
        if received > as_of or updated > as_of:
            reasons.append(f"future_gbfs_{name}")
        # Conservative F3 hold: positive TTL, never longer than 60s after receipt.
        if feed["ttl"] <= 0 or as_of >= min(
            received + timedelta(seconds=60), updated + timedelta(seconds=feed["ttl"])
        ):
            reasons.append(f"stale_gbfs_{name}")
    aliases: dict[str, list[dict[str, Any]]] = {}
    for r in gbfs:
        if r.get("trip_station_id"):
            aliases.setdefault(r["trip_station_id"], []).append(r)
    points = {
        (r["station_id"], utc(r["target_hour_utc"]), r["prediction_type"]): r
        for r in rows
    }
    stations = []
    demand: dict[str, tuple[tuple[float, float], ...]] = {}
    exclusions: dict[str, list[str]] = {}
    for sid in sorted(ids):
        errors: list[str] = []
        match = aliases.get(sid, [])
        if len(match) != 1:
            errors.append(
                "missing_station_mapping" if not match else "ambiguous_station_mapping"
            )
        values = []
        for hour in range(config.hours):
            pair = []
            for kind in TYPES:
                point = points.get((sid, start + timedelta(hours=hour), kind))
                if point is None or point["status"] != "ok":
                    errors.append(
                        f"forecast_unavailable:{hour}:{kind}:"
                        + (str(point["reason"]) if point else "missing_hour")
                    )
                else:
                    pair.append(float(point["predicted_value"]))
            if len(pair) == 2:
                values.append((pair[0], pair[1]))
        if match:
            r = match[0]
            if r["quality_status"] != "valid":
                errors.append("gbfs_quality:" + ",".join(r["quality_reason_codes"]))
            if r.get("last_reported") is None:
                errors.append("unknown_gbfs_last_reported")
            elif utc(r["last_reported"]) > as_of:
                errors.append("future_gbfs_last_reported")
            elif as_of >= utc(r["last_reported"]) + timedelta(
                seconds=min(60, r["ttl"])
            ):
                errors.append("stale_gbfs_station")
            try:
                station = Station(
                    sid,
                    r["station_id"],
                    r["capacity"],
                    r["bikes_available"],
                    r["docks_available"],
                    r["is_installed"],
                    r["is_renting"],
                    r["is_returning"],
                    r["lat"],
                    r["lon"],
                )
            except (ValueError, TypeError) as exc:
                errors.append("invalid_station_state:" + str(exc))
        if errors:
            exclusions[sid] = sorted(set(errors))
        else:
            stations.append(station)
            demand[sid] = tuple(values)
    if not stations:
        reasons.append("no_eligible_stations")
    gate = {
        "status": "unavailable" if reasons else "ok",
        "reasons": sorted(set(reasons)),
        "as_of": as_of.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "coverage": {
            "requested": len(ids),
            "eligible": len(stations),
            "fraction": len(stations) / len(ids),
            "exclusions": exclusions,
        },
        "provenance": provenance,
        "risk": None if reasons else "scenario-only; see simulation outcomes",
    }
    return (
        None
        if reasons
        else Scenario(as_of, start, tuple(stations), demand, provenance, exclusions)
    ), gate


def load(
    forecast: Path,
    gbfs_path: Path,
    as_of: datetime,
    config: Config,
    settings: DatabaseSettings | None = None,
    requested: list[str] | None = None,
) -> tuple[Scenario | None, dict[str, Any]]:
    provenance: dict[str, Any] = {
        "forecast_path": str(forecast),
        "gbfs_path": str(gbfs_path),
    }
    try:
        report = checked(forecast)
        bundle = checked(Path(report["model_bundle_path"]))
        data = checked(Path(bundle["dataset_path"]))
        provenance.update(
            {
                "data_mode": data["data_mode"],
                "forecast_id": report["publication_id"],
                "forecast_manifest_sha256": checksum(forecast / "manifest.json"),
                "dataset_id": data["id"],
                "model_bundle_id": bundle["id"],
                "source_snapshot_id": data["source_snapshot_id"],
                "gold_publication_id": data["gold_publication_id"],
                "accuracy_evidence_mode": report["accuracy_evidence_mode"],
                "operational_promotion": "NOT VERIFIED; Seasonal Naive fallback only",
            }
        )
        if report.get("kind") != "forecast" or report["feature_schema"] != SCHEMA:
            raise ValueError("forecast_schema_mismatch")
        for key in ("dataset_id", "source_snapshot_id", "gold_publication_id"):
            expected = data["id"] if key == "dataset_id" else data[key]
            if report[key] != bundle[key] or report[key] != expected:
                raise ValueError("forecast_lineage_mismatch")
        if (
            report["model_bundle_id"] != bundle["id"]
            or report["station_version"] != data["checksums"]["stations.parquet"]
        ):
            raise ValueError("forecast_model_station_version_mismatch")
        if any(bundle["models"][k]["name"] != "seasonal_naive" for k in TYPES):
            raise ValueError("operational_model_not_approved; fallback required")
        if (
            identity(
                [
                    bundle["id"],
                    report["forecast_origin_utc"],
                    report["station_ids"],
                    SCHEMA,
                    report["forecast_recipe"],
                ]
            )
            != report["publication_id"]
        ):
            raise ValueError("forecast_identity_mismatch")
        rows = parquet_rows(forecast / "forecasts.parquet")
        for r in rows:
            if r["model_id"] != bundle["models"][r["prediction_type"]]["model_id"]:
                raise ValueError("forecast_row_model_mismatch")
        with connect(settings, readonly=True) as db:
            saved = db.execute(
                "SELECT manifest,published_at FROM ml_forecast_publications "
                "WHERE publication_id=%s",
                (report["publication_id"],),
            ).fetchone()
            if not saved:
                raise ValueError("forecast_publication_missing")
            if saved["manifest"] != report:
                raise ValueError("published_forecast_manifest_mismatch")
            published_rows = db.execute(
                "SELECT station_id,forecast_origin_utc,target_hour_utc,"
                "horizon_h,prediction_type,predicted_value,status,reason,"
                "model_id,feature_available_at FROM ml_forecasts "
                "WHERE publication_id=%s",
                (report["publication_id"],),
            ).fetchall()

        def row_key(r: dict[str, Any]) -> tuple[str, str, int]:
            return r["station_id"], r["prediction_type"], r["horizon_h"]

        if sorted(rows, key=row_key) != sorted(published_rows, key=row_key):
            raise ValueError("published_forecast_rows_mismatch")
        provenance["published_at"] = saved["published_at"].isoformat()
        provenance["published_rows_sha256"] = identity(
            sorted(published_rows, key=row_key)
        )
        quality = read_json(gbfs_path / "quality.json")
        if (
            quality["pipeline_version"] != GBFS_PIPELINE
            or checksum(gbfs_path / "station_status.parquet") != quality["checksum"]
        ):
            raise ValueError("gbfs_silver_checksum_or_version_mismatch")
        bronze = gbfs_path.parents[2] / "bronze" / quality["collection_id"]
        manifest = read_json(bronze / "manifest.json")
        feeds = manifest["feeds"]
        for name, feed in feeds.items():
            safe_url(feed["url"])
            if checksum(bronze / (name + ".json")) != feed["checksum"]:
                raise ValueError("gbfs_bronze_checksum_mismatch")
        rebuilt, _, _ = normalize(
            read_json(bronze / "station_information.json"),
            read_json(bronze / "station_status.json"),
            {
                **feeds["station_information"],
                **envelope(
                    read_json(bronze / "station_information.json"),
                    utc(feeds["station_information"]["received_at_utc"]),
                ),
            },
            {
                **feeds["station_status"],
                **envelope(
                    read_json(bronze / "station_status.json"),
                    utc(feeds["station_status"]["received_at_utc"]),
                ),
            },
            quality["collection_id"],
        )
        gbfs_rows = parquet_rows(gbfs_path / "station_status.parquet")
        for r in gbfs_rows:
            for k in ("received_at_utc", "last_reported"):
                if r[k] is not None:
                    r[k] = r[k].isoformat()
        if sorted(rebuilt, key=lambda r: r["station_id"]) != sorted(
            gbfs_rows, key=lambda r: r["station_id"]
        ):
            raise ValueError("gbfs_raw_silver_mismatch")
        provenance.update(
            {
                "gbfs_collection_id": quality["collection_id"],
                "gbfs_quality_sha256": checksum(gbfs_path / "quality.json"),
                "gbfs_silver_sha256": quality["checksum"],
                "gbfs_bronze_manifest_sha256": checksum(bronze / "manifest.json"),
                "gbfs_feeds": feeds,
                "station_mapping": "Unique same-collection GBFS short_name -> "
                "trip station_id; no fuzzy/current-to-historical backfill",
            }
        )
        return adapt(
            report,
            rows,
            saved["published_at"],
            gbfs_rows,
            feeds,
            as_of,
            config,
            provenance,
            requested,
        )
    except (ValueError, TypeError, KeyError, OSError, psycopg.Error) as exc:
        # No numeric risk for unverifiable inputs; reason never leaks DB credentials.
        code = (
            "database_unavailable" if isinstance(exc, psycopg.Error) else str(exc)[:500]
        )
        return None, {
            "status": "unavailable",
            "reasons": [f"{type(exc).__name__}:{code}"],
            "as_of": utc(as_of).isoformat(),
            "provenance": provenance,
            "coverage": None,
            "risk": None,
        }
