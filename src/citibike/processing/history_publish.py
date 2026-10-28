"""Cross-source canonicalization and atomic historical Silver manifest publication."""

import string
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.ingestion.archive import checksum
from citibike.ingestion.inventory import shift_month
from citibike.processing.historical import HISTORY_VERSION
from citibike.processing.resources import peak_rss_bytes
from citibike.processing.trips import verify_artifacts
from citibike.storage import identity, read_json, write_json

KEYS = "source_object_id,source_file,source_row_number"
PUBLICATION_VERSION = "f2.0.0-silver.2"


def global_changes(states: list[dict[str, Any]], target: Path) -> dict[str, Any]:
    before = time.monotonic()
    with duckdb.connect(str(target / "identity.duckdb")) as db:
        db.execute(
            "SET memory_limit='2GB'; SET threads=2; SET preserve_insertion_order=false"
        )
        db.execute(
            "CREATE TABLE changes (source_object_id "
            "VARCHAR,source_file VARCHAR,source_row_number "
            "BIGINT,disposition VARCHAR)"
        )
        db.execute("CREATE TABLE independent_changes AS SELECT * FROM changes")
        cross_ids = 0
        for bucket in string.hexdigits[:16]:
            paths = [
                str(p)
                for s in states
                for p in Path(s["identity"]).glob(f"bucket={bucket}/*.parquet")
            ]
            audit_paths = [
                str(p)
                for s in states
                for p in (Path(s["audit"]) / "identity").glob(
                    f"bucket={bucket}/*.parquet"
                )
            ]
            if not paths:
                continue
            if not audit_paths:
                raise ValueError("Independent identity partitions missing")
            db.read_parquet(paths, hive_partitioning=False).create_view(
                "input_ids", replace=True
            )
            db.execute("""CREATE OR REPLACE TABLE cross_keys AS
                SELECT ride_id FROM input_ids GROUP BY 1
                HAVING min(source_object_id)<>max(source_object_id)""")
            count = db.execute("SELECT count(*) FROM cross_keys").fetchone()
            assert count is not None
            cross_ids += count[0]
            db.execute("""CREATE OR REPLACE TABLE resolutions AS SELECT ride_id,
                count(DISTINCT record_hash) variants,
                min(CASE WHEN disposition IN ('accepted','duplicate_exact')
                    THEN struct_pack(p:=period,f:=source_file,n:=source_row_number)
                    END) keeper
                FROM input_ids JOIN cross_keys USING(ride_id) GROUP BY 1;
                INSERT INTO changes SELECT
                source_object_id,source_file,source_row_number,
                    CASE WHEN variants>1 THEN 'conflicting_id'
                         WHEN disposition='invalid' THEN 'invalid'
                         WHEN
                         struct_pack(p:=period,f:=source_file,n:=source_row_number)<>keeper
                             THEN 'duplicate_exact' ELSE 'accepted' END final_status
                FROM input_ids JOIN resolutions USING(ride_id)
                WHERE final_status<>disposition""")
            # Separate raw-CSV fingerprints/eligibility and an independent window-based
            # decision query, rather than invoking the transform's resolution helper.
            db.read_parquet(audit_paths, hive_partitioning=False).create_view(
                "raw_audit", replace=True
            )
            db.execute("""INSERT INTO independent_changes
                WITH candidates AS (
                    SELECT * FROM raw_audit WHERE ride_id IN (
                        SELECT ride_id FROM raw_audit GROUP BY ride_id
                        HAVING count(DISTINCT period)>1)),
                examined AS (
                    SELECT *,min(record_hash) OVER (PARTITION BY ride_id) low_hash,
                        max(record_hash) OVER (PARTITION BY ride_id) high_hash,
                        min(CASE WHEN eligible=1 THEN
                        row(period,source_file,source_row_number) END)
                            OVER (PARTITION BY ride_id) canonical FROM candidates),
                judged AS (SELECT *,CASE WHEN low_hash<>high_hash THEN 'conflicting_id'
                    WHEN eligible=0 THEN 'invalid'
                    WHEN row(period,source_file,source_row_number)=canonical THEN
                    'accepted'
                    ELSE 'duplicate_exact' END final_status FROM examined)
                SELECT source_object_id,source_file,source_row_number,final_status
                FROM judged WHERE final_status<>disposition""")
            print(f"Cross-source identity bucket {bucket} checked", flush=True)
        mismatch = db.execute("""SELECT count(*) FROM (
            (SELECT * FROM changes EXCEPT ALL SELECT * FROM independent_changes)
            UNION ALL (SELECT * FROM independent_changes EXCEPT ALL SELECT * FROM
            changes))""").fetchone()
        if mismatch != (0,):
            raise ValueError("Independent cross-month row decisions differ")
        duplicate_keys = db.execute(
            f"SELECT count(*) FROM (SELECT {KEYS} FROM changes "
            "GROUP BY ALL HAVING count(*)>1)"
        ).fetchone()
        if duplicate_keys != (0,):
            raise ValueError("Cross-source override grain violated")
        db.execute(
            "COPY changes TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
            [str(target / "cross_source_changes.parquet")],
        )
        totals = dict(
            db.execute("SELECT disposition,count(*) FROM changes GROUP BY 1").fetchall()
        )
    # This owned work DB is not a published data artifact.
    (target / "identity.duckdb").unlink()
    return {
        "cross_source_ride_ids": cross_ids,
        "changed_row_counts": totals,
        "independent_row_mismatches": 0,
        "elapsed_seconds": round(time.monotonic() - before, 3),
    }


