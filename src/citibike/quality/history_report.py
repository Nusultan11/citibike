"""Compact, source-backed EDA evidence; full monthly profiles stay outside Git."""

import os
import platform
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.ingestion.archive import checksum
from citibike.ingestion.gbfs import GBFS_PIPELINE
from citibike.processing.resources import peak_rss_bytes
from citibike.quality.profiling import research
from citibike.storage import read_json, write_json


def station_join_counts(db: duckdb.DuckDBPyConnection) -> tuple[int, int, int]:
    # LEFT JOIN retains every input row. A larger output count proves expansion;
    # no high-cardinality GROUP BY on all historical row keys is necessary.
    row = db.execute("""SELECT (SELECT count(*) FROM trips),count(*),count(h.station_id)
        FROM trips t LEFT JOIN station_history h ON t.start_station_id=h.trip_station_id
        AND t.started_at_utc>=h.valid_from AND t.started_at_utc<h.valid_to
    """).fetchone()
    assert row is not None
    if row[0] != row[1]:
        raise ValueError("Station history join explosion")
    return int(row[0]), int(row[2]), 0


def gbfs_evidence(root: Path) -> dict[str, Any]:
    collections = []
    for path in sorted(
        (root / "gbfs" / "silver" / GBFS_PIPELINE).glob("*/quality.json")
    ):
        quality = read_json(path)
        if (
            quality["collection_id"] != path.parent.name
            or quality["pipeline_version"] != GBFS_PIPELINE
        ):
            raise ValueError("GBFS collection lineage mismatch")
        if checksum(path.parent / "station_status.parquet") != quality["checksum"]:
            raise ValueError("GBFS Silver checksum mismatch")
        bronze = root / "gbfs/bronze" / quality["collection_id"]
        for name, metadata in quality["manifest"]["feeds"].items():
            if checksum(bronze / f"{name}.json") != metadata["checksum"]:
                raise ValueError("GBFS Bronze checksum mismatch")
        collections.append(
            {
                k: quality[k]
                for k in [
                    "collection_id",
                    "pipeline_version",
                    "input_station_ids",
                    "accepted",
                    "invalid",
                    "quality_counts",
                    "reason_counts",
                    "manifest",
                    "checksum",
                ]
            }
        )
        with duckdb.connect() as db:
            age = db.execute(
                """SELECT count(*) FILTER (WHERE last_reported IS NULL),
                quantile_cont(epoch(received_at_utc-last_reported),[0,0.5,0.95,1]),
                count(*),count(*)-count(DISTINCT (collection_id,station_id)),
                count(*) FILTER (WHERE collection_id<>$expected)
                FROM read_parquet($input)""",
                {
                    "expected": quality["collection_id"],
                    "input": str(path.parent / "station_status.parquet"),
                },
            ).fetchone()
            assert age is not None
            if age[2] != quality["accepted"] or age[3] or age[4]:
                raise ValueError("GBFS Silver count/grain mismatch")
            collections[-1]["station_reported_freshness"] = {
                "unknown_last_reported_rows": age[0],
                "age_seconds_min_median_p95_max": age[1],
                "note": "Raw source timestamps retained; large ages stay stale.",
            }
    gaps = []
    for previous, current in zip(collections, collections[1:], strict=False):
        a = previous["manifest"]["feeds"]["station_status"]
        b = current["manifest"]["feeds"]["station_status"]
        elapsed = (
            datetime.fromisoformat(b["received_at_utc"])
            - datetime.fromisoformat(a["received_at_utc"])
        ).total_seconds()
        gaps.append(
            {
                "from": previous["collection_id"],
                "to": current["collection_id"],
                "elapsed_seconds": elapsed,
                "previous_ttl": a["ttl"],
                "unobserved_after_ttl_seconds": max(0, elapsed - a["ttl"]),
                "source_timestamp_advanced": b["last_updated"] > a["last_updated"],
            }
        )
    history_path = root / "gbfs/station_history/active.json"
    history = read_json(history_path) if history_path.exists() else None
    if history:
        history_file = Path(history["path"]) / "dim_station_history.parquet"
        expected = read_json(history_file.parent / "manifest.json")["checksum"]
        if checksum(history_file) != expected:
            raise ValueError("Station history checksum mismatch")
        with duckdb.connect() as db:
            db.read_parquet(str(history_file)).create_view("history")
            grain = db.execute("""SELECT count(*),count(*)-count(DISTINCT
                (station_id,valid_from)),count(*) FILTER (WHERE valid_to<=valid_from)
                FROM history""").fetchone()
            assert grain is not None
            overlaps = {}
            for key in ["station_id", "trip_station_id"]:
                value = db.execute(f"""SELECT count(*) FROM (
                    SELECT valid_from,max(valid_to) OVER (PARTITION BY {key}
                    ORDER BY valid_from ROWS BETWEEN UNBOUNDED PRECEDING AND
                    1 PRECEDING) previous_end FROM history WHERE {key} IS NOT NULL)
                    WHERE valid_from<previous_end""").fetchone()
                assert value is not None
                overlaps[key] = value[0]
        if (
            grain[0] != history["intervals"]
            or grain[1]
            or grain[2]
            or any(overlaps.values())
        ):
            raise ValueError("Station history integrity failed")
        history = {
            **history,
            "checksum": checksum(history_file),
            "tested_intervals": grain[0],
            "duplicate_grain": grain[1],
            "invalid_intervals": grain[2],
            "overlaps": overlaps,
        }
    return {
        "collections": collections,
        "gaps": gaps,
        "station_history": history,
        "coverage_note": (
            "Only observed collections; TTL gaps and stale station "
            "states are unknown, never interpolated into "
            "availability or historical trips."
        ),
    }


