"""All source CSVs -> row ledger/quarantine/Silver -> verified hourly Gold."""

import json
import shutil
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.gold import build_gold
from citibike.contracts import (
    FIELDS,
    PIPELINE_VERSION,
    SCHEMA_VERSION,
    period_bounds,
    version_id,
)
from citibike.ingestion.archive import SourceManifest, checksum
from citibike.quality.rules import critical_reasons


def process(
    source: Path,
    root: Path,
    *,
    pipeline_version: str = PIPELINE_VERSION,
    memory_limit_mb: int = 512,
) -> Path:
    if not 256 <= memory_limit_mb <= 2048:
        raise ValueError("Memory limit must be between 256 and 2048 MB")
    manifest = SourceManifest.model_validate_json(
        (source / "manifest.json").read_text(encoding="utf-8")
    )
    if checksum(source / "source.zip") != manifest.checksum:
        raise ValueError("Bronze checksum mismatch")
    for csv_object in manifest.csv_files:
        if set(csv_object.columns) != set(FIELDS):
            raise ValueError(f"Unsupported source schema in {csv_object.path}")
    start, end = period_bounds(manifest.period)
    dataset_id = version_id(manifest.checksum, pipeline_version)
    destination = root / "processed" / dataset_id
    if destination.exists():
        verify_artifacts(destination)
        return destination
    work = root / "work"
    work.mkdir(parents=True, exist_ok=True)
    before = time.monotonic()
    with tempfile.TemporaryDirectory(dir=work) as working:
        staging = Path(working) / "result"
        staging.mkdir()
        db = duckdb.connect(str(Path(working) / "transform.duckdb"))
        try:
            db.execute(
                f"SET memory_limit='{memory_limit_mb}MB'; "
                "SET threads=2; SET TimeZone='UTC'"
            )
            db.execute("SET temp_directory = ?", [str(Path(working) / "spill")])
            db.execute(
                """
                CREATE TABLE context AS SELECT ?::TIMESTAMPTZ period_start,
                ?::TIMESTAMPTZ period_end, ?::TIMESTAMPTZ retrieved_at
            """,
                [start, end, manifest.retrieved_at],
            )
            raw_columns = ",".join(f"{name}_raw VARCHAR" for name in FIELDS)
            db.execute(
                f"CREATE TABLE raw ({raw_columns}, source_file VARCHAR, "
                "source_row_number BIGINT, source_object_id VARCHAR)"
            )
            files: list[dict[str, Any]] = []
            with zipfile.ZipFile(source / "source.zip") as archive:
                for index, csv_object in enumerate(manifest.csv_files):
                    path = Path(working) / f"input-{index}.csv"
                    with (
                        archive.open(csv_object.path) as incoming,
                        path.open("wb") as output,
                    ):
                        shutil.copyfileobj(incoming, output, length=1024 * 1024)
                    projection = ",".join(f'"{name}"' for name in FIELDS)
                    nonnull = ",".join(f"'{name}'" for name in FIELDS)
                    db.execute(
                        f"""
                        INSERT INTO raw SELECT {projection}, ?, row_number() OVER (), ?
                        FROM read_csv(?, all_varchar=true, header=true, parallel=false,
                             force_not_null=[{nonnull}], strict_mode=true)
                    """,
                        [csv_object.path, manifest.checksum, str(path)],
                    )
                    count = db.execute(
                        "SELECT count(*) FROM raw WHERE source_file=?",
                        [csv_object.path],
                    ).fetchone()
                    assert count is not None
                    files.append({"file": csv_object.path, "input_rows": count[0]})
                    path.unlink()
            total = sum(item["input_rows"] for item in files)
            if pipeline_version != PIPELINE_VERSION:
                db.execute("SET preserve_insertion_order=false")
            if total == 0:
                raise ValueError("Empty source cannot be published")
            # Inspect the unchanged input before applying acceptance/canonicalization.
            raw_nulls = {}
            for field in FIELDS:
                observed = db.execute(
                    f"SELECT count(*) FROM raw WHERE trim({field}_raw)='' "
                    f"OR {field}_raw IS NULL"
                ).fetchone()
                assert observed is not None
                raw_nulls[field] = int(observed[0])
            categories = {
                field: dict(
                    db.execute(
                        f"SELECT {field}_raw,count(*) FROM raw GROUP BY 1"
                    ).fetchall()
                )
                for field in ["rideable_type", "member_casual"]
            }
            fingerprint = ",".join(f"{name}:={name}_raw" for name in FIELDS)
            db.execute(f"""
                CREATE TABLE typed AS SELECT *,
                nullif(trim(ride_id_raw),'') ride_id,
                nullif(trim(start_station_id_raw),'') start_station_id,
                nullif(trim(end_station_id_raw),'') end_station_id,
                try_cast(started_at_raw AS TIMESTAMP) start_local,
                try_cast(ended_at_raw AS TIMESTAMP) end_local,
                sha256(to_json(struct_pack({fingerprint}))) record_hash
                FROM raw;
                CREATE VIEW timed AS SELECT *,
                timezone('America/New_York',start_local) start_candidate,
                timezone('America/New_York',end_local) end_candidate
                FROM typed;
                CREATE TABLE checked AS SELECT *, {critical_reasons()} critical_reasons
                FROM timed;
                CREATE TABLE identities AS SELECT ride_id,
                count(DISTINCT record_hash) variants FROM checked
                WHERE ride_id IS NOT NULL GROUP BY 1;
                CREATE TABLE classified AS SELECT c.*, CASE
                    WHEN i.variants>1 THEN 'conflicting_id'
                    WHEN len(critical_reasons)>0 THEN 'invalid'
                    WHEN row_number() OVER (PARTITION BY c.ride_id
                         ORDER BY source_file,source_row_number)>1
                         THEN 'duplicate_exact'
                    ELSE 'accepted' END disposition
                FROM checked c LEFT JOIN identities i ON c.ride_id=i.ride_id
            """)
            db.execute("""
                CREATE VIEW flagged AS SELECT *, list_concat(critical_reasons,
                list_filter([
                    CASE WHEN disposition<>'accepted' THEN disposition END,
                    CASE WHEN start_station_id IS NULL THEN 'missing_start_station' END,
                    CASE WHEN end_station_id IS NULL THEN 'missing_end_station' END,
                    CASE WHEN end_candidate-start_candidate < INTERVAL '60 seconds'
                         THEN 'short_duration' END,
                    CASE WHEN end_candidate-start_candidate > INTERVAL '24 hours'
                         THEN 'long_duration' END,
                    CASE WHEN start_candidate < (SELECT period_start FROM context)
                           OR start_candidate >= (SELECT period_end FROM context)
                         THEN 'outside_archive_period' END,
                    CASE WHEN member_casual_raw NOT IN ('member','casual')
                         THEN 'unknown_member_category' END,
                    CASE WHEN rideable_type_raw NOT IN
                         ('classic_bike','electric_bike','docked_bike')
                         THEN 'unknown_rideable_category' END,
                    CASE WHEN start_lat_raw='' OR start_lng_raw=''
                           OR end_lat_raw='' OR end_lng_raw=''
                         THEN 'missing_coordinates' END,
                    CASE WHEN (start_lat_raw<>'' AND (try_cast(start_lat_raw AS DOUBLE)
                               IS NULL OR try_cast(start_lat_raw AS DOUBLE)
                               NOT BETWEEN -90 AND 90))
                           OR (end_lat_raw<>'' AND (try_cast(end_lat_raw AS DOUBLE)
                               IS NULL OR try_cast(end_lat_raw AS DOUBLE)
                               NOT BETWEEN -90 AND 90))
                           OR (start_lng_raw<>'' AND (try_cast(start_lng_raw AS DOUBLE)
                               IS NULL OR try_cast(start_lng_raw AS DOUBLE)
                               NOT BETWEEN -180 AND 180))
                           OR (end_lng_raw<>'' AND (try_cast(end_lng_raw AS DOUBLE)
                               IS NULL OR try_cast(end_lng_raw AS DOUBLE)
                               NOT BETWEEN -180 AND 180))
                         THEN 'invalid_coordinates' END
                ], r -> r IS NOT NULL)) quality_reason_codes FROM classified;
                CREATE TABLE silver AS SELECT *, start_local started_at_local,
                    end_local ended_at_local, start_candidate started_at_utc,
                    end_candidate ended_at_utc,
                    started_at_raw started_at, ended_at_raw ended_at,
                    nullif(trim(rideable_type_raw),'') rideable_type,
                    nullif(trim(member_casual_raw),'') member_casual,
                    nullif(start_station_name_raw,'') start_station_name,
                    nullif(end_station_name_raw,'') end_station_name,
                    CASE WHEN try_cast(start_lat_raw AS DOUBLE) BETWEEN -90 AND 90
                         THEN try_cast(start_lat_raw AS DOUBLE) END start_lat,
                    CASE WHEN try_cast(start_lng_raw AS DOUBLE) BETWEEN -180 AND 180
                         THEN try_cast(start_lng_raw AS DOUBLE) END start_lng,
                    CASE WHEN try_cast(end_lat_raw AS DOUBLE) BETWEEN -90 AND 90
                         THEN try_cast(end_lat_raw AS DOUBLE) END end_lat,
                    CASE WHEN try_cast(end_lng_raw AS DOUBLE) BETWEEN -180 AND 180
                         THEN try_cast(end_lng_raw AS DOUBLE) END end_lng,
                    date_diff('microsecond',start_candidate,end_candidate)/1000000.0
                         duration_seconds,
                    CASE WHEN len(quality_reason_codes)>0 THEN 'warning'
                         ELSE 'valid' END
                         quality_status
                FROM flagged WHERE disposition='accepted';
            """)
            # Version and processing time are explicit lineage columns in Silver.
            db.execute("ALTER TABLE silver ADD COLUMN schema_version VARCHAR")
            db.execute("ALTER TABLE silver ADD COLUMN pipeline_version VARCHAR")
            db.execute("ALTER TABLE silver ADD COLUMN processed_at_utc TIMESTAMPTZ")
            db.execute(
                "UPDATE silver SET schema_version=?,pipeline_version=?,"
                "processed_at_utc=?",
                [SCHEMA_VERSION, pipeline_version, datetime.now(UTC)],
            )
            dispositions = dict(
                db.execute(
                    "SELECT disposition,count(*) FROM classified GROUP BY 1"
                ).fetchall()
            )
            if sum(dispositions.values()) != total:
                raise ValueError("Row reconciliation failed")
            for item in files:
                item["dispositions"] = dict(
                    db.execute(
                        "SELECT disposition,count(*) FROM classified "
                        "WHERE source_file=? GROUP BY 1",
                        [item["file"]],
                    ).fetchall()
                )
            for table, query in {
                "silver": "SELECT * FROM silver",
                "quarantine": "SELECT * FROM flagged WHERE "
                "disposition IN ('invalid','conflicting_id')",
                "row_ledger": "SELECT source_object_id,source_file,source_row_number,"
                "ride_id,"
                "record_hash,disposition,quality_reason_codes FROM flagged",
            }.items():
                db.execute(
                    f"COPY ({query}) TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
                    [str(staging / f"{table}.parquet")],
                )
            if dispositions.get("accepted", 0) == 0:
                failed = root / "failed" / dataset_id
                failed.parent.mkdir(parents=True, exist_ok=True)
                if not failed.exists():
                    shutil.copytree(staging, failed)
                raise ValueError(
                    "No canonical rows; quarantine retained, publication blocked"
                )
            metrics = build_gold(db, staging, f"{dataset_id}/silver.parquet")
            profile = db.execute("""
                SELECT min(started_at_utc),max(started_at_utc),max(ended_at_utc),
                       min(duration_seconds),max(duration_seconds),
                       count(*) FILTER (WHERE quality_status='warning')
                FROM silver
            """).fetchone()
            assert profile is not None
            reasons = dict(
                db.execute("""
                SELECT reason,count(*) FROM (
                    SELECT unnest(quality_reason_codes) reason FROM flagged
                ) GROUP BY 1
            """).fetchall()
            )
            report = {
                "dataset_id": dataset_id,
                "pipeline_version": pipeline_version,
                "schema_version": SCHEMA_VERSION,
                "source": manifest.model_dump(mode="json"),
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
                "timezone": "America/New_York",
                "scope": "NYC",
                "input_rows": total,
                "dispositions": dispositions,
                "files": files,
                "metrics": metrics,
                "raw_missing_fields": raw_nulls,
                "raw_categories": categories,
                "reason_counts": reasons,
                "data_as_of": profile[2].isoformat(),
                "profile": {
                    "start_min": profile[0].isoformat(),
                    "start_max": profile[1].isoformat(),
                    "duration_min": profile[3],
                    "duration_max": profile[4],
                    "warning_rows": profile[5],
                },
                "quality_status": "warning"
                if profile[5] or len(dispositions) > 1
                else "pass",
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "memory_limit": f"{memory_limit_mb}MB",
                "threads": 2,
                "rules": {
                    "identity": "Exact duplicates: first file/row; "
                    "all conflicting IDs quarantined",
                    "eligibility": "ID, valid unambiguous NY times; "
                    "end>=start and end<=retrieved_at",
                    "station_missing": "Accepted for ride count; "
                    "excluded only from respective station metric",
                    "duration": "No extra duration cutoff; "
                    "short/long durations flagged, not removed",
                    "scope": "Official NYC archive selection; "
                    "JC excluded as scope, not a quality error",
                },
            }
            report["artifact_checksums"] = {
                p.name: checksum(p) for p in staging.glob("*.parquet")
            }
            (staging / "quality.json").write_text(
                json.dumps(report, indent=2), encoding="utf-8"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)
        finally:
            db.close()
    return destination


def verify_artifacts(result: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(
        (result / "quality.json").read_text(encoding="utf-8")
    )
    for name, digest in report["artifact_checksums"].items():
        if checksum(result / name) != digest:
            raise ValueError("Processed artifact checksum mismatch")
    return report
