"""Incremental source contributions → verified immutable event-month Gold."""

import argparse
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.audit import verify_month
from citibike.analytics.contracts import CORE, GEO_VERSION, MARTS, VERSION
from citibike.analytics.transform import (
    contribution_tables,
    month_tables,
    scalar_metrics,
    write_tables,
)
from citibike.contracts import period_bounds
from citibike.ingestion.archive import checksum
from citibike.processing.history_publish import PUBLICATION_VERSION, publish_history
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json


def configured(path: Path) -> duckdb.DuckDBPyConnection:
    db = duckdb.connect(str(path))
    db.execute("SET TimeZone='UTC'; SET memory_limit='2GB'; SET threads=2")
    db.execute("SET preserve_insertion_order=false; SET enable_progress_bar=false")
    db.execute("SET temp_directory=?", [str(path.parent / "spill")])
    return db


def publish_directory(stage: Path, target: Path) -> None:
    """Bounded retry for transient Windows scanner handles; never overwrite."""
    for attempt in range(6):
        try:
            stage.rename(target)
            return
        except PermissionError:
            if attempt == 5 or target.exists():
                raise
            time.sleep(0.2 * (attempt + 1))


def checked(path: Path) -> dict[str, Any]:
    report = read_json(path / "manifest.json")
    if report["pipeline_version"] != VERSION:
        raise ValueError("Incompatible Gold pipeline")
    for name, expected in report["checksums"].items():
        if checksum(path / name) != expected:
            raise ValueError(f"Gold checksum mismatch: {name}")
    return report


def silver_reference(path: Path) -> dict[str, Any]:
    quality = read_json(path / "quality.json")
    if quality["pipeline_version"] != PUBLICATION_VERSION:
        raise ValueError("Unsupported F2 Silver publication version")
    expected = quality["checksums"]["silver.parquet"]
    if checksum(path / "silver.parquet") != expected:
        raise ValueError("Input Silver checksum mismatch")
    return {
        "partition_id": path.name,
        "path": str(path),
        "checksum": expected,
        "source_version": quality["source_version"],
        "source_period": quality["period"],
        "rows": quality["dispositions"].get("accepted", 0),
    }