def evidence(
    root: Path, inventory: Path, snapshot: Path, output: Path
) -> dict[str, Any]:
    before = time.monotonic()
    inventory_report = read_json(inventory)
    manifest = read_json(snapshot / "manifest.json")
    if inventory_report["window"] != manifest["window"]:
        raise ValueError("Inventory/snapshot window mismatch")
    base_version = manifest.get("base_pipeline_version", manifest["pipeline_version"])
    periods = manifest["window"]
    monthly = []
    profiles = []
    source_receipts = [
        read_json(p) for p in (root / "inventory/objects").glob("*.json")
    ]
    by_checksum = {
        r.get("checksum"): r for r in source_receipts if r["availability"] == "verified"
    }
    for partition in manifest["partitions"]:
        sid = partition["source_object_id"]
        state = read_json(root / "f2/states" / f"{sid}-{base_version}.json")
        profile = read_json(Path(state["profile"]))
        profiles.append(profile)
        source = by_checksum[sid]
        monthly.append(
            {
                "period": partition["period"],
                "source_url": source["source_url"],
                "source_version": sid,
                "bytes": source["bytes"],
                "etag": source["etag"],
                "source_modified_at": source["source_modified_at"],
                "retrieved_at": source["retrieved_at"],
                "csv_files": source["csv_files"],
                "csv_quality": profile["files"],
                "zip_entries": source["zip_entries"],
                "schemas": state["schemas"],
                "input_rows": partition["input_rows"],
                "dispositions": partition["dispositions"],
                "dependency_months": partition["dependency_months"],
                "partition_id": partition["partition_id"],
                "checksums": partition["checksums"],
                "independent_row_decision_sha256": state["row_decision_sha256"],
                "base_dispositions": profile["dispositions"],
                "profile_checksum": checksum(Path(state["profile"])),
                "measurements": state["measurements"],
                "raw_missing_fields": profile["raw_missing_fields"],
                "raw_categories": profile["raw_categories"],
                "canonical_missing_fields": profile["canonical_missing_fields"],
                "reason_counts": profile["reason_counts"],
                "overview": profile["overview"],
                "expected_hours": profile["expected_hours"],
                "covered_hours": profile["covered_hours"],
                "missing_hour_buckets": profile["missing_hour_buckets"],
                "source_last_modified_minus_month_end_seconds": profile[
                    "source_last_modified_minus_month_end_seconds"
                ],
            }
        )
    drift = []
    for previous, current in zip(profiles, profiles[1:], strict=False):
        comparisons = {}
        for field in ["rideable_type", "member_casual"]:
            a, b = previous["raw_categories"][field], current["raw_categories"][field]
            comparisons[field] = {
                "new_categories": sorted(set(b) - set(a)),
                "removed_categories": sorted(set(a) - set(b)),
                "l1_share_change": sum(
                    abs(
                        a.get(k, 0) / previous["input_rows"]
                        - b.get(k, 0) / current["input_rows"]
                    )
                    for k in a.keys() | b.keys()
                ),
            }
        drift.append(
            {
                "from": previous["period"],
                "to": current["period"],
                "schema_changed": previous["schema_version"]
                != current["schema_version"],
                "row_ratio": current["input_rows"] / previous["input_rows"],
                "raw_missing_rate_changes": {
                    k: current["raw_missing_fields"][k] / current["input_rows"]
                    - previous["raw_missing_fields"][k] / previous["input_rows"]
                    for k in previous["raw_missing_fields"]
                },
                "categories": comparisons,
            }
        )
    counts: Counter[str] = Counter()
    for month in monthly:
        counts.update(month["dispositions"])
    raw_missing: Counter[str] = Counter()
    weekly: Counter[tuple[int, int]] = Counter()
    reasons: Counter[str] = Counter()
    for profile in profiles:
        raw_missing.update(profile["raw_missing_fields"])
        reasons.update(profile["reason_counts"])
        for row in profile["hour_of_week"]:
            weekly[(int(row["dow"]), int(row["local_hour"]))] += row["n"]
    base_rows = sum(p["overview"]["canonical_rows"] for p in profiles)
    findings = {
        "profile_grain": "Canonical per-source base before cross-source exclusions",
        "highest_input_month": max(monthly, key=lambda m: m["input_rows"])["period"],
        "lowest_input_month": min(monthly, key=lambda m: m["input_rows"])["period"],
        "raw_missing_counts": dict(raw_missing),
        "raw_missing_rates": {
            k: v / manifest["input_rows"] for k, v in raw_missing.items()
        },
        "route_coverage_rows": sum(
            p["overview"]["route_coverage_rows"] for p in profiles
        ),
        "route_coverage_denominator": base_rows,
        "duration_min_seconds": min(p["overview"]["duration_min"] for p in profiles),
        "duration_max_seconds": max(p["overview"]["duration_max"] for p in profiles),
        "reason_counts_before_cross_source": dict(reasons),
        "missing_hour_buckets": sum(p["missing_hour_buckets"] for p in profiles),
        "hour_of_week": [
            {"dow": k[0], "local_hour": k[1], "n": v} for k, v in sorted(weekly.items())
        ],
        "peak_hour_of_week": list(weekly.most_common(1)[0][0]) if weekly else None,
    }
    gbfs = gbfs_evidence(root)
    join_coverage: dict[str, Any] = {
        "station_history_available": gbfs["station_history"] is not None,
        "matched_start_rows": 0,
        "multiple_matches": 0,
        "tested_rows": 0,
        "rule": (
            "trip station alias plus event timestamp within "
            "[valid_from,valid_to); no present-day backfill"
        ),
    }
    if gbfs["station_history"]:
        history_file = (
            Path(gbfs["station_history"]["path"]) / "dim_station_history.parquet"
        )
        spill = root / "f2/work/station-join-spill"
        spill.mkdir(parents=True, exist_ok=True)
        with duckdb.connect() as db:
            db.execute("SET memory_limit='1GB'; SET threads=2; SET TimeZone='UTC'")
            db.execute("SET temp_directory=?", [str(spill)])
            db.read_parquet(str(history_file)).create_view("station_history")
            for partition in manifest["partitions"]:
                db.read_parquet(
                    str(Path(partition["path"]) / "silver.parquet")
                ).create_view("trips", replace=True)
                row = station_join_counts(db)
                join_coverage["tested_rows"] += row[0]
                join_coverage["matched_start_rows"] += row[1]
                join_coverage["multiple_matches"] += row[2]
        if join_coverage["multiple_matches"]:
            raise ValueError("Station history join explosion")
    real = all(
        m["source_url"].startswith("https://s3.amazonaws.com/tripdata/")
        for m in monthly
    )
    ingest_attempts = []
    for path in sorted((root / "inventory/receipts").glob("*.json")):
        attempt = read_json(path)
        ingest_attempts.append(
            {
                "receipt_id": path.stem,
                "checked_at": attempt["checked_at"],
                "complete": attempt["complete"],
                "availability_counts": dict(
                    Counter(obj["availability"] for obj in attempt["objects"])
                ),
                "recorded_object_seconds": sum(
                    obj.get("elapsed_seconds", 0) for obj in attempt["objects"]
                ),
            }
        )
    disk_bytes = {
        area: sum(p.stat().st_size for p in (root / area).rglob("*") if p.is_file())
        for area in [
            "bronze",
            "f2/processed",
            "f2/auxiliary",
            "f2/audits",
            "f2/silver",
            "f2/snapshots",
            "gbfs",
            "f2/work",
            "downloads",
        ]
    }
    failed_preparations = []
    for path in sorted((root / "f2/failures").glob("*.json")):
        failure = read_json(path)
        failed_preparations.append(
            {
                key: failure.get(key)
                for key in [
                    "period",
                    "source_object_id",
                    "pipeline_version",
                    "error_type",
                    "at",
                ]
            }
        )
    report = {
        "stage": "F2",
        "status": "verified" if real else "verified_fixture",
        "data_mode": "real_official" if real else "controlled_fixture",
        "snapshot_id": manifest["snapshot_id"],
        "pipeline_version": manifest["pipeline_version"],
        "window": periods,
        "scope_selection": (
            "Latest 24 contiguous fully published NYC months from "
            "complete official listing; JC excluded by geography."
        ),
        "inventory_checked_at": inventory_report["checked_at"],
        "inventory_source": inventory_report["source_url"],
        "inventory_object_count": len(inventory_report["objects"]),
        "inventory_objects": [
            {
                k: o[k]
                for k in [
                    "key",
                    "period",
                    "year",
                    "scope",
                    "bytes",
                    "etag",
                    "last_modified",
                    "availability",
                    "packaging",
                ]
            }
            for o in inventory_report["objects"]
        ],
        "input_rows": manifest["input_rows"],
        "canonical_rows": manifest["canonical_rows"],
        "dispositions": dict(counts),
        "monthly": monthly,
        "drift": drift,
        "findings": findings,
        "cross_source": manifest["cross_source"],
        "station_history_join": join_coverage,
        "gbfs": gbfs,
        "measurements": manifest["measurements"],
        "execution_context": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "logical_cpus": os.cpu_count(),
            "local_bytes_by_area": disk_bytes,
            "free_disk_bytes_at_report": shutil.disk_usage(root).free,
            "ingest_attempts": ingest_attempts,
            "failed_preparation_attempts": failed_preparations,
            "base_phase_measurement_missing_periods": [
                m["period"] for m in monthly if "base_phase" not in m["measurements"]
            ],
            "measurement_caveat": (
                "First execution included failed attempts and cached recovery; "
                "there is no clean cold end-to-end benchmark. Worker attempts, "
                "base phases and publication are measured separately. Object "
                "seconds include cached verifications and repeat receipts; "
                "do not sum receipts as download wall time. OS peak RSS is "
                "per process, not aggregate concurrent RAM or DuckDB limit. "
                "Disk totals include retained attempts, not minimal disk needs."
            ),
        },
        "runs": [read_json(p) for p in sorted((root / "f2/runs").glob("*.json"))],
        "weather": {"status": "unavailable", "rows": 0, "join_performed": False},
        "train_serve_availability": (
            "F2 has no weather/forecast or historical GBFS before collection. "
            "F4 uses a separate retrospective delayed-source experiment. "
            "Last-Modified/retrieved_at do not prove first-publication availability."
        ),
        "interpretation": (
            "Raw/canonical profiles precede cross-source exclusions;"
            " monthly final dispositions and partitions reflect the "
            "published global decisions. No duration/geography "
            "outlier removal."
        ),
        "rules_evidence_impact_tests": [
            {
                "evidence": {
                    "raw_missing_counts": dict(raw_missing),
                    "route_coverage_rows": findings["route_coverage_rows"],
                    "denominator": base_rows,
                },
                "impact": "Station/route coverage differs from ride count",
                "rule": (
                    "Keep eligible rides; null stations excluded only from "
                    "relevant event metrics"
                ),
                "test": (
                    "test_all_files_reconciled_and_idempotent / "
                    "test_silver_types_raw_times_nullable_coordinates_and_lineage"
                ),
            },
            {
                "evidence": manifest["cross_source"],
                "impact": "Double counting or ambiguous ride identity",
                "rule": (
                    "Exact payload: earliest eligible month/file/row; "
                    "conflicts quarantine every variant"
                ),
                "test": "test_global_reconciliation_"
                "incremental_dependencies_and_rollback",
            },
            {
                "evidence": {
                    "reason_counts": {
                        key: value
                        for key, value in reasons.items()
                        if any(word in key for word in ["time", "duration", "future"])
                    },
                    "duration_min_seconds": findings["duration_min_seconds"],
                    "duration_max_seconds": findings["duration_max_seconds"],
                    "note": (
                        "Reason flags overlap; they are not additive row exclusions."
                    ),
                },
                "impact": "Wrong UTC hour or invalid event order",
                "rule": (
                    "Reject ambiguous/gap/invalid/negative/future; retain "
                    "flagged real duration peaks"
                ),
                "test": (
                    "test_dst_ambiguity_gap_and_normalization / "
                    "test_profile_preserves_null_and_does_not_drop_duration_peaks"
                ),
            },
            {
                "evidence": {
                    "collections": len(gbfs["collections"]),
                    "gaps": gbfs["gaps"],
                    "station_history_integrity": gbfs["station_history"],
                },
                "impact": "Unknown availability and unsafe historical join",
                "rule": (
                    "Stale/missing stay explicit; station history begins "
                    "only at confirmed receipt and expires by source ttl"
                ),
                "test": (
                    "test_versions_zero_unknown_stale_and_invalid_counts / "
                    "test_real_collection_protocol_history_and_failure_rollback"
                ),
            },
        ],
        "source_policy": "https://citibikenyc.com/data-sharing-policy",
        "privacy": (
            "Aggregate evidence only; raw trips, GBFS payloads and "
            "Parquet remain local and ignored by Git."
        ),
    }
    report["research"] = research(snapshot, root / "f2/work/research-spill")
    report["ml_readiness"] = cohort_readiness(root, snapshot, report["research"])
    report["research_hypotheses"] = hypotheses(report["research"])
    report["reproduction"] = {
        "inventory_path": str(inventory),
        "inventory_sha256": checksum(inventory),
        "snapshot_path": str(snapshot),
        "report_code_sha256": checksum(Path(__file__)),
        "verified_at": datetime.now().astimezone().isoformat(),
        "elapsed_seconds": round(time.monotonic() - before, 3),
        "peak_rss_bytes": peak_rss_bytes(),
        "scope": (
            "Fresh report/SQL checks; original ingest/audit "
            "measurements retain their original dates"
        ),
    }
    write_json(output, report)
    return report


