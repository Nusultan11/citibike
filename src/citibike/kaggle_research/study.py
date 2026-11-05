"""Freeze and execute bounded portable trials using existing F4 data and models."""

import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from citibike.decision_research.features import gate
from citibike.forecasting.artifacts import checked
from citibike.forecasting.evaluation_weather import attach
from citibike.forecasting.feature_research import read_frame, row_hash, save_frame
from citibike.forecasting.metrics import segmented
from citibike.ingestion.archive import checksum
from citibike.kaggle_research.models import QUANTILES, configurations, predictions
from citibike.storage import identity, read_json, write_json

PACKAGES = (
    "numpy",
    "pandas",
    "scipy",
    "scikit-learn",
    "lightgbm",
    "catboost",
    "duckdb",
    "joblib",
    "threadpoolctl",
    "httpx",
    "pydantic",
    "pydantic-settings",
    "psycopg",
    "psycopg-binary",
    "tzdata",
    "pytz",
)
KEY = [
    "station_id",
    "forecast_origin_utc",
    "target_hour_utc",
    "horizon_h",
    "prediction_type",
]


def environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "packages": {p: importlib.metadata.version(p) for p in PACKAGES},
        "cpu_count": os.cpu_count(),
    }


def validate_frames(train: Any, evaluation: Any, split: dict[str, Any]) -> None:
    """Fail closed on null labels, unknown eligibility, PIT or population changes."""
    import numpy as np

    pd = importlib.import_module("pandas")

    cutoff = pd.Timestamp(split["fit_asof"])
    for frame in (train, evaluation):
        if frame.empty or frame.duplicated(KEY).any():
            raise ValueError("Empty or duplicated population")
        if frame["actual"].isna().any() or not np.isfinite(frame["actual"]).all():
            raise ValueError("Missing observation cannot become a zero")
        if (frame["actual"] < 0).any() or (frame["actual"] % 1 != 0).any():
            raise ValueError("Invalid count label")
        if not (frame["eligibility"] == "eligible").all():
            raise ValueError("Ineligible population")
        if (
            frame[["known_at", "feature_available_at", "max_event_at"]]
            .isna()
            .any()
            .any()
        ):
            raise ValueError("Missing base availability evidence")
        if not frame["horizon_h"].between(1, 24).all():
            raise ValueError("Invalid horizon")
        if not (
            frame["target_hour_utc"]
            == frame["forecast_origin_utc"].dt.floor("h")
            + pd.to_timedelta(frame["horizon_h"], unit="h")
        ).all():
            raise ValueError("Origin/target horizon mismatch")
        if (frame["known_at"] > frame["forecast_origin_utc"]).any():
            raise ValueError("Future station identity")
        gate(frame, cutoff.isoformat())
    if (train["label_available_at"] > cutoff).any():
        raise ValueError("Future training label")
    if train["target_hour_utc"].max() >= cutoff.to_pydatetime() - timedelta(hours=24):
        raise ValueError("Embargo violated")
    if (evaluation["forecast_origin_utc"] < cutoff).any() or (
        evaluation["forecast_origin_utc"] >= pd.Timestamp(split["origin_end"])
    ).any():
        raise ValueError("Validation window substitution")
    for kind in ("departures", "arrivals"):
        part = evaluation[evaluation["prediction_type"] == kind]
        if row_hash(part) != split["populations"][kind]["evaluation_rows_hash"]:
            raise ValueError("Evaluation population hash changed")


