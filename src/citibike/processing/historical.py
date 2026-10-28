"""Versioned archive preparation and atomic, globally reconciled historical Silver."""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.audit import audit_archive
from citibike.contracts import version_id
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.processing.schemas import detect
from citibike.processing.trips import process, verify_artifacts
from citibike.quality.profiling import profile
from citibike.storage import identity, read_json, write_json

HISTORY_VERSION = "f2.0.0"
AUDIT_VERSION = "f2-independent-1"


def ledger_digest(db: duckdb.DuckDBPyConnection, path: Path) -> str:
    digest = hashlib.sha256()
    cursor = db.execute(
        (
            "SELECT source_file,source_row_number,disposition FROM "
            "read_parquet(?) ORDER BY 1,2"
        ),
        [str(path)],
    )
    while rows := cursor.fetchmany(10000):
        for row in rows:
            digest.update(json.dumps(list(row), separators=(",", ":")).encode() + b"\n")
    return digest.hexdigest()


def prepare(source: Path, root: Path, *, base_only: bool = False) -> Path:
    before = time.monotonic()
    manifest = read_json(source / "manifest.json")
    source_id: str = manifest["checksum"]
    base_was_cached = (
        root / "f2/processed" / version_id(source_id, HISTORY_VERSION)
    ).exists()
    state = root / "f2" / "states" / f"{source_id}-{HISTORY_VERSION}.json"
    if state.exists():
        previous = read_json(state)
        if previous["status"] == "verified":
            verify_artifacts(Path(previous["result"]))
            if checksum(source / "source.zip") != source_id:
                raise ValueError("Bronze checksum changed")
            for path, expected in previous["checksums"].items():
                if checksum(Path(path)) != expected:
                    raise ValueError("Historical preparation artifact changed")
            return state
    try:
        schemas = {f["path"]: detect(f["columns"]) for f in manifest["csv_files"]}
        result = process(
            source, root / "f2", pipeline_version=HISTORY_VERSION, memory_limit_mb=2048
        )
        quality = verify_artifacts(result)
        print(
            f"Processed {manifest['period']}: {quality['input_rows']} rows", flush=True
        )
        auxiliary = root / "f2" / "auxiliary" / source_id / HISTORY_VERSION
        auxiliary.mkdir(parents=True, exist_ok=True)
        profile_path = auxiliary / "profile.json"
        if not profile_path.exists():
            profile(result, profile_path)
        identity_dir = auxiliary / "identity"
        if not identity_dir.exists():
            with duckdb.connect() as db:
                db.execute("SET memory_limit='512MB'; SET threads=2")
                db.execute(
                    """COPY (SELECT *,$period::VARCHAR period,
                    substr(sha256(ride_id),1,1) bucket FROM read_parquet($input)
                    WHERE ride_id IS NOT NULL) TO $output
                    (FORMAT PARQUET,PARTITION_BY(bucket),COMPRESSION ZSTD)""",
                    # Named parameters avoid COPY destination binding order.
                    {
                        "period": manifest["period"],
                        "input": str(result / "row_ledger.parquet"),
                        "output": str(identity_dir),
                    },
                )
        base_receipt = auxiliary / "base_ready.json"
        if not base_receipt.exists():
            write_json(
                base_receipt,
                {
                    "status": "base_ready",
                    "source_object_id": source_id,
                    "pipeline_version": HISTORY_VERSION,
                    "elapsed_seconds": round(time.monotonic() - before, 3),
                    "peak_rss_bytes": peak_rss_bytes(),
                    "base_cached_on_entry": base_was_cached,
                    "at": datetime.now(UTC).isoformat(),
                },
            )
        if base_only:
            return base_receipt
        audit_dir = root / "f2" / "audits" / source_id / AUDIT_VERSION
        audit_file = audit_dir / "baseline.json"
        if not audit_file.exists():
            with tempfile.TemporaryDirectory(dir=auxiliary) as temporary:
                staged_audit = Path(temporary) / "audit"
                baseline = audit_archive(
                    source, root / "f2" / "work", export_dir=staged_audit
                )
                write_json(staged_audit / "baseline.json", baseline)
                audit_dir.parent.mkdir(parents=True, exist_ok=True)
                staged_audit.rename(audit_dir)
        baseline = read_json(audit_file)
        with duckdb.connect() as db:
            db.execute("SET memory_limit='256MB'; SET threads=2")
            row_digest = ledger_digest(db, result / "row_ledger.parquet")
        if (
            baseline["source_checksum"] != source_id
            or baseline["input_rows"] != quality["input_rows"]
            or baseline["dispositions"] != quality["dispositions"]
            or baseline["completed_published_rides"]
            != quality["metrics"]["completed_published_rides"]
            or baseline["row_decision_sha256"] != row_digest
        ):
            raise ValueError("Independent row-level archive reconciliation failed")
        if [
            (f["file"], f["input_rows"], f["dispositions"]) for f in baseline["files"]
        ] != [
            (f["file"], f["input_rows"], f["dispositions"]) for f in quality["files"]
        ]:
            raise ValueError("Independent per-CSV reconciliation failed")
        files = [
            profile_path,
            audit_file,
            *identity_dir.rglob("*.parquet"),
            *(audit_dir / "identity").rglob("*.parquet"),
        ]
        report = {
            "status": "verified",
            "period": manifest["period"],
            "source_object_id": source_id,
            "source_version": source_id,
            "pipeline_version": HISTORY_VERSION,
            "audit_version": AUDIT_VERSION,
            "schemas": schemas,
            "result": str(result),
            "profile": str(profile_path),
            "identity": str(identity_dir),
            "audit": str(audit_dir),
            "row_decision_sha256": row_digest,
            "input_rows": quality["input_rows"],
            "dispositions": quality["dispositions"],
            "checksums": {str(p): checksum(p) for p in files},
            "measurements": {
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
                "base_phase": read_json(base_receipt),
                "base_cached_on_entry": base_was_cached,
                "base_transform_elapsed_seconds": quality["elapsed_seconds"],
                "measurement_scope": (
                    "This worker attempt; cached base transformation is not remeasured."
                ),
                "source_zip_bytes": manifest["bytes"],
                "output_parquet_bytes": sum(
                    p.stat().st_size for p in result.glob("*.parquet")
                ),
                "free_disk_bytes_after": shutil.disk_usage(root).free,
            },
            "verified_at": datetime.now(UTC).isoformat(),
        }
        write_json(state, report)
        print(
            f"Independently verified {manifest['period']}: "
            f"{quality['input_rows']} rows",
            flush=True,
        )
        return state
    except Exception as exc:
        failure = {
            "status": "failed",
            "source_object_id": source_id,
            "period": manifest["period"],
            "pipeline_version": HISTORY_VERSION,
            "error_type": type(exc).__name__,
            "error": str(exc),
            "at": datetime.now(UTC).isoformat(),
        }
        write_json(root / "f2" / "failures" / f"{identity(failure)}.json", failure)
        raise