def cohort_readiness(
    root: Path, snapshot: Path, analysis: dict[str, Any]
) -> dict[str, Any]:
    """Read existing F4 labels/cohort; never fit, select or mutate artifacts."""
    pointer = root / "f4/active_datasets.json"
    if not pointer.exists():
        return {"status": "unavailable", "reason": "No F4 dataset in this data root"}
    from citibike.forecasting.artifacts import checked

    path = Path(read_json(pointer)["path"])
    data = checked(path)
    if (
        data["source_snapshot_id"]
        != read_json(snapshot / "manifest.json")["snapshot_id"]
    ):
        raise ValueError("F4 cohort belongs to a different F2 snapshot")
    with duckdb.connect() as db:
        db.execute("SET memory_limit='1GB'; SET threads=2; SET TimeZone='UTC'")
        db.read_parquet(str(path / "dense.parquet")).create_view("dense")
        db.execute("""CREATE VIEW labels AS SELECT station_id,hour_utc,
            'departures' prediction_type,departures actual FROM dense UNION ALL
            SELECT station_id,hour_utc,'arrivals',arrivals FROM dense""")
        measured = db.execute("""SELECT count(DISTINCT station_id),
            count(*) FILTER(WHERE actual=0),count(*) FILTER(WHERE actual IS NULL),
            sum(actual) FILTER(WHERE prediction_type='departures'),
            sum(actual) FILTER(WHERE prediction_type='arrivals'),
            count(*)-count(DISTINCT(station_id,hour_utc,prediction_type))
            FROM labels""").fetchone()
        assert measured is not None
        db.read_parquet(str(path / "examples.parquet")).create_view("examples")
        example_counts = db.execute("""SELECT count(*) FILTER(WHERE actual=0),
            count(*) FILTER(WHERE actual IS NULL) FROM examples""").fetchone()
        if (
            measured[0] != data["coverage"]["stations"]
            or measured[5]
            or example_counts != (data["zero_labels"], data["missing_labels"])
        ):
            raise ValueError("F4 label coverage/grain mismatch")
        per_month = query_rows_local(
            db,
            """SELECT
            strftime(timezone('America/New_York',hour_utc),'%Y-%m') period,
            prediction_type,count(*) candidate_hours,count(actual) eligible_hours,
            count(*) FILTER(WHERE actual=0) zero_hours,
            count(*) FILTER(WHERE actual IS NULL) missing_hours,sum(actual) events
            FROM labels GROUP BY 1,2 ORDER BY 1,2""",
        )
        per_station = query_rows_local(
            db,
            """SELECT station_id,prediction_type,
            count(*) candidate_hours,count(actual) eligible_hours,
            count(*) FILTER(WHERE actual IS NULL) missing_hours,sum(actual) events
            FROM labels GROUP BY 1,2 ORDER BY 1,2""",
        )
        partitions = read_json(snapshot / "manifest.json")["partitions"]
        db.read_parquet(
            [str(Path(p["path"]) / "silver.parquet") for p in partitions]
        ).create_view("trips")
        direct = []
        for kind, prefix, field in [
            ("departures", "start", "started"),
            ("arrivals", "end", "ended"),
        ]:
            direct += query_rows_local(
                db,
                f"""SELECT '{kind}' prediction_type,
                count(*) cohort_events FROM trips WHERE {prefix}_station_id IN
                    (SELECT DISTINCT station_id FROM labels)
                AND {field}_at_utc>='{analysis["window_utc"][0]}'::TIMESTAMPTZ
                AND {field}_at_utc<'{analysis["window_utc"][1]}'::TIMESTAMPTZ""",
            )
        for row in direct:
            if (
                row["cohort_events"]
                != data["coverage"]["cohort_" + row["prediction_type"]]
            ):
                raise ValueError("Independent F4 cohort Silver count mismatch")
    return {
        "status": "verified",
        "dataset_id": data["id"],
        "manifest_sha256": checksum(path / "manifest.json"),
        "checksums": data["checksums"],
        "coverage": data["coverage"],
        "independent_direct_silver": direct,
        "monthly": per_month,
        "stations": per_station,
        "join_duplicate_keys": measured[5],
        "dense_label_zero_hours": measured[1],
        "dense_label_missing_hours": measured[2],
        "examples_zero_rows": example_counts[0] if example_counts else None,
        "examples_missing_rows": example_counts[1] if example_counts else None,
        "availability_audit": data["availability_audit"],
        "availability_mode": data["availability_mode"],
        "limitation": (
            "Cohort64 is bounded, not city representative; "
            "historical publication times unknown; +45d is an "
            "assumption, not verified production readiness. No "
            "capacity/current coordinates/historical GBFS/actual "
            "future weather features."
        ),
    }


