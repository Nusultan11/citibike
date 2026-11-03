"""Fail-closed F7 gates, reusing F2/F4 validation and strict PIT recipes."""

import importlib.metadata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citibike.features.contracts import (
    MAX_AGE_DAYS,
    SCHEMA,
    TYPES,
    code_hash,
    utc,
)
from citibike.forecasting.artifacts import checked
from citibike.ingestion.archive import SourceManifest, checksum
from citibike.ingestion.gbfs import GBFS_PIPELINE, envelope, normalize
from citibike.processing.schemas import detect
from citibike.risk.inputs import parquet_rows
from citibike.storage import read_json


class GateRejected(ValueError):
    """Stable reason code, safe for structured alerts; not retryable."""


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise GateRejected(reason)


def bronze(path: Path) -> None:
    source = SourceManifest.model_validate(read_json(path / "manifest.json"))
    require(checksum(path / "source.zip") == source.checksum, "bronze_checksum")
    for csv in source.csv_files:
        try:
            detect(csv.columns)
        except ValueError:
            raise GateRejected("source_schema_drift") from None


def gbfs(path: Path, as_of: datetime) -> dict[str, Any]:
    """Verify originals, normalized parity and conservative F3/F5 freshness."""
    quality = read_json(path / "quality.json")
    require(quality["pipeline_version"] == GBFS_PIPELINE, "gbfs_schema_drift")
    require(
        checksum(path / "station_status.parquet") == quality["checksum"],
        "gbfs_checksum",
    )
    original = path.parents[2] / "bronze" / quality["collection_id"]
    manifest = read_json(original / "manifest.json")
    feeds = manifest["feeds"]
    for name, meta in feeds.items():
        require(
            checksum(original / f"{name}.json") == meta["checksum"],
            "gbfs_bronze_checksum",
        )
    metadata = {}
    for name in ("station_information", "station_status"):
        meta = feeds[name]
        raw = read_json(original / f"{name}.json")
        recalculated = envelope(raw, utc(meta["received_at_utc"]))
        for key in ("version", "last_updated", "ttl", "received_at_utc"):
            require(meta[key] == recalculated[key], "gbfs_envelope_parity")
        received, updated = utc(meta["received_at_utc"]), utc(meta["last_updated"])
        require(received <= as_of and updated <= as_of, "gbfs_future_input")
        require(meta["ttl"] > 0, "gbfs_nonpositive_ttl")
        expiry = min(
            received + timedelta(seconds=60), updated + timedelta(seconds=meta["ttl"])
        )
        require(as_of < expiry, "gbfs_stale_input")
        metadata[name] = {**meta, **recalculated}
    rows, rejected, _ = normalize(
        read_json(original / "station_information.json"),
        read_json(original / "station_status.json"),
        metadata["station_information"],
        metadata["station_status"],
        quality["collection_id"],
    )
    actual = parquet_rows(path / "station_status.parquet")
    for row in actual:
        for key in ("received_at_utc", "last_reported"):
            if row[key] is not None:
                row[key] = row[key].isoformat()
    require(
        sorted(rows, key=lambda r: r["station_id"])
        == sorted(actual, key=lambda r: r["station_id"]),
        "gbfs_train_serve_parity",
    )
    require(
        len(rows) + len(rejected) == quality["input_station_ids"], "gbfs_reconciliation"
    )
    valid = sum(r["quality_status"] == "valid" for r in rows)
    require(valid > 0, "gbfs_no_valid_stations")
    return {
        "collection_id": quality["collection_id"],
        "checksum": quality["checksum"],
        "quality": quality["quality_counts"],
        "coverage": valid / quality["input_station_ids"],
        "source_age_seconds": (
            as_of - utc(feeds["station_status"]["last_updated"])
        ).total_seconds(),
        "feeds": feeds,
    }


def model(
    path: Path, origin: datetime, *, baseline_only: bool = True
) -> dict[str, Any]:
    bundle = checked(path)
    data = checked(Path(bundle["dataset_path"]))
    require(bundle["kind"] == "model_bundle", "model_kind")
    require(
        bundle["feature_schema"] == data["feature_schema"] == SCHEMA,
        "feature_schema_drift",
    )
    require(bundle["dataset_id"] == data["id"], "model_dataset_lineage")
    require(
        bundle["feature_recipe"]
        == data["recipe"]
        == code_hash(Path(__file__).parents[1] / "features"),
        "train_serve_parity",
    )
    for key in ("source_snapshot_id", "gold_publication_id"):
        require(bundle[key] == data[key], "model_source_lineage")
    available = max(
        utc(bundle["created_at"]),
        utc(data["created_at"]),
        utc(data["strict_source_available_at"]),
    )
    require(available <= origin <= datetime.now(UTC), "pit_unavailable_at_origin")
    age = origin - utc(data["source_event_end"])
    require(
        timedelta(0) <= age <= timedelta(days=MAX_AGE_DAYS), "source_event_window_stale"
    )
    if baseline_only:
        require(
            all(bundle["models"][k]["name"] == "seasonal_naive" for k in TYPES),
            "operational_model_not_approved",
        )
    for package, version in bundle["environment"].items():
        require(importlib.metadata.version(package) == version, "model_runtime_parity")
    return bundle
