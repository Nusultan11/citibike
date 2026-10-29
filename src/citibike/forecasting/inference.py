"""Strict, versioned 24-hour batch; no training or file reads from HTTP."""

import importlib.metadata
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from citibike.features.contracts import (
    CATALOG,
    MAX_AGE_DAYS,
    SCHEMA,
    TYPES,
    code_hash,
    pit_gate,
    target_hour,
    utc,
    valid_prediction,
)
from citibike.features.dataset import inference_features
from citibike.forecasting import models
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity


def train(root: Path, selection: Path) -> Path:
    chosen = checked(selection)
    data_path = Path(chosen["dataset_path"])
    data = checked(data_path)
    if chosen["dataset_id"] != data["id"]:
        raise ValueError("Selection/dataset version mismatch")
    if chosen["feature_schema"] != SCHEMA or data["feature_schema"] != SCHEMA:
        raise ValueError("Model/feature schema mismatch")
    now = datetime.now(UTC)
    if utc(data["strict_source_available_at"]) > now or utc(data["created_at"]) > now:
        raise ValueError("Training immutable inputs not yet available")
    environment = {
        p: importlib.metadata.version(p)
        for p in ("numpy", "pandas", "scikit-learn", "lightgbm", "catboost", "duckdb")
    }
    recipe = checksum(Path(__file__))
    identifier = identity(
        ["production-fit", chosen["id"], data["id"], SCHEMA, recipe, environment]
    )

    def fit_bundle(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        trained = {}
        with duckdb.connect() as db:
            db.execute("SET threads=2; SET memory_limit='512MB'; SET TimeZone='UTC'")
            db.read_parquet(str(data_path / "examples.parquet")).create_view("examples")
            for kind in TYPES:
                name = chosen["decisions"][kind]["champion"]
                model_id = identity([identifier, kind, name])
                if name == "seasonal_naive":
                    fitted = {
                        "algorithm": "seasonal_naive",
                        "feature_names": list(CATALOG),
                        "config": {"algorithm": "seasonal_naive"},
                        "model": None,
                    }
                    measurement = {
                        "fit_seconds": 0.0,
                        "training_rows": 0,
                        "fit": (
                            "delayed seasonal recipe; immutable profiles "
                            "are dataset artifact"
                        ),
                    }
                else:
                    config = checked(Path(chosen["experiment_path"]))["configuration"][
                        name
                    ]
                    frame = db.execute(
                        """SELECT * FROM examples WHERE prediction_type=?
                        AND eligibility='eligible' AND target_hour_utc<?
                        ORDER BY sha256(station_id||forecast_origin_utc::VARCHAR||'42'),
                            horizon_h LIMIT ?""",
                        [kind, now, data["config"]["max_train_rows"]],
                    ).df()
                    fitted, measurement = models.fit(frame, config)
                models.save(stage / f"{kind}.joblib", fitted)
                trained[kind] = {
                    "model_id": model_id,
                    "name": name,
                    "file": f"{kind}.joblib",
                    "measurements": measurement,
                }
        return {
            "kind": "model_bundle",
            "selection_id": chosen["id"],
            "dataset_id": data["id"],
            "dataset_path": str(data_path),
            "feature_schema": SCHEMA,
            "models": trained,
            "training_basis": (
                "strict currently available immutable snapshot; "
                "historical experiment approximate"
            ),
            "historical_accuracy_mode": data["availability_mode"],
            "environment": environment,
            "model_code_checksum": recipe,
            "feature_recipe": data["recipe"],
            "source_snapshot_id": data["source_snapshot_id"],
            "gold_publication_id": data["gold_publication_id"],
            "strict_fit_asof": now.isoformat(),
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(root, "models", identifier, fit_bundle)


def rows_valid(rows: list[dict[str, Any]], stations: list[str]) -> None:
    keys = set()
    expected = {(s, k, h) for s in stations for k in TYPES for h in range(1, 25)}
    for r in rows:
        valid_prediction(r)
        key = (r["station_id"], r["prediction_type"], r["horizon_h"])
        if key in keys:
            raise ValueError("Duplicate forecast ML grain")
        keys.add(key)
    if keys != expected:
        raise ValueError(
            "Forecast must contain 24 hours × 2 types per requested station"
        )


def predict(
    root: Path,
    bundle_path: Path,
    origin: datetime | None = None,
    stations: list[str] | None = None,
) -> Path:
    before = time.monotonic()
    bundle = checked(bundle_path)
    dataset_path = Path(bundle["dataset_path"])
    data = checked(dataset_path)
    if bundle["dataset_id"] != data["id"]:
        raise ValueError("Model/dataset version mismatch")
    if bundle["feature_schema"] != SCHEMA or data["feature_schema"] != SCHEMA:
        raise ValueError("Model/feature schema mismatch")
    if data["recipe"] != code_hash(Path(__file__).parent.parent / "features"):
        raise ValueError("Feature builder recipe mismatch; rebuild dataset")
    for package, version in bundle["environment"].items():
        if importlib.metadata.version(package) != version:
            raise ValueError("Model runtime environment mismatch")
    origin = utc(origin or datetime.now(UTC))
    if origin > datetime.now(UTC):
        raise ValueError("Forecast origin is actual decision time, not a future date")
    with duckdb.connect() as db:
        names = [
            r[0]
            for r in db.execute(
                "SELECT station_id FROM read_parquet(?) ORDER BY 1",
                [str(dataset_path / "stations.parquet")],
            ).fetchall()
        ]
    requested = sorted(set(stations or names))
    if (
        not requested
        or len(requested) > 256
        or any(not s or len(s) > 80 for s in requested)
    ):
        raise ValueError("Invalid bounded station cohort")
    # Source/model schema errors block instead of quietly substituting a model.
    available = max(
        utc(bundle["created_at"]),
        utc(data["created_at"]),
        utc(data["strict_source_available_at"]),
    )
    if available > origin:
        raise ValueError("Strict immutable model/data inputs unavailable at origin")
    recipe = checksum(Path(__file__))
    identifier = identity([bundle["id"], origin.isoformat(), requested, SCHEMA, recipe])

    def calculate(stage: Path) -> dict[str, Any]:
        features = inference_features(dataset_path, origin, requested)
        by_key = {
            (r["station_id"], r["prediction_type"], r["horizon_h"]): r for r in features
        }
        rows = []
        stale = origin - utc(data["source_event_end"]) > timedelta(days=MAX_AGE_DAYS)
        for station in requested:
            for kind in TYPES:
                model = bundle["models"][kind]
                fitted = models.load(bundle_path / model["file"])
                for horizon in range(1, 25):
                    feature = by_key.get((station, kind, horizon))
                    value = None
                    reason = None
                    if stale:
                        reason = "source_event_window_stale"
                    elif feature is None:
                        reason = "unknown_station_id"
                    elif utc(feature["known_at"]) > origin:
                        reason = "station_not_known_at_origin"
                    elif origin - utc(feature["last_event"]) > timedelta(
                        days=MAX_AGE_DAYS
                    ):
                        reason = (
                            "station_history_stale; current operating status unknown"
                        )
                    elif (
                        feature["seasonal_naive"] is None
                        or feature["station_mean"] is None
                    ):
                        reason = "no_available_seasonal_history"
                    else:
                        pit_gate(
                            origin,
                            feature["feature_available_at"],
                            feature["max_event_at"],
                            mode="strict",
                            strict_available_at=available,
                        )
                        pd = __import__("pandas")
                        values, _elapsed = models.predict(
                            fitted, pd.DataFrame([feature])
                        )
                        value = values[0]
                    rows.append(
                        {
                            "station_id": station,
                            "forecast_origin_utc": origin.isoformat(),
                            "target_hour_utc": target_hour(origin, horizon).isoformat(),
                            "horizon_h": horizon,
                            "prediction_type": kind,
                            "predicted_value": value,
                            "status": "unavailable" if reason else "ok",
                            "reason": reason,
                            "model_id": model["model_id"],
                            "feature_available_at": max(
                                available, utc(feature["feature_available_at"])
                            ).isoformat()
                            if feature and feature["feature_available_at"]
                            else available.isoformat(),
                        }
                    )
        rows_valid(rows, requested)
        from citibike.storage import write_json

        write_json(stage / "forecasts.json", rows)
        with duckdb.connect() as db:
            db.execute(
                """COPY(SELECT * FROM read_json($input,columns={
                station_id:'VARCHAR',forecast_origin_utc:'TIMESTAMPTZ',
                target_hour_utc:'TIMESTAMPTZ',horizon_h:'INTEGER',prediction_type:'VARCHAR',
                predicted_value:'DOUBLE',status:'VARCHAR',reason:'VARCHAR',model_id:'VARCHAR',
                feature_available_at:'TIMESTAMPTZ'})) TO $output (FORMAT PARQUET)""",
                {
                    "input": str(stage / "forecasts.json"),
                    "output": str(stage / "forecasts.parquet"),
                },
            )
        good = sum(r["status"] == "ok" for r in rows)
        return {
            "kind": "forecast",
            "publication_id": identifier,
            "model_bundle_id": bundle["id"],
            "model_bundle_path": str(bundle_path),
            "dataset_id": data["id"],
            "feature_schema": SCHEMA,
            "forecast_recipe": recipe,
            "station_version": data["checksums"]["stations.parquet"],
            "source_snapshot_id": data["source_snapshot_id"],
            "gold_publication_id": data["gold_publication_id"],
            "forecast_origin_utc": origin.isoformat(),
            "issued_at": datetime.now(UTC).isoformat(),
            "data_as_of": data["source_event_end"],
            "data_as_of_kind": "source event window end",
            "strict_source_available_at": available.isoformat(),
            "valid_until": (target_hour(origin, 24) + timedelta(hours=1)).isoformat(),
            "station_ids": requested,
            "rows": len(rows),
            "valid_rows": good,
            "unavailable_rows": len(rows) - good,
            "quality_status": "warning",
            "status": "ok" if good else "unavailable",
            "availability_mode": "strict",
            "accuracy_evidence_mode": data["availability_mode"],
            "freshness": {
                "max_source_age_days": MAX_AGE_DAYS,
                "source_age_days": (
                    origin - utc(data["source_event_end"])
                ).total_seconds()
                / 86400,
            },
            "limitations": (
                "Published observed trip counts; no weather/GBFS; station "
                "operating status unknown; no prediction intervals; "
                "retrospective accuracy not operational uplift"
            ),
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(root, "forecasts", identifier, calculate)
