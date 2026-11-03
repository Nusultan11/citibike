"""Freeze inputs, hypotheses and budgets before evaluating diagnostic outcomes."""

import importlib.metadata
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citibike.decision_research import protected
from citibike.features.contracts import utc
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json

VERSION = "f4-ml-diagnostics-1"
EVIDENCE = Path("docs/evidence/f4-ml-diagnostics.json")
RULES = {
    "min_cell_history": 4,
    "spike_z": 6.0,
    "spike_count": 20,
    "zero_hours": 6,
    "zero_expected": 12.0,
    "cusum_k": 0.5,
    "cusum_h": 8.0,
    "cusum_min_hours": 3,
    "z_clip": 10.0,
    "merge_gap_hours": 1,
    "top_k": 10,
}


def boundary(value: str) -> datetime:
    """Frozen F4 date-only window boundaries explicitly mean 00:00 UTC."""
    return utc(value + "T00:00:00+00:00" if len(value) == 10 else value)


def recipe() -> dict[str, str]:
    # Canonical new-source text is portable across autocrlf checkouts. Existing
    # frozen research code is neither edited nor re-hashed with another policy.
    return {
        p.name: identity(p.read_text(encoding="utf-8").replace("\r\n", "\n"))
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }


def check_files(files: dict[str, str]) -> None:
    for name, expected in files.items():
        if not Path(name).is_file() or checksum(Path(name)) != expected:
            raise ValueError(f"Protected checksum mismatch: {name}")