def prepare_contribution(source: Path, root: Path) -> Path:
    before = time.monotonic()
    reference = silver_reference(source)
    identifier = identity(
        [VERSION, GEO_VERSION, reference["partition_id"], reference["checksum"]]
    )
    target = root / "f3/contributions" / identifier
    if target.exists():
        checked(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        work = Path(temporary)
        stage = work / "result"
        stage.mkdir()
        with configured(work / "compute.duckdb") as db:
            db.read_parquet(str(source / "silver.parquet")).create_view("silver")
            observed = db.execute(
                "SELECT count(*) FROM silver WHERE disposition='accepted'"
            ).fetchone()
            if observed != (reference["rows"],):
                raise ValueError("Silver accepted count mismatch")
            contribution_tables(db)
            metrics = scalar_metrics(db)
            if (
                metrics["n"] != reference["rows"]
                or metrics["arrival_rows"] != reference["rows"]
            ):
                raise ValueError("Contribution event reconciliation failed")
            rows = write_tables(db, stage, CORE)
            months = [
                r[0]
                for r in db.execute("""SELECT DISTINCT
                strftime(timezone('America/New_York',hour_utc),'%Y-%m')
                FROM trip_hourly ORDER BY 1""").fetchall()
            ]
        report = {
            "id": identifier,
            "pipeline_version": VERSION,
            "geo_mapping_version": GEO_VERSION,
            "silver": reference,
            "months": months,
            "rows": rows,
            "metrics": metrics,
            "checksums": {p.name: checksum(p) for p in stage.glob("*.parquet")},
            "measurements": {
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        write_json(stage / "manifest.json", report)
        publish_directory(stage, target)
    print(
        f"Gold contribution {reference['source_period']}: {reference['rows']} rows",
        flush=True,
    )
    return target


def prepare_month(month: str, contributions: list[Path], root: Path) -> Path:
    before = time.monotonic()
    reports = [(p, read_json(p / "manifest.json")) for p in contributions]
    selected = [(p, checked(p)) for p, r in reports if month in r["months"]]
    identifier = identity(
        [VERSION, GEO_VERSION, month, sorted(r["id"] for _, r in selected)]
    )
    target = root / "f3/months" / identifier
    if target.exists():
        checked(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        work = Path(temporary)
        stage = work / "result"
        stage.mkdir()
        with configured(work / "compute.duckdb") as db:
            for name in CORE:
                db.read_parquet(
                    [str(p / f"{name}.parquet") for p, _ in selected]
                ).create_view(f"input_{name}")
            month_tables(db, month)
            metrics = scalar_metrics(db)
            rows = write_tables(db, stage, list(MARTS))
        built = time.monotonic() - before
        with configured(work / "audit.duckdb") as db:
            db.read_parquet(
                [str(Path(r["silver"]["path"]) / "silver.parquet") for _, r in selected]
            ).create_view("audit_input")
            audit = verify_month(
                db, month, {n: str(stage / f"{n}.parquet") for n in MARTS}
            )
        report = {
            "id": identifier,
            "month": month,
            "pipeline_version": VERSION,
            "geo_mapping_version": GEO_VERSION,
            "contributions": [r["id"] for _, r in selected],
            "silver": [r["silver"] for _, r in selected],
            "rows": rows,
            "metrics": metrics,
            "independent_group_mismatches": audit,
            "checksums": {p.name: checksum(p) for p in stage.glob("*.parquet")},
            "measurements": {
                "build_seconds": round(built, 3),
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
                "scope": "month merge + independent direct Silver group oracle",
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        write_json(stage / "manifest.json", report)
        publish_directory(stage, target)
    print(
        f"Gold event month {month}: all {len(MARTS)} marts independently verified",
        flush=True,
    )
    return target


def validate_silver(snapshot: Path, root: Path) -> dict[str, Any]:
    manifest = read_json(snapshot / "manifest.json")
    if manifest["pipeline_version"] != PUBLICATION_VERSION:
        raise ValueError("Unsupported published F2 pipeline")
    controlled = manifest["data_mode"] == "controlled_fixture"
    count = len(manifest["window"]) if controlled else 24
    states = [
        root
        / "f2/states"
        / f"{p['source_object_id']}-{manifest['base_pipeline_version']}.json"
        for p in manifest["partitions"]
    ]
    verified = publish_history(
        states, root, manifest["window"], required_months=count, activate=False
    )
    if verified.resolve() != snapshot.resolve():
        raise ValueError("F2 snapshot identity mismatch")
    return manifest


def activate(root: Path, report: dict[str, Any]) -> None:
    write_json(root / "f3/active.json", report)


def build(
    root: Path, snapshot: Path | None = None, *, prepare_only: bool = False
) -> Path:
    before = time.monotonic()
    snapshot = snapshot or Path(read_json(root / "f2/active.json")["path"])
    upstream = validate_silver(snapshot, root)
    contributions = []
    for partition in upstream["partitions"]:
        command = [
            sys.executable,
            "-m",
            "citibike.analytics.build",
            "--source",
            partition["path"],
            "--root",
            str(root),
        ]
        subprocess.run(command, check=True)
        reference = silver_reference(Path(partition["path"]))
        identifier = identity(
            [VERSION, GEO_VERSION, reference["partition_id"], reference["checksum"]]
        )
        contributions.append(root / "f3/contributions" / identifier)
    if prepare_only:
        return root / "f3/contributions"
    contribution_reports = [read_json(p / "manifest.json") for p in contributions]
    months = sorted({m for r in contribution_reports for m in r["months"]})
    partitions = []
    for month in months:
        command = [
            sys.executable,
            "-m",
            "citibike.analytics.build",
            "--month",
            month,
            "--root",
            str(root),
            "--contributions",
            *map(str, contributions),
        ]
        subprocess.run(command, check=True)
        selected = [r["id"] for r in contribution_reports if month in r["months"]]
        identifier = identity([VERSION, GEO_VERSION, month, sorted(selected)])
        path = root / "f3/months" / identifier
        partitions.append({"path": str(path), **checked(path)})
    from citibike.analytics.availability import build_availability

    if sum(p["metrics"]["n"] for p in partitions) != upstream["canonical_rows"]:
        raise ValueError("Gold whole-window start reconciliation failed")
    if (
        sum(p["metrics"]["arrival_rows"] for p in partitions)
        != upstream["canonical_rows"]
    ):
        raise ValueError("Gold whole-window arrival reconciliation failed")

    availability = build_availability(root)
    identifier = identity(
        [
            VERSION,
            upstream["snapshot_id"],
            [p["id"] for p in partitions],
            availability["id"],
        ]
    )
    target = root / "f3/publications" / identifier
    if target.exists():
        checked(target)
        activate(root, {"publication_id": identifier, "path": str(target)})
        write_json(
            root / "f3/runs" / f"{time.time_ns()}.json",
            {
                "mode": "unchanged",
                "publication_id": identifier,
                "rebuilt_months": 0,
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
            },
        )
        return target
    start, _ = period_bounds(upstream["window"][0])
    _, end = period_bounds(upstream["window"][-1])
    previous = (
        read_json(root / "f3/active.json")
        if (root / "f3/active.json").exists()
        else None
    )
    old = (
        read_json(Path(previous["path"]) / "manifest.json")["months"]
        if previous
        else []
    )
    old_ids = {p["id"] for p in old}
    affected = sorted(
        {p["month"] for p in partitions if p["id"] not in old_ids}
        | {p["month"] for p in old if p["id"] not in {v["id"] for v in partitions}}
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "id": identifier,
        "publication_id": identifier,
        "pipeline_version": VERSION,
        "geo_mapping_version": GEO_VERSION,
        "source_snapshot_id": upstream["snapshot_id"],
        "data_as_of": upstream["published_at"],
        "data_as_of_kind": (
            "F2 Silver publication timestamp; source interval separately stated"
        ),
        "source_snapshot": str(snapshot),
        "source_manifest_checksum": checksum(snapshot / "manifest.json"),
        "source": "Citi Bike F2 globally canonical published Silver",
        "scope": "NYC",
        "timezone": "America/New_York",
        "data_mode": upstream["data_mode"],
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "accepted_silver_rows": upstream["canonical_rows"],
        "input_rows": upstream["input_rows"],
        "months": partitions,
        "availability": availability,
        "affected_months": affected,
        "previous_publication": previous,
        "quality_status": "warning",
        "checksums": {},
        "created_at": datetime.now(UTC).isoformat(),
        "measurements": {
            "elapsed_seconds": round(time.monotonic() - before, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "rebuilt_months": len(affected),
            "throughput_input_rows_per_sec": upstream["canonical_rows"]
            / (time.monotonic() - before),
            "source_contribution_seconds": sum(
                r["measurements"]["elapsed_seconds"] for r in contribution_reports
            ),
            "event_month_build_audit_seconds": sum(
                p["measurements"]["elapsed_seconds"] for p in partitions
            ),
            "worker_peak_rss_bytes": max(
                r["measurements"]["peak_rss_bytes"]
                for r in [*contribution_reports, *partitions]
            ),
            "timing_scope": (
                "invocation wall time includes cache validation; first-build "
                "child costs recorded separately and retained on reuse"
            ),
        },
    }
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary) / "publication"
        stage.mkdir()
        write_json(stage / "manifest.json", report)
        publish_directory(stage, target)
    activate(root, {"publication_id": identifier, "path": str(target)})
    write_json(
        root / "f3/runs" / f"{time.time_ns()}.json",
        {
            "mode": "first" if previous is None else "incremental",
            **report["measurements"],
            "publication_id": identifier,
        },
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--month")
    parser.add_argument("--contributions", nargs="+", type=Path)
    parser.add_argument("--root", type=Path, default=Path("data"))
    args = parser.parse_args()
    if args.source:
        print(prepare_contribution(args.source, args.root))
    elif args.month and args.contributions:
        print(prepare_month(args.month, args.contributions, args.root))
    else:
        parser.error("--source or --month/--contributions required")


if __name__ == "__main__":
    main()