def source_for_period(
    inventory: dict[str, Any], root: Path, period: str, wait_seconds: int
) -> Path:
    objects = inventory["objects"]
    item = next(o for o in objects if o["scope"] == "NYC" and o["period"] == period)
    receipt = root / "inventory" / "objects" / f"{identity(item)}.json"
    deadline = time.monotonic() + wait_seconds
    while not receipt.exists() and time.monotonic() < deadline:
        time.sleep(5)
    if not receipt.exists():
        raise ValueError(f"Source {period} not ingested; previous publication retained")
    downloaded = read_json(receipt)
    if downloaded["availability"] != "verified":
        raise ValueError(f"Source {period} failed ingestion")
    return Path(downloaded["bronze"])


def prepare_inventory(inventory: Path, root: Path, wait_seconds: int = 0) -> list[Path]:
    report = read_json(inventory)
    states: list[Path] = []
    pending = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        for period in report["window"]:
            source = source_for_period(report, root, period, wait_seconds)
            source_id = read_json(source / "manifest.json")["checksum"]
            state = root / "f2/states" / f"{source_id}-{HISTORY_VERSION}.json"
            command = [
                sys.executable,
                "-m",
                "citibike.processing.historical",
                "--prepare",
                str(source),
                "--data-dir",
                str(root),
            ]
            # One heavy transform/profile at a time; at most two lower-memory
            # independent audits overlap it. Each phase has a separate OS peak.
            if not state.exists() or read_json(state)["status"] != "verified":
                subprocess.run([*command, "--base-only"], check=True)
            pending.append(executor.submit(subprocess.run, command, check=True))
            states.append(state)
            if len(pending) >= 2:
                pending.pop(0).result()
        for future in pending:
            future.result()
    return states


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--base-only", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        print(prepare(args.prepare, args.data_dir, base_only=args.base_only))
    elif args.inventory:
        states = prepare_inventory(args.inventory, args.data_dir, args.wait_seconds)
        print(f"Verified {len(states)} historical source objects")
    else:
        parser.error("--prepare or --inventory required")


if __name__ == "__main__":
    main()