def effective_partition(
    state: dict[str, Any], changes: Path, root: Path
) -> dict[str, Any]:
    base = Path(state["result"])
    source_id = state["source_object_id"]
    with duckdb.connect() as db:
        db.execute("SET memory_limit='512MB'; SET threads=2; SET TimeZone='UTC'")
        overrides = db.execute(
            (
                "SELECT source_file,source_row_number,disposition FROM "
                "read_parquet(?) WHERE source_object_id=? ORDER BY 1,2"
            ),
            [str(changes), source_id],
        ).fetchall()
    partition_id = identity([base.name, PUBLICATION_VERSION, overrides])
    destination = root / "f2" / "silver" / partition_id
    if destination.exists():
        report = read_json(destination / "quality.json")
        for name, expected in report["checksums"].items():
            if checksum(destination / name) != expected:
                raise ValueError("Published historical Silver artifact changed")
        return {
            "partition_id": partition_id,
            "path": str(destination),
            "reused": True,
            **report,
        }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as working:
        stage = Path(working) / "partition"
        stage.mkdir()
        with duckdb.connect(str(Path(working) / "effective.duckdb")) as db:
            db.execute("SET memory_limit='2GB'; SET threads=2; SET TimeZone='UTC'")
            db.execute("SET preserve_insertion_order=false")
            db.read_parquet(str(changes)).create_view("all_changes")
            db.execute(
                (
                    "CREATE TEMP TABLE edits AS SELECT * FROM all_changes "
                    "WHERE source_object_id=?"
                ),
                [source_id],
            )
            db.read_parquet(str(base / "row_ledger.parquet")).create_view("base_ledger")
            db.execute(
                f"""CREATE TABLE ledger AS SELECT b.* EXCLUDE(
                    disposition,quality_reason_codes),
                coalesce(e.disposition,b.disposition) disposition,
                CASE WHEN e.disposition IS NOT NULL THEN list_append(
                    b.quality_reason_codes, 'cross_source_'||e.disposition)
                    ELSE b.quality_reason_codes END quality_reason_codes,
                ?::VARCHAR pipeline_version,source_object_id source_version,
                ?::TIMESTAMPTZ processed_at_utc
                FROM base_ledger b LEFT JOIN edits e USING({KEYS})""",
                [PUBLICATION_VERSION, datetime.now(UTC)],
            )
            db.execute("""ALTER TABLE ledger ADD COLUMN quality_status VARCHAR;
                UPDATE ledger SET quality_status=CASE
                    WHEN disposition IN ('invalid','conflicting_id') THEN 'quarantined'
                    WHEN disposition='duplicate_exact' THEN 'duplicate'
                    WHEN len(quality_reason_codes)>0 THEN 'warning' ELSE 'valid' END""")
            db.read_parquet(str(base / "silver.parquet")).create_view("base_silver")
            db.execute(
                f"""CREATE TABLE silver AS SELECT s.* EXCLUDE(
                pipeline_version,processed_at_utc),
                s.pipeline_version base_pipeline_version,
                s.processed_at_utc base_processed_at_utc,
                ?::VARCHAR pipeline_version,?::TIMESTAMPTZ processed_at_utc,
                s.source_object_id source_version FROM base_silver s
                LEFT JOIN edits e USING({KEYS})
                WHERE coalesce(e.disposition,s.disposition)='accepted'""",
                [PUBLICATION_VERSION, datetime.now(UTC)],
            )
            # Full source payloads for new conflicts accompany the original
            # quarantine. Exact duplicates remain auditable through ledger + Bronze.
            db.read_parquet(
                [str(base / "silver.parquet"), str(base / "quarantine.parquet")],
                union_by_name=True,
            ).create_view("stored_rows")
            # A formerly exact duplicate can become a conflict after another
            # source appears. Its payload equals the retained canonical row;
            # recover that payload with its own original ledger source keys.
            db.execute("""CREATE VIEW available_rows AS SELECT * FROM stored_rows
                UNION ALL BY NAME SELECT s.* EXCLUDE(source_file,source_row_number),
                b.source_file,b.source_row_number FROM base_silver s
                JOIN base_ledger b ON s.ride_id=b.ride_id
                    AND s.record_hash=b.record_hash
                JOIN edits e ON b.source_object_id=e.source_object_id
                    AND b.source_file=e.source_file
                    AND b.source_row_number=e.source_row_number
                WHERE b.disposition='duplicate_exact' AND e.disposition='conflicting_id'
            """)
            db.execute(
                f"""CREATE TABLE quarantine AS SELECT a.* EXCLUDE(
                    disposition,quality_reason_codes,quality_status,pipeline_version,
                    processed_at_utc,schema_version),
                l.disposition,l.quality_reason_codes,l.quality_status,
                l.pipeline_version,l.processed_at_utc,l.source_version,
                coalesce(a.schema_version,?) schema_version FROM available_rows a
                JOIN ledger l USING({KEYS})
                WHERE l.disposition IN ('invalid','conflicting_id')""",
                [read_json(base / "quality.json")["schema_version"]],
            )
            observed = dict(
                db.execute(
                    "SELECT disposition,count(*) FROM ledger GROUP BY 1"
                ).fetchall()
            )
            if sum(observed.values()) != state["input_rows"]:
                raise ValueError("Historical ledger lost input rows")
            if db.execute("SELECT count(*) FROM silver").fetchone() != (
                observed.get("accepted", 0),
            ):
                raise ValueError("Historical Silver count differs from ledger")
            if db.execute("SELECT count(*) FROM quarantine").fetchone() != (
                observed.get("invalid", 0) + observed.get("conflicting_id", 0),
            ):
                raise ValueError("Historical quarantine count differs from ledger")
            duplicate_rides = db.execute(
                "SELECT count(*)-count(DISTINCT ride_id) FROM silver"
            ).fetchone()
            if duplicate_rides != (0,):
                raise ValueError("Historical Silver contains duplicate ride IDs")
            dependencies = [
                r[0]
                for r in db.execute("""SELECT DISTINCT strftime(event_time,'%Y-%m')
                FROM (
                SELECT started_at_local AS event_time FROM silver
                UNION ALL SELECT ended_at_local FROM silver)
                ORDER BY 1""").fetchall()
            ]
            metric = db.execute(
                (
                    "SELECT count(*) FROM silver WHERE started_at_utc>=? AND"
                    " started_at_utc<?"
                ),
                [
                    read_json(base / "quality.json")["period_start"],
                    read_json(base / "quality.json")["period_end"],
                ],
            ).fetchone()
            assert metric is not None
            for name in ["silver", "ledger", "quarantine"]:
                db.execute(
                    f"COPY {name} TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
                    [str(stage / f"{name}.parquet")],
                )
        report = {
            "period": state["period"],
            "source_object_id": source_id,
            "source_version": source_id,
            "pipeline_version": PUBLICATION_VERSION,
            "base_pipeline_version": HISTORY_VERSION,
            "schema_versions": sorted(
                {v["schema_version"] for v in state["schemas"].values()}
            ),
            "input_rows": state["input_rows"],
            "dispositions": observed,
            "completed_published_rides_in_source_month": metric[0],
            "dependency_months": dependencies,
            "base_result": str(base),
            "checksums": {p.name: checksum(p) for p in stage.glob("*.parquet")},
        }
        write_json(stage / "quality.json", report)
        stage.rename(destination)
    return {
        "partition_id": partition_id,
        "path": str(destination),
        "reused": False,
        **report,
    }