def query_rows_local(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    from citibike.quality.profiling import query_rows

    return query_rows(db, sql)


def hypotheses(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Explicit questions and supported descriptive conclusions, not causal tests."""
    result = []
    for name, question, population, calculation, value, limitation in [
        (
            "distribution",
            "Are eligible station-hours sparse and long-tailed?",
            "All observed IDs x UTC hour x target within 24 NY months",
            (
                "monthly/histograms: count zero, 1, 2-4, >=20; "
                "quantile_cont on eligible labels"
            ),
            [
                {
                    "prediction_type": k,
                    "eligible": sum(
                        x["eligible_hours"]
                        for x in r["monthly"]
                        if x["prediction_type"] == k
                    ),
                    "zero": sum(
                        x["zero_hours"]
                        for x in r["monthly"]
                        if x["prediction_type"] == k
                    ),
                    "single": sum(
                        x["single_event_hours"]
                        for x in r["monthly"]
                        if x["prediction_type"] == k
                    ),
                    "missing": sum(
                        x["source_gap_hours"] + x["station_gap_hours"]
                        for x in r["monthly"]
                        if x["prediction_type"] == k
                    ),
                }
                for k in ("departures", "arrivals")
            ],
            (
                "Zeros are published-event labels, not station "
                "operating status or unmet demand; missing excluded "
                "from quantiles"
            ),
        ),
        (
            "churn",
            "Does observed ID composition change?",
            "ID x event month, either target",
            (
                "station_month sets, first/last, adjacent set "
                "difference/Jaccard; activity quantiles"
            ),
            {
                "union_ids": r["station_universe"],
                "stable_ids": r["stable_24_month_ids"],
                "activity": r["heterogeneity"],
            },
            (
                "First/last seen are window-censored; identity "
                "reuse/moves cannot be resolved as installations"
            ),
        ),
        (
            "weekly",
            "Do observed rates differ across local weekday/hour?",
            (
                "Eligible target station-hours, NY ISO weekday 1..7; "
                "repeated DST hours both included"
            ),
            "sum(actual)/count(eligible hours), not unnormalised weekday totals",
            [
                max(
                    (
                        x
                        for x in r["weekly"]
                        if x["prediction_type"] == k and x["mean"] is not None
                    ),
                    key=lambda x: x["mean"],
                    default={},
                )
                for k in ("departures", "arrivals")
            ],
            (
                "Eligibility mix changes; descriptive associations "
                "only, no commuter or weather causal claim"
            ),
        ),
        (
            "annual",
            "Does seasonal rate variation persist in stable IDs?",
            "24 event months; full universe and IDs observed in all months",
            (
                "monthly eligible mean + stable_mean; 12 year-on-year "
                "pairs, no partial months"
            ),
            [
                {
                    "prediction_type": k,
                    "lowest": min(
                        (
                            x
                            for x in r["monthly"]
                            if x["prediction_type"] == k and x["mean"] is not None
                        ),
                        key=lambda x: x["mean"],
                        default={},
                    ),
                    "highest": max(
                        (
                            x
                            for x in r["monthly"]
                            if x["prediction_type"] == k and x["mean"] is not None
                        ),
                        key=lambda x: x["mean"],
                        default={},
                    ),
                }
                for k in ("departures", "arrivals")
            ],
            (
                "Only two annual cycles, no trend/season separation; "
                "stable cohort has hindsight survivorship and is "
                "descriptive only"
            ),
        ),
        (
            "geography",
            "Are endpoint event volumes spatially heterogeneous?",
            "Canonical endpoint x 0.1 degree cell, both targets, event window",
            (
                "floor(endpoint latitude*10), floor(longitude*10); "
                "broad box; unknown/outside explicit"
            ),
            {
                "largest_cells": [
                    next((x for x in r["geography"] if x["prediction_type"] == k), {})
                    for k in ("departures", "arrivals")
                ],
                "coordinate_churn": r["coordinate_churn"],
            },
            (
                "Unequal cell area/station exposure, not borough rates; "
                "historical trip coordinates only, never current "
                "GBFS/capacity; no causal spatial effect"
            ),
        ),
        (
            "outliers",
            "Are extreme station-hours retained?",
            "Positive observed ID x UTC hour x target",
            "Tukey q3+1.5*IQR on positive hours; deterministic top20; daily extremes",
            r["outliers"],
            (
                "Heavy-tailed counts are not automatically erroneous; "
                "no clipping/exclusion; anomaly causes unverified"
            ),
        ),
        (
            "shift",
            "Do count distributions shift across months and matched seasons?",
            (
                "Eligible station-hour histograms, full observed "
                "universe; adjacent and year-on-year"
            ),
            (
                "TV=0.5*sum(abs(bin_share_current-bin_share_previous)), "
                "fixed 0/1/2-4/5-9/10-19/20+ bins"
            ),
            sorted(
                (x for x in r["shifts"] if x["total_variation"] is not None),
                key=lambda x: -x["total_variation"],
            )[:8],
            (
                "Mix, season and source/eligibility gaps confounded; "
                "descriptive effect size, no significance/causality or "
                "model selection"
            ),
        ),
    ]:
        result.append(
            {
                "id": name,
                "hypothesis": question,
                "population_grain": population,
                "calculation": calculation,
                "result": value,
                "limitations": limitation,
                "verification": (
                    "Independent raw-Silver event-month totals, Python "
                    "label oracle; frozen fixture expected values and "
                    "grain/coverage tests. Geography/churn/TV additionally "
                    "checked by independent calculation in corrective run."
                ),
            }
        )
    return result


def analytical_markdown(report: dict[str, Any], target: Path) -> None:
    monthly = report["monthly"]
    rows = [
        "# F2 — аналитический разбор "
        + (
            "реальной истории"
            if report["data_mode"] == "real_official"
            else "контролируемых fixtures"
        ),
        "",
        f"Охват NYC: {report['window'][0]} — {report['window'][-1]}; "
        f"{len(monthly)} полных месяцев.",
        f"Вход: {report['input_rows']:,}; "
        f"канонических поездок: {report['canonical_rows']:,}.",
        (
            "Детальные машинные профили — локальные артефакты "
            "pipeline; компактное evidence содержит hashes, версии, "
            "исключения, drift и GBFS timestamps."
        ),
        "",
        "| Архив NYC | CSV | Вход | Канонические | Исключения | Пропущенные часы |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for month in monthly:
        accepted = month["dispositions"].get("accepted", 0)
        values = [
            month["period"],
            len(month["csv_files"]),
            month["input_rows"],
            accepted,
            month["input_rows"] - accepted,
            month["missing_hour_buckets"],
        ]
        rows.append("| " + " | ".join(map(str, values)) + " |")
    findings = report["findings"]
    rows += [
        "",
        f"Режим данных: {report['data_mode']}.",
        f"Максимальный входной объём: {findings['highest_input_month']}; "
        f"минимальный: {findings['lowest_input_month']}.",
        f"Границы длительности: {findings['duration_min_seconds']} — "
        f"{findings['duration_max_seconds']} секунд; пики сохранены с флагами.",
        "Покрытие маршрутов на базовом canonical слое: "
        f"{findings['route_coverage_rows']} / "
        f"{findings['route_coverage_denominator']}.",
        "Календарный пик объёма (NY weekday/hour, Sunday=0): "
        f"{findings['peak_hour_of_week']}; пропущенных UTC hour buckets: "
        f"{findings['missing_hour_buckets']}.",
        f"Raw null rates: {findings['raw_missing_rates']}.",
    ]
    rows += [
        "",
        "Сезонность и покрытие",
        "",
        (
            "Объёмы по месяцам, NY-дням и часам недели измерены в "
            "профилях; различия не объявляются причинным эффектом. "
            "Пики длительности и поездки вне широкого описательного "
            "NYC bounding box не удаляются автоматически."
        ),
        (
            "Station/route coverage ограничено отсутствующими ID и "
            "координатами. Доли категорий сравниваются только в "
            "современной сопоставимой схеме; новые категории явно "
            "перечислены в drift."
        ),
        (
            "Временные пробелы считаются отсутствием наблюдений. "
            "Публикационная задержка по Last-Modified может отражать"
            " переиздание, поэтому не доказывает исходную train-time"
            " availability."
        ),
        "",
        "Качество и независимость",
        "",
        (
            "Для каждого CSV сохранён итоговый статус каждой строки;"
            " отдельный CSV/ZoneInfo/SQLite расчёт сверяет "
            "row-decision SHA и метрики. Межмесячные решения "
            "дополнительно сверяются по независимым raw "
            "fingerprints. Exact duplicates канонизируются, "
            "конфликтующие ID изолируются."
        ),
        f"Итоговые dispositions: {report['dispositions']}. "
        f"Cross-source: {report['cross_source']}.",
        "Reason flags могут пересекаться; их сумма не равна числу исключённых строк.",
        "Межархивная коррекция accepted rows: "
        + (
            "; ".join(
                f"{m['period']}: {m['base_dispositions'].get('accepted', 0)} → "
                f"{m['dispositions'].get('accepted', 0)}"
                for m in monthly
                if m["base_dispositions"] != m["dispositions"]
            )
            or "нет"
        )
        + ". Числа в таблице относятся к source partitions, не к витрине F3.",
        "",
        "GBFS и исторические соединения",
        "",
        f"Настоящих collections: {len(report['gbfs']['collections'])}; "
        f"station-history join: {report['station_history_join']}.",
        (
            "История начинается с реально полученных наблюдений и не"
            " достраивается в прошлое; TTL gaps и stale выделены. "
            "Характеристики текущих станций не присваиваются "
            "поездкам прошлых месяцев."
        ),
        (
            "Weather отсутствует: соединение не выполнялось. "
            "F4 использует отдельные retrospective experiments и ERA5 outcome labels; "
            "историческая публикационная availability остаётся неизвестной."
        ),
        "",
        "Измерения и ограничения",
        "",
        f"Публикация: {report['measurements']}. "
        "Архивные workers отдельно записывают wall time, OS peak RSS и output bytes. "
        "Повторы и incremental runs записаны в evidence.",
        (
            "Первый запуск включал failed attempts и cached recovery; чистый cold "
            "end-to-end benchmark не измерен. RSS относится к отдельным процессам; "
            "общий concurrent RAM не измерялся. Две ранние подготовки не имеют "
            "отдельного base-phase RSS; сохранён RSS соответствующих attempts. "
            "Engine memory limit не равен OS peak RSS."
        ),
        (
            "Результаты относятся к выбранным источникам и "
            "наблюдённым GBFS-сборам; они не доказывают скрытый "
            "спрос, круглосуточную доступность или production SLA."
        ),
        "",
        (
            "Источники: [официальный inventory](https://s3.amazonaws.com/tripdata), "
            "[GBFS discovery](https://gbfs.citibikenyc.com/gbfs/2.3/gbfs.json), "
            "[GBFS 2.3](https://github.com/MobilityData/gbfs/blob/v2.3/gbfs.md), "
            "[Data Sharing Policy](https://citibikenyc.com/data-sharing-policy)."
            if report["data_mode"] == "real_official"
            else "Controlled fixtures: официальные feeds не запрашивались."
        ),
    ]
    if "research" in report:
        r = report["research"]
        rows += [
            "",
            "## Исследовательский EDA / ML-readiness",
            "",
            f"Версия {r['version']}; effective Silver snapshot "
            f"`{report['snapshot_id']}`. "
            f"Event window NY: {report['window'][0]}…{report['window'][-1]}; "
            f"{r['station_universe']} наблюдаемых IDs; "
            f"{r['stable_24_month_ids']} во всех месяцах.",
            "Grain: station ID × UTC hour × departures/arrivals. "
            "IDs не равны установкам "
            "или active_stations. Все доли и квантили используют явно eligible labels.",
            "Ноль: городской source-hour наблюдён, station event любого типа не далее "
            "7 дней до и после target в том же NY месяце. Будущее "
            "bracketing применяется "
            "только к оценке label; features его не получают. "
            "Source gaps и длинные/краевые "
            "station gaps остаются missing; отсутствие поездок не "
            "доказывает закрытие станции.",
            "",
            (
                "| Месяц | Target | Eligible / candidate | Zero | "
                "Source gap | Station gap | Mean | Stable mean |"
            ),
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for m in r["monthly"]:
            rows.append(
                f"| {m['period']} | {m['prediction_type']} | {m['eligible_hours']} / "
                f"{m['candidate_hours']} | {m['zero_hours']} | "
                f"{m['source_gap_hours']} | "
                f"{m['station_gap_hours']} | {m['mean']} | {m['stable_mean']} |"
            )
        for h in report["research_hypotheses"]:
            import json

            rows += [
                "",
                f"### {h['id']}: {h['hypothesis']}",
                "",
                f"Population/grain: {h['population_grain']}.",
                f"Расчёт: {h['calculation']}.",
                "Результат: `"
                + json.dumps(h["result"], ensure_ascii=False, default=str)
                + "`.",
                f"Ограничения: {h['limitations']}.",
                f"Проверка: {h['verification']}.",
            ]
        rows += [
            "",
            "### Источники labels и ограничения ML",
            "",
            f"Прямой подсчёт targets из Silver: `{r['target_provenance']}`; "
            f"события вне окна: `{r['out_of_window_events']}`.",
            f"Независимых расхождений по event-month: {r['independent_mismatches']}; "
            f"Python label oracle: `{r['label_oracle']}`; "
            f"дубликаты label grain: {r['join_duplicate_keys']}.",
            "Исключения по event month / station распределениям — "
            "`research.station_exclusions`; "
            "по source month — `monthly.dispositions`. Reason flags "
            "пересекаются и не складываются. "
            "Детальные churn/weekly/daily/geo/histograms/shifts и "
            "исполнявшийся SQL — в существующем JSON.",
            "F4 cohort read-only: `"
            + str(
                {
                    k: v
                    for k, v in report["ml_readiness"].items()
                    if k
                    in (
                        "status",
                        "dataset_id",
                        "coverage",
                        "dense_label_zero_hours",
                        "dense_label_missing_hours",
                        "join_duplicate_keys",
                        "limitation",
                    )
                }
            )
            + "`.",
            "Историческая первая публикация архивов UNKNOWN. "
            "retrieved_at/Last-Modified не "
            "доказывают доступность на прошлом origin; +45d — гипотеза F4. "
            "Calendar известен заранее; actual weather — только "
            "outcome diagnostics; historical "
            "GBFS/capacity/availability не восстановлены из будущих "
            "снимков. Strict operational "
            "ML-readiness и причинные эффекты этим EDA не доказаны.",
            f"Новый EDA run: `{r['measurements']}`; "
            f"полный report: `{report['reproduction']}`.",
            "Измерения wall/RSS — shared host, warm caches, OS "
            "process high-water, не citywide SLA. "
            "Исходные ingest/SQLite row audits выше сохраняют "
            "прежние даты; в этой задаче "
            "они checksum-bound, а не повторно исполненный CSV/SQLite проход.",
            "",
            "Воспроизведение (без download/build/train):",
            "",
            "```sh",
            "uv run --locked --group ml python -m citibike.history verify --inventory "
            + report["reproduction"]["inventory_path"].replace("\\", "/"),
            "```",
        ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(rows), encoding="utf-8")