def prepare(destination: Path) -> Path:
    """Copy only four already-prepared folds and package public source code."""
    if destination.exists():
        raise ValueError("Use a new content-bound export directory")
    evidence = read_json(Path("docs/evidence/f4-decision-research.json"))
    population = evidence["plan"]["populations"]["cohort64"]
    source = Path(population["inputs"])
    data = checked(source)
    dataset = Path(population["dataset"])
    dataset_report = checked(dataset)
    if checksum(source / "manifest.json") != population["inputs_manifest_sha256"]:
        raise ValueError("Prior input manifest changed")
    if data["dataset_id"] != dataset_report["id"]:
        raise ValueError("Dataset lineage mismatch")
    destination.mkdir(parents=True)
    weather = Path(read_json(Path("data/f4/active_evaluation_weather.json"))["path"])
    checked(weather)
    shutil.copyfile(weather / "daily.json", destination / "weather-daily.json")
    diagnostic = read_json(Path("docs/evidence/f4-ml-diagnostics.json"))
    splits = []
    for split in data["splits"]:
        if split["fold"] not in ("T0", "T1", "T2", "T3"):
            continue
        train = read_frame(source / split["train_file"])
        evaluation = read_frame(source / split["evaluation_file"])
        validate_frames(train, evaluation, split)
        for field in ("train_file", "evaluation_file"):
            shutil.copyfile(source / split[field], destination / split[field])
        splits.append(split)
    if [s["fold"] for s in splits] != ["T0", "T1", "T2", "T3"]:
        raise ValueError("Four temporal folds required")
    with zipfile.ZipFile(destination / "source.zip", "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path("src/citibike").rglob("*.py")):
            z.writestr(p.relative_to("src").as_posix(), p.read_bytes())
        # SQL migrations are import-time resources in existing package modules.
        for p in sorted(Path("src/citibike").rglob("*.sql")):
            z.writestr(p.relative_to("src").as_posix(), p.read_bytes())
    runtime = environment()
    (destination / "requirements.txt").write_text(
        "\n".join(f"{p}=={v}" for p, v in runtime["packages"].items()) + "\n",
        encoding="utf-8",
    )
    plan = {
        "version": "f4-kaggle-research-1",
        "created_at": datetime.now(UTC).isoformat(),
        "base_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "mode": "validation-only approximate retrospective; NOT strict historical PIT",
        "dataset_id": dataset_report["id"],
        "dataset_manifest_sha256": checksum(dataset / "manifest.json"),
        "source_snapshot_id": dataset_report["source_snapshot_id"],
        "gold_manifest_sha256": dataset_report["gold_manifest_checksum"],
        "source_manifest_sha256": dataset_report["source_manifest_checksum"],
        "prior_input_manifest_sha256": checksum(source / "manifest.json"),
        "splits": splits,
        "configurations": configurations(),
        "code": {
            p.relative_to("src/citibike").as_posix(): checksum(p)
            for p in sorted(Path("src/citibike").rglob("*.py"))
        },
        "environment": runtime,
        "hypotheses": {
            "loss": (
                "L1 vs Poisson at identical parameters lowers MAE but may "
                "bias count means"
            ),
            "regularization": (
                "Shallower regularized fits reduce seasonal/rare-station degradation"
            ),
            "features": (
                "Separate A, P, S additions to identical L1 fit improve "
                "calendar/reliability/seasonality"
            ),
            "probabilistic": (
                "Quantile boosting improves pinball and central-80% coverage "
                "over empirical and Poisson-SN baselines"
            ),
            "spikes": (
                "Train-only classifier improves Brier over train prevalence "
                "for published count>=20"
            ),
        },
        "budgets": {
            "trials": 144,
            "worker_wall_seconds": 360,
            "fit_seconds": 300,
            "cumulative_wall_seconds": 7200,
            "peak_rss_bytes": 2 * 1024**3,
            "threads": 2,
        },
        "selection": (
            "Per target majority folds better than SN, no >20% "
            "degradation in predefined key slices n>=100, valid "
            "resources; minimum macro MAE, lexical tie. Validation reuse "
            "makes all new effects exploratory; no promotion."
        ),
        "uncertainty": (
            "5000 crossed station x NY target-day bootstrap; macro folds;"
            " Bonferroni familywise intervals across every frozen "
            "comparison and both targets; exact fold sign-flips reported "
            "separately"
        ),
        "quantiles": list(QUANTILES),
        "quantile_projection": "max(0,q), then row sort; raw crossings recorded",
        "probability_gate": (
            "mean pinball lower than both baselines in >=3/4 folds and "
            "each target; absolute pooled coverage error from 0.8 <=0.05,"
            " n>=100 key-slice coverage >=0.7; exploratory only"
        ),
        "nulls": (
            "Only eligible observed targets; no zero imputation or "
            "validation filtering; numeric NaN handled by original "
            "adapters"
        ),
        "identity": (
            "Raw station IDs unchanged; no physical mapping; ambiguous "
            "IDs reported separately, never merged"
        ),
        "ambiguous_ids": diagnostic["plan"]["excluded_ids"],
        "weather_policy": "ERA5 labels AFTER prediction only; same old weather gates",
        "forbidden_inputs": [
            "final predictions",
            "H0-H3 scoring",
            "short unpublished lags",
            "future actual weather",
            "GBFS backfill",
        ],
        "operational_champion": {
            "departures": "seasonal_naive",
            "arrivals": "seasonal_naive",
        },
        "final_test_scored": False,
        "independent_forward_shadow": "NOT VERIFIED",
        "F8": "PARTIAL",
        "checksums": {
            p.name: checksum(p) for p in sorted(destination.iterdir()) if p.is_file()
        },
    }
    plan["id"] = identity(plan)
    write_json(destination / "protocol.json", plan)
    return destination


def check_bundle(bundle: Path) -> dict[str, Any]:
    plan = read_json(bundle / "protocol.json")
    if identity({k: v for k, v in plan.items() if k != "id"}) != plan["id"]:
        raise ValueError("Protocol identity mismatch")
    for name, sha in plan["checksums"].items():
        if Path(name).name != name or checksum(bundle / name) != sha:
            raise ValueError("Bundle checksum or path mismatch")
    if plan["configurations"] != configurations():
        raise ValueError("Search space drift")
    base = Path(__file__).parents[1]
    for name, sha in plan.get("code", {}).items():
        if (
            not (base / name).resolve().is_relative_to(base.resolve())
            or checksum(base / name) != sha
        ):
            raise ValueError("Research source code drift")
    current = environment()["packages"]
    if current != plan["environment"]["packages"]:
        raise ValueError("Numeric/runtime package versions differ from frozen protocol")
    return plan


def probabilistic_metrics(frame: Any) -> dict[str, Any]:
    import numpy as np

    values = frame[[f"q{q}" for q in QUANTILES]].to_numpy(float)
    y = frame["actual"].to_numpy(float)
    if (
        not np.isfinite(values).all()
        or (values < 0).any()
        or (np.diff(values, axis=1) < 0).any()
    ):
        raise ValueError("Invalid quantile order/support")
    pinball = {}
    calibration = {}
    for q in QUANTILES:
        residual = y - frame[f"q{q}"].to_numpy(float)
        pinball[str(q)] = float(np.maximum(q * residual, (q - 1) * residual).mean())
        calibration[str(q)] = float((residual <= 0).mean())
    return {
        "n": len(y),
        "pinball": pinball,
        "mean_pinball": sum(pinball.values()) / 3,
        "coverage_80": float(((y >= values[:, 0]) & (y <= values[:, 2])).mean()),
        "mean_width": float((values[:, 2] - values[:, 0]).mean()),
        "cdf_at_quantile": calibration,
        "discrete_caveat": (
            "CDF(q_tau) may exceed tau at count atoms; central interval "
            "can be conservative"
        ),
    }


def worker(bundle: Path, request_file: Path, destination: Path) -> None:
    plan = check_bundle(bundle)
    request = read_json(request_file)
    if request["protocol_id"] != plan["id"]:
        raise ValueError("Request protocol mismatch")
    config = plan["configurations"][request["name"]]
    split = next(s for s in plan["splits"] if s["fold"] == request["fold"])
    train, evaluation = (
        read_frame(bundle / split[f]) for f in ("train_file", "evaluation_file")
    )
    validate_frames(train, evaluation, split)
    kind = request["prediction_type"]
    if kind not in ("departures", "arrivals"):
        raise ValueError("Unknown target")
    train, evaluation = (
        f[f["prediction_type"] == kind].copy() for f in (train, evaluation)
    )
    output, resources = predictions(train, evaluation, config)
    output = attach(output, read_json(bundle / "weather-daily.json"))
    output["ambiguous_id"] = output["station_id"].isin(plan["ambiguous_ids"])
    if row_hash(output) != split["populations"][kind]["evaluation_rows_hash"]:
        raise ValueError("Prediction population changed")
    destination.mkdir(parents=True, exist_ok=True)
    columns = [
        *KEY,
        "actual",
        "activity",
        "new_known",
        "local_hour",
        "weekday",
        "month",
        "anomalous_id",
        "suspect_closure",
        "history_depth",
        "ambiguous_id",
    ]
    columns += [c for c in output if c.startswith("eval_weather_")]
    columns += [
        c
        for c in (
            "predicted",
            "probability",
            "baseline_probability",
            "q0.1",
            "q0.5",
            "q0.9",
        )
        if c in output
    ]
    save_frame(destination / "predictions.parquet", output[columns])
    metrics: dict[str, Any] = {}
    if "predicted" in output:
        metrics["point"] = segmented(
            output, output["predicted"].tolist(), research=True
        )
    if config.get("task") == "quantile":
        metrics["probabilistic"] = probabilistic_metrics(output)
    write_json(
        destination / "worker.json",
        {
            "request": request,
            "config": config,
            "resources": resources,
            "metrics": metrics,
            "predictions_sha256": checksum(destination / "predictions.parquet"),
        },
    )


def run(bundle: Path, destination: Path) -> Path:
    plan = check_bundle(bundle)
    if destination.exists():
        raise ValueError("Run outputs are immutable; use a separate attempt directory")
    destination.mkdir(parents=True)
    started = time.monotonic()
    trials = []
    for name in plan["configurations"]:
        for split in plan["splits"]:
            for kind in ("departures", "arrivals"):
                request = {
                    "protocol_id": plan["id"],
                    "name": name,
                    "fold": split["fold"],
                    "prediction_type": kind,
                }
                identifier = identity(request)
                path = destination / identifier
                path.mkdir()
                request_file = path / "request.json"
                write_json(request_file, request)
                before = time.monotonic()
                outcome: dict[str, Any] = {**request, "path": identifier}
                if before - started >= plan["budgets"]["cumulative_wall_seconds"]:
                    outcome.update(
                        status="not_run", reason="cumulative wall budget exhausted"
                    )
                else:
                    try:
                        with (
                            (path / "stdout.txt").open("w") as stdout,
                            (path / "stderr.txt").open("w") as stderr,
                        ):
                            result = subprocess.run(
                                [
                                    sys.executable,
                                    "-m",
                                    "citibike.kaggle_research",
                                    "worker",
                                    "--bundle",
                                    str(bundle.resolve()),
                                    "--request",
                                    str(request_file.resolve()),
                                    "--output",
                                    str(path.resolve()),
                                ],
                                stdout=stdout,
                                stderr=stderr,
                                timeout=plan["budgets"]["worker_wall_seconds"],
                                env={
                                    **os.environ,
                                    "OMP_NUM_THREADS": "2",
                                    "OPENBLAS_NUM_THREADS": "2",
                                    "MKL_NUM_THREADS": "2",
                                },
                                check=False,
                            )
                        if result.returncode != 0:
                            outcome.update(
                                status="failed",
                                reason=f"worker exit {result.returncode}",
                            )
                        else:
                            report = read_json(path / "worker.json")
                            resource = report["resources"]
                            ok = (
                                resource["fit_seconds"]
                                <= plan["budgets"]["fit_seconds"]
                                and resource["peak_rss_bytes"]
                                <= plan["budgets"]["peak_rss_bytes"]
                            )
                            outcome.update(
                                status="success" if ok else "resource_failed", **report
                            )
                    except subprocess.TimeoutExpired:
                        outcome.update(
                            status="timeout", reason="hard worker wall timeout"
                        )
                outcome["worker_wall_seconds"] = time.monotonic() - before
                outcome["files"] = {
                    p.name: checksum(p) for p in sorted(path.iterdir()) if p.is_file()
                }
                write_json(path / "trial.json", outcome)
                trials.append(outcome)
                print(
                    json.dumps(
                        {
                            k: outcome[k]
                            for k in (
                                "name",
                                "fold",
                                "prediction_type",
                                "status",
                                "worker_wall_seconds",
                            )
                        }
                    ),
                    flush=True,
                )
    report = {
        "protocol_id": plan["id"],
        "environment": environment(),
        "trials": trials,
        "wall_seconds": time.monotonic() - started,
        "final_test_scored": False,
    }
    write_json(destination / "result.json", report)
    # Bind every artifact, including failure logs and partial predictions.
    write_json(
        destination / "checksums.json",
        {
            p.relative_to(destination).as_posix(): checksum(p)
            for p in sorted(destination.rglob("*"))
            if p.is_file()
        },
    )
    return destination