def activate_snapshot(root: Path, report: dict[str, Any]) -> None:
    write_json(root / "f2" / "active.json", report)


def publish_history(
    states_paths: list[Path],
    root: Path,
    window: list[str],
    *,
    required_months: int = 24,
    activate: bool = True,
) -> Path:
    before = time.monotonic()
    states = [read_json(p) for p in states_paths]
    if (
        len(window) != required_months
        or len(states) != required_months
        or sorted(s["period"] for s in states) != sorted(window)
        or len(set(window)) != len(window)
    ):
        raise ValueError(
            "Complete selected historical window required before publication"
        )
    if window != [shift_month(max(window), i) for i in range(1 - required_months, 1)]:
        raise ValueError("Complete contiguous historical window required")
    if any(
        s["status"] != "verified" or s["pipeline_version"] != HISTORY_VERSION
        for s in states
    ):
        raise ValueError("Failed or mixed pipeline versions cannot be published")
    for state in states:
        verify_artifacts(Path(state["result"]))
        source_zip = root / "bronze/NYC" / state["source_object_id"] / "source.zip"
        if checksum(source_zip) != state["source_object_id"]:
            raise ValueError("Historical Bronze checksum changed")
        for path, expected in state["checksums"].items():
            if checksum(Path(path)) != expected:
                raise ValueError("Historical verification artifact changed")
    snapshot_id = identity(
        [
            PUBLICATION_VERSION,
            sorted((s["period"], s["source_object_id"]) for s in states),
        ]
    )
    destination = root / "f2" / "snapshots" / snapshot_id
    if destination.exists():
        report = read_json(destination / "manifest.json")
        if (
            checksum(destination / "cross_source_changes.parquet")
            != report["changes_checksum"]
        ):
            raise ValueError("Historical cross-source lineage changed")
        for partition in report["partitions"]:
            for name, expected in partition["checksums"].items():
                if checksum(Path(partition["path"]) / name) != expected:
                    raise ValueError("Active historical artifact changed")
        if not activate:
            return destination
        pointer = root / "f2/active.json"
        already_active = (
            pointer.exists() and read_json(pointer)["snapshot_id"] == snapshot_id
        )
        activate_snapshot(root, {"snapshot_id": snapshot_id, "path": str(destination)})
        write_json(
            root / "f2" / "runs" / f"{time.time_ns()}.json",
            {
                "mode": "unchanged" if already_active else "activate_existing",
                "snapshot_id": snapshot_id,
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
                "rebuilt_partitions": 0,
            },
        )
        return destination
    if not activate:
        raise ValueError("Requested historical snapshot is not published")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        stage = Path(directory) / "snapshot"
        stage.mkdir()
        cross = global_changes(states, stage)
        partitions = [
            effective_partition(s, stage / "cross_source_changes.parquet", root)
            for s in states
        ]
        accepted = sum(p["dispositions"].get("accepted", 0) for p in partitions)
        if accepted <= 0:
            raise ValueError("No canonical historical rides; publication blocked")
        previous = (
            read_json(root / "f2" / "active.json")
            if (root / "f2" / "active.json").exists()
            else None
        )
        current_ids = {p["partition_id"] for p in partitions}
        previous_partitions = (
            read_json(Path(previous["path"]) / "manifest.json")["partitions"]
            if previous
            else []
        )
        affected_months = {
            m for p in partitions if not p["reused"] for m in p["dependency_months"]
        } | {
            m
            for p in previous_partitions
            if p["partition_id"] not in current_ids
            for m in p["dependency_months"]
        }
        measurements = {
            "elapsed_seconds": round(time.monotonic() - before, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "rebuilt_partitions": sum(not p["reused"] for p in partitions),
            "output_bytes": sum(
                f.stat().st_size
                for p in partitions
                for f in Path(p["path"]).glob("*.parquet")
            ),
        }
        report = {
            "snapshot_id": snapshot_id,
            "pipeline_version": PUBLICATION_VERSION,
            "base_pipeline_version": HISTORY_VERSION,
            "window": window,
            "previous_snapshot": previous,
            "partitions": partitions,
            "input_rows": sum(p["input_rows"] for p in partitions),
            "canonical_rows": accepted,
            "quality_status": "warning"
            if any(
                read_json(Path(s["result"]) / "quality.json")["quality_status"]
                != "pass"
                for s in states
            )
            or cross["changed_row_counts"]
            else "pass",
            "data_mode": "real_official"
            if all(
                read_json(Path(s["result"]) / "quality.json")["source"][
                    "source_url"
                ].startswith("https://s3.amazonaws.com/tripdata/")
                for s in states
            )
            else "controlled_fixture",
            "cross_source": cross,
            "changes_checksum": checksum(stage / "cross_source_changes.parquet"),
            "affected_dependency_months": sorted(affected_months),
            "measurements": measurements,
            "published_at": datetime.now(UTC).isoformat(),
            "serving": (
                "F1 PostgreSQL publication retained; historical Silver "
                "is published through this atomic manifest, without F3 "
                "marts."
            ),
        }
        write_json(stage / "manifest.json", report)
        stage.rename(destination)
    activate_snapshot(root, {"snapshot_id": snapshot_id, "path": str(destination)})
    write_json(
        root / "f2" / "runs" / f"{time.time_ns()}.json",
        {
            "mode": "first" if previous is None else "incremental",
            "snapshot_id": snapshot_id,
            **measurements,
        },
    )
    return destination