def freeze(
    root: Path,
    dataset: Path,
    outcome: Path,
    lineage: Path,
    *,
    corrects: Path | None = None,
) -> Path:
    data, old, ids = checked(dataset), checked(outcome), checked(lineage)
    gold = Path(data["gold_path"])
    report = read_json(gold / "manifest.json")
    snapshot = read_json(Path(report["source_snapshot"]) / "manifest.json")
    if (
        checksum(gold / "manifest.json") != data["gold_manifest_checksum"]
        or checksum(Path(report["source_snapshot"]) / "manifest.json")
        != data["source_manifest_checksum"]
        or report["source_manifest_checksum"] != data["source_manifest_checksum"]
        or report["source_snapshot_id"] != data["source_snapshot_id"]
        or (
            ids.get("gold_manifest_sha256") is not None
            and ids["gold_manifest_sha256"] != data["gold_manifest_checksum"]
        )
    ):
        raise ValueError("Dataset/Gold/Silver/ID lineage mismatch")
    actual_available = max(
        utc(data["created_at"]),
        utc(report["created_at"]),
        utc(snapshot["published_at"]),
        utc(ids["created_at"]),
        utc(old["created_at"]),
    )
    sources = []
    for part in snapshot["partitions"]:
        manifest = root / "bronze/NYC" / part["source_object_id"] / "manifest.json"
        source = read_json(manifest)
        actual_available = max(actual_available, utc(source["retrieved_at"]))
        sources.append(
            {
                "period": part["period"],
                "path": str(manifest),
                "manifest_sha256": checksum(manifest),
                "source_url": source["source_url"],
                "checksum": source["checksum"],
                "retrieved_at": source["retrieved_at"],
                "published_at": None,
                "silver": str(Path(part["path"]) / "silver.parquet"),
                "silver_sha256": part["checksums"]["silver.parquet"],
                "archive": str(manifest.parent / "source.zip"),
                "pipeline_version": part.get("pipeline_version", "controlled_fixture"),
                "source_version": part.get("source_version", source["checksum"]),
            }
        )
    trials = []
    for item in old["stages"]["S1_features"]:
        if item["population"] != "cohort64" or item["name"] != "seasonal_naive":
            continue
        if item["status"] != "success":
            raise ValueError("Frozen Seasonal Naive trial unavailable")
        trial = checked(Path(item["path"]))
        if trial["request"]["dataset_id"] != data["id"]:
            raise ValueError("Frozen trial dataset lineage mismatch")
        trials.append(
            {
                "fold": item["fold"],
                "prediction_type": item["prediction_type"],
                "path": item["path"],
                "manifest_sha256": checksum(Path(item["path"]) / "manifest.json"),
                "predictions_sha256": trial["checksums"]["predictions.parquet"],
            }
        )
    windows = [
        {
            "fold": f"T{i}",
            "start": boundary(a).isoformat(),
            "end": boundary(b).isoformat(),
        }
        for i, (a, b) in enumerate(data["config"]["validation"])
    ]
    if len(trials) != 2 * len(windows) or len(
        {(t["fold"], t["prediction_type"]) for t in trials}
    ) != len(trials):
        raise ValueError("Both targets and every validation fold required")
    if any(utc(w["end"]) >= boundary(data["config"]["final"][0]) for w in windows):
        raise ValueError("Validation diagnostics reaches frozen final test")
    excluded = sorted({p[k] for p in ids["pairs"] for k in ("short_id", "twin_id")})
    files = protected(root, gold)
    for source in sources:
        if data["data_mode"] == "real_official":
            files[source["archive"]] = source["checksum"]
        files[source["silver"]] = source["silver_sha256"]
    # Include all old decision research, contracts, code and evidence. Exclude
    # mutable F7/staging execution logs, but protect every active metadata file.
    files.update(
        {
            p.as_posix(): checksum(p)
            for p in (root / "decision_research").rglob("*")
            if p.is_file()
        }
    )
    for p in root.rglob("active*.json"):
        if "staging" not in p.parts:
            files[p.as_posix()] = checksum(p)
    for p in [
        dataset / "manifest.json",
        outcome / "manifest.json",
        lineage / "manifest.json",
        *(Path(s["path"]) for s in sources),
    ]:
        files[p.as_posix()] = checksum(p)
    tracked = subprocess.check_output(["git", "ls-files", "-z"], text=True).split("\0")
    for name in tracked:
        if (
            name
            and name
            not in (
                "README.md",
                "ROADMAP.md",
                EVIDENCE.as_posix(),
                "tests/ml/test_ml_diagnostics.py",
            )
            and not name.startswith("src/citibike/ml_diagnostics/")
        ):
            p = Path(name)
            files[p.as_posix()] = checksum(p)
    now = datetime.now(UTC)
    if actual_available > now:
        raise ValueError("Inputs not available at analysis as-of")
    check_files(files)
    plan = {
        "version": VERSION,
        "code": recipe(),
        "rules": RULES,
        "dataset": str(dataset),
        "dataset_id": data["id"],
        "outcome": str(outcome),
        "lineage": str(lineage),
        "data_mode": data["data_mode"],
        "sources": sources,
        "analysis_as_of": now.isoformat(),
        "input_available_at": actual_available.isoformat(),
        "windows": windows,
        "trials": sorted(trials, key=lambda t: (t["fold"], t["prediction_type"])),
        "excluded_ids": excluded,
        "mapping": "source IDs unchanged; no merge",
        "cohort": "existing operational cohort64 only; not citywide",
        "history": "same station x NY weekday/hour x meteorological season; "
        "observed labels only; event hour < window start; >=4 observations; "
        "no fallback or imputation; history frozen within each window",
        "as_of_policy": "strict retrospective analysis at analysis_as_of using "
        "actual local receipt/materialization. Historical first publication UNKNOWN. "
        "No strict-origin feature/detection claim; +45d is a separate proxy only. "
        "Hindsight 7-day label brackets and cohort/ID exclusions are post-hoc.",
        "hypotheses": {
            "H1": "Season-matched mean reduces paired absolute error against SN "
            "on supported cells in >=3/4 windows for BOTH targets, >=50% coverage; "
            "otherwise inconclusive. Exploratory feature hypothesis only.",
            "H2": "6 consecutive observed zeros with >=12 expected events warrant "
            "review; neither missing nor known-low demand should trigger.",
            "H3": "Clipped two-sided CUSUM detects sustained standardized shifts "
            "in controlled streams; real rarity is not a data-error label.",
        },
        "decision": "Only H1 may propose ONE future seasonal-profile feature "
        "ablation. No training/loss selection/promotion; otherwise inconclusive.",
        "controls": "constant and seeded Poisson null; isolated spike, zero-run, "
        "positive/negative shift; missing, delayed publication, DST folds, "
        "ambiguous ID, cold start, absent history. No threshold search.",
        "labels": "No independent expert anomaly labels; Precision@K and real "
        "false-positive rate UNKNOWN. Synthetic detection does not prove accuracy.",
        "budget": {
            "wall_seconds": 1800,
            "duckdb_memory": "1GB",
            "threads": 2,
            "rss_gate_bytes": 3 * 1024**3,
            "output_bytes": 256 * 1024**2,
        },
        "final_test_scored": False,
        "final_prediction_artifacts_accessed": False,
        "source_anchor_audit": "Independent label audit may read Silver events "
        "after the UTC cutoff within the same NY month only as bracketing "
        "anchors. No final forecast artifacts/metrics or selection are read.",
        "protected_files": files,
        "environment": {
            p: importlib.metadata.version(p) for p in ("duckdb", "numpy", "pandas")
        },
        "provenance": "Thresholds precede this evaluation, informed by prior F4 "
        "findings. Reused validation windows, not blind/new independent evidence.",
    }
    if corrects is not None:
        prior = checked(corrects)
        previous = prior["plan"]
        for field in (
            "rules",
            "windows",
            "dataset",
            "dataset_id",
            "trials",
            "excluded_ids",
            "hypotheses",
            "history",
            "budget",
        ):
            if plan[field] != previous[field]:
                raise ValueError(
                    "Verifier correction must retain the scientific protocol"
                )
        if any(
            plan["code"][name] != previous["code"][name]
            for name in ("baseline.py", "events.py")
        ):
            raise ValueError("Verifier correction cannot change baseline/detectors")
        plan["corrects_verifier"] = {
            "protocol": str(corrects),
            "protocol_id": prior["id"],
            "manifest_sha256": checksum(corrects / "manifest.json"),
            "reason": "Oracle truncated next-event anchors at UTC audit cutoff; "
            "fixed NY-month boundary only. Original thresholds/hypotheses "
            "precede the first evaluation; this correction is after that result "
            "existed, not a new blind pre-registration.",
        }
    return create(
        root / "decision_research",
        "diagnostics",
        identity([VERSION, plan]),
        lambda stage: {"kind": "ml_diagnostics_protocol", "plan": plan},
        activate=False,
    )


def load_plan(path: Path, *, protected_check: bool = True) -> dict[str, Any]:
    report = checked(path)
    plan: dict[str, Any] = report["plan"]
    if report["id"] != identity([VERSION, plan]) or plan["code"] != recipe():
        raise ValueError("Diagnostics protocol identity/code mismatch")
    if plan["rules"] != RULES or plan["version"] != VERSION:
        raise ValueError("Diagnostics rules mismatch")
    if protected_check:
        check_files(plan["protected_files"])
    return plan
