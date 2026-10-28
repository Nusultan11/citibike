"""Compact, source-backed EDA evidence; full monthly profiles stay outside Git."""

import os
import platform
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.ingestion.archive import checksum
from citibike.ingestion.gbfs import GBFS_PIPELINE
from citibike.storage import read_json, write_json


def gbfs_evidence(root: Path) -> dict[str, Any]:
    collections = []
    for path in sorted(
        (root / "gbfs" / "silver" / GBFS_PIPELINE).glob("*/quality.json")
    ):
        quality = read_json(path)
        collections.append(
            {
                k: quality[k]
                for k in [
                    "collection_id",
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
        if grain[1] or grain[2] or any(overlaps.values()):
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
    inventory_report = read_json(inventory)
    manifest = read_json(snapshot / "manifest.json")
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
    join_coverage = {
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
                row = db.execute("""SELECT count(*) tested_rows,
                    count(*) FILTER (WHERE n>0) matched,
                    count(*) FILTER (WHERE n>1) exploded FROM (
                    SELECT source_object_id,source_file,source_row_number,
                    count(h.station_id) n
                    FROM trips t LEFT JOIN station_history h
                    ON t.start_station_id=h.trip_station_id
                    AND t.started_at_utc>=h.valid_from
                    AND t.started_at_utc<h.valid_to GROUP BY 1,2,3)""").fetchone()
                assert row is not None
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
            "gbfs",
            "f2/work",
            "downloads",
        ]
    }
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
            "No weather/forecast data or historical GBFS before "
            "collection. Source Last-Modified can represent a "
            "reissue; first-publication availability is unknown. F4 "
            "not implemented."
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
    write_json(output, report)
    return report


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
        "| Месяц | CSV | Вход | Канонические | Исключения | Пропущенные часы |",
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
            "Прогнозы и ML training не реализованы; отсутствующая "
            "train/serve availability остаётся неизвестной."
        ),
        "",
        "Измерения и ограничения",
        "",
        f"Публикация: {report['measurements']}. "
        "Архивные workers отдельно записывают wall time, OS peak RSS и output bytes. "
        "Повторы и incremental runs записаны в evidence.",
        (
            "Результаты относятся к выбранным источникам и "
            "наблюдённым GBFS-сборам; они не доказывают скрытый "
            "спрос, круглосуточную доступность или production SLA."
        ),
        "",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(rows), encoding="utf-8")
