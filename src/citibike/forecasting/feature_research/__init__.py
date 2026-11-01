"""Opt-in post-audit A/B study. Never activates any F4/F5 pointer or test."""

import argparse
import importlib.metadata
import os
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb

from citibike.features.contracts import NY, SEED, TYPES, utc
from citibike.features.dataset import build, settings
from citibike.features.research import CATALOG, SCHEMA, VERSION, B, enrich, gate
from citibike.forecasting import models as legacy_models
from citibike.forecasting.artifacts import active, checked, create
from citibike.forecasting.evaluation_weather import attach
from citibike.forecasting.experiment import ROW_KEY, split_frames
from citibike.forecasting.feature_research import models as feature_models
from citibike.forecasting.metrics import error_analysis, segmented
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json

SUPERSEDED = {
    "protocol": "69a184b58d11eab35257550ee5c762723bc7c6bd20fe87a1598b2fb03658668b",
    "outcome": "8ded015982b1ce7a385a4e1be0bf4685c8b9e011b67c5a779a02e9b27945f2d6",
    "reason": "Audit before any metric was read: no Poisson base for ablation, "
    "prior weather key segments absent from the gate, no decision rule, "
    "verifier bound to the fit hash. Outcomes retained, never reused as results.",
}
HYPOTHESES = {
    "H1_level_shift": {
        "claim": "Delayed labels leave train without the validation season; bias "
        "sign follows the validation/train level ratio across folds",
        "expect": "A cannot extrapolate an unseen month; B helps only where a "
        "published month or annual profile already carries the level",
        "check": "fold bias, fold0 delta, seasonal_month missing share",
    },
    "H2_sparse_station": {
        "claim": "Stations with little or no train history carry excess error",
        "expect": "B station-level history helps new/low stations more than A",
        "check": "activity:new/new_known, unseen-station rows, named stations",
    },
    "H3_peak_tail": {
        "claim": "actual>=20 concentrates absolute error but is a post-hoc slice; "
        "conditional means regress towards the mean",
        "expect": "No feature group removes the tail bias",
        "check": "actual:peak_ge20 versus predefined peak_hour; never a gate",
    },
    "H4_peak_calendar": {
        "claim": "Predefined weekday peaks need calendar interactions",
        "expect": "A lowers peak_hour MAE against the same algorithm base",
        "check": "peak_hour fold deltas, paired A vs base",
    },
    "H5_stale_trend": {
        "claim": "Published trend lags 45+ days and can flip at seasonal turns",
        "expect": "B effect changes sign across folds; no stable gain from trend",
        "check": "B vs base fold differences; group ablation",
    },
}
DECISION_RULE = (
    "Per target: a variant is a research candidate only if it wins the majority "
    "of folds against Seasonal Naive with no >20% key-segment degradation "
    "(n>=100) and valid resources in cohort64 AND cohort128; lowest cohort64 "
    "macro MAE among those, lexical tie; otherwise seasonal_naive"
)
GROUP_RULE = (
    "A group effect is claimed only if the 95% crossed interval of macro MAE "
    "difference excludes zero and the same sign holds in most folds; "
    "otherwise inconclusive"
)


def recipe() -> dict[str, str]:
    """Bind everything that can change a fit; the verifier is hashed in evidence."""
    base = Path(__file__).parents[2]
    return {
        p.relative_to(base).as_posix(): checksum(p)
        for folder in ("features", "forecasting")
        for p in sorted((base / folder).rglob("*.py"))
        if p != Path(__file__).with_name("verify.py")
    }


def frozen(root: Path) -> dict[str, str]:
    """Protect all previously existing F4/F5 files, including inactive artifacts."""
    return {
        str(p): checksum(p)
        for folder in (root / "f4", root / "f5")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
        and not any(part.startswith("feature_") for part in p.relative_to(folder).parts)
    }


def assert_hashes(files: dict[str, str]) -> None:
    for file, expected in files.items():
        if checksum(Path(file)) != expected:
            raise ValueError(f"Protected input changed: {file}")


def read_frame(path: Path) -> Any:
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        return db.execute(
            "SELECT * FROM read_parquet(?) ORDER BY "
            "prediction_type,station_id,forecast_origin_utc,horizon_h",
            [str(path)],
        ).df()


def save_frame(path: Path, frame: Any) -> None:
    with duckdb.connect() as db:
        db.register("frame", frame)
        db.execute("COPY frame TO ? (FORMAT PARQUET,COMPRESSION ZSTD)", [str(path)])


def row_hash(frame: Any) -> str:
    return identity(frame[ROW_KEY].astype(str).values.tolist())


def distribution(frame: Any) -> dict[str, Any]:
    import importlib

    np = importlib.import_module("numpy")

    values = frame["actual"]
    return {
        "n": len(frame),
        "stations": int(frame["station_id"].nunique()),
        "actual_mean": float(values.mean()),
        "zero_fraction": float((values == 0).mean()),
        "peak_ge20_fraction": float((values >= 20).mean()),
        "bins": np.histogram(values, bins=[0, 1, 5, 20, float("inf")])[0].tolist(),
    }


def concentration(frame: Any) -> dict[str, Any]:
    """Share of rows, demand and absolute error; outcome slices stay descriptive."""
    error = frame["residual"].abs()
    total = float(error.sum())
    demand = float(frame["actual"].sum())
    peak = (frame["weekday"] < 5) & frame["local_hour"].isin([7, 8, 9, 16, 17, 18, 19])

    def share(mask: Any) -> dict[str, float | None]:
        return {
            "rows": float(mask.mean()),
            "actual": float(frame.loc[mask, "actual"].sum()) / demand
            if demand
            else None,
            "absolute_error": float(error[mask].sum()) / total if total else None,
        }

    stations = error.groupby(frame["station_id"]).sum().sort_values(ascending=False)
    return {
        "actual_ge20": share(frame["actual"] >= 20),
        "peak_hour": share(peak),
        "top5_stations_absolute_error": float(stations.head(5).sum()) / total
        if total
        else None,
        "stations": len(stations),
    }


def coverage_shift(train: Any, scoring: Any) -> dict[str, Any]:
    """Train coverage and level shift; rank correlation is association only."""

    def months(frame: Any) -> dict[str, int]:
        local = frame["target_hour_utc"].dt.tz_convert(NY).dt.strftime("%Y-%m")
        return {str(k): int(v) for k, v in local.value_counts().sort_index().items()}

    observed, predicted = months(train), months(scoring)
    seen = scoring["station_id"].isin(train["station_id"])
    stations = scoring.groupby("station_id").agg(
        actual=("actual", "mean"), bias=("residual", "mean")
    )
    level = train.groupby("station_id")["actual"].mean().reindex(stations.index)
    ratio = (stations["actual"] + 1) / (level + 1)
    valid = ratio.notna()
    rank = (
        ratio[valid].rank().corr(stations.loc[valid, "bias"].rank())
        if int(valid.sum()) >= 3
        else None
    )
    return {
        "train_target_months": observed,
        "validation_target_months": predicted,
        "validation_calendar_months_in_train": sorted(
            {m[-2:] for m in predicted} & {m[-2:] for m in observed}
        ),
        "validation_to_train_mean_ratio": float(
            scoring["actual"].mean() / train["actual"].mean()
        ),
        "validation_rows_unseen_station": int((~seen).sum()),
        "validation_stations_unseen": int(scoring.loc[~seen, "station_id"].nunique()),
        "train_stations": int(train["station_id"].nunique()),
        "station_rank_correlation_level_ratio_vs_bias": None
        if rank is None or rank != rank
        else float(rank),
    }


def root_cause(root: Path, dataset: Path) -> Path:
    """Read only the old validation predictions and training files, never final."""
    experiment = active(root, "experiments")
    report = checked(experiment)
    if report["dataset_id"] != checked(dataset)["id"]:
        raise ValueError("Frozen validation dataset mismatch")
    identifier = identity(
        [VERSION, "root-cause", checksum(experiment / "manifest.json"), recipe()]
    )

    def diagnose(stage: Path) -> dict[str, Any]:
        details = []
        for result in report["results"]:
            if result["model_name"] not in (
                "seasonal_naive",
                "lightgbm_bounded",
                "catboost_bounded",
            ):
                continue
            prediction = experiment / result["predictions"]
            scoring = read_frame(prediction)
            train_file = experiment / report["splits"][result["fold"]]["training_file"]
            train = read_frame(train_file)
            train = train[train["prediction_type"] == result["prediction_type"]]
            stations = {}
            for station in ("6753.08", "6230.02"):
                t = train[train["station_id"] == station]
                v = scoring[scoring["station_id"] == station]
                with duckdb.connect() as db:
                    count = db.execute(
                        "SELECT count(*) FROM read_parquet(?) WHERE "
                        "station_id=? AND prediction_type=? AND eligibility='eligible' "
                        "AND label_available_at<=?::TIMESTAMPTZ AND "
                        "target_hour_utc<?::TIMESTAMPTZ-INTERVAL '24 hours'",
                        [
                            str(dataset / "examples.parquet"),
                            station,
                            result["prediction_type"],
                            report["splits"][result["fold"]]["fit_asof"],
                            report["splits"][result["fold"]]["fit_asof"],
                        ],
                    ).fetchone()
                    assert count is not None
                stations[station] = {
                    "eligible_before_block_sampling": count[0],
                    "train": distribution(t) if len(t) else {"n": 0},
                    "validation": distribution(v) if len(v) else {"n": 0},
                    "metrics": segmented(v, v["predicted"].tolist(), research=True)
                    if len(v)
                    else {},
                }
            td, vd = distribution(train), distribution(scoring)
            tv = (
                sum(
                    abs(a / td["n"] - b / vd["n"])
                    for a, b in zip(td["bins"], vd["bins"], strict=True)
                )
                / 2
            )
            details.append(
                {
                    "fold": result["fold"],
                    "target": result["prediction_type"],
                    "model": result["model_name"],
                    "file": str(prediction),
                    "sha256": checksum(prediction),
                    "training_file": str(train_file),
                    "training_sha256": checksum(train_file),
                    "train": td,
                    "validation": vd,
                    "train_validation_count_bin_TV": tv,
                    "concentration": concentration(scoring),
                    "coverage_shift": coverage_shift(train, scoring),
                    "stations": stations,
                    "metrics": segmented(
                        scoring, scoring["predicted"].tolist(), research=True
                    ),
                }
            )
        write_json(stage / "diagnostics.json", details)
        return {
            "kind": "feature_root_cause",
            "dataset_id": checked(dataset)["id"],
            "experiment": str(experiment),
            "final_accessed": False,
            "hypotheses": HYPOTHESES,
            "limits": "actual>=20 is post-hoc; no causal/operational calibration claim",
        }

    return create(root, "feature_diagnostics", identifier, diagnose, activate=False)


def inputs(root: Path, dataset: Path) -> Path:
    data = checked(dataset)
    identifier = identity([VERSION, data["id"], SCHEMA, recipe()])

    def prepare(stage: Path) -> dict[str, Any]:
        splits = []
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
            for fold, (first, last) in enumerate(data["config"]["validation"]):
                fit_asof = utc(first + "T00:00:00+00:00")
                train, evaluation, split = split_frames(
                    db,
                    dataset,
                    fit_asof,
                    utc(last + "T00:00:00+00:00"),
                    data["config"]["max_train_rows"],
                )
                for name, frame in (("train", train), ("evaluation", evaluation)):
                    enriched = enrich(
                        frame, dataset / "dense.parquet", fit_asof.isoformat()
                    )
                    if len(enriched) != len(frame) or row_hash(enriched) != row_hash(
                        frame
                    ):
                        raise ValueError("Feature join changed population")
                    filename = f"{fold}-{name}.parquet"
                    save_frame(stage / filename, enriched)
                    split[name + "_file"] = filename
                    split[name + "_file_sha256"] = checksum(stage / filename)
                    split[name + "_b_missing_fraction"] = {
                        column: float(enriched[column].isna().mean()) for column in B
                    }
                split["populations"] = {
                    kind: {
                        "evaluation_rows_hash": row_hash(
                            evaluation[evaluation["prediction_type"] == kind]
                        ),
                        "evaluation": distribution(
                            evaluation[evaluation["prediction_type"] == kind]
                        ),
                        "train": distribution(train[train["prediction_type"] == kind]),
                    }
                    for kind in TYPES
                }
                splits.append(split)
        return {
            "kind": "feature_inputs",
            "dataset_id": data["id"],
            "splits": splits,
            "schema": SCHEMA,
            "recipe": recipe(),
            "final_test_accessed": False,
        }

    return create(root, "feature_inputs", identifier, prepare, activate=False)


def outcome_weather(root: Path, data: dict[str, Any]) -> dict[str, Any]:
    """Same post-prediction labels as the corrective protocol; never model input."""
    if data["data_mode"] != "real_official":
        return {}
    path = active(root, "evaluation_weather")
    return {
        "daily_file": str(path / "daily.json"),
        "sha256": checksum(path / "daily.json"),
        "policy": checked(path)["policy"],
        "use": "Key-segment gate and diagnostics AFTER prediction; not a feature",
    }


def superseded(root: Path) -> dict[str, str] | None:
    protocol_file = root / "f4/feature_protocols" / SUPERSEDED["protocol"]
    outcome_file = root / "f4/feature_outcomes" / SUPERSEDED["outcome"]
    if not (protocol_file.exists() and outcome_file.exists()):
        return None
    return {
        **SUPERSEDED,
        "protocol_manifest_sha256": checksum(protocol_file / "manifest.json"),
        "outcome_manifest_sha256": checksum(outcome_file / "manifest.json"),
    }


def protocol(root: Path, dataset: Path, *, expand: bool = True) -> Path:
    before = frozen(root)
    base = checked(dataset)
    diagnostics = root_cause(root, dataset)
    datasets = {"cohort64": dataset}
    expansion: dict[str, Any] = {}
    if expand:
        try:
            expanded = build(
                root / "f4/feature_expansion",
                Path(base["gold_path"]),
                station_count=128,
                smoke=base["config"]["smoke"],
            )
            datasets["cohort128"] = expanded
            expansion = {"status": "available", "path": str(expanded)}
        except Exception as error:
            expansion = {
                "status": "blocked",
                "error": type(error).__name__,
                "message": str(error)[:2000],
            }
    else:
        expansion = {"status": "not_requested", "reason": "explicit fixture option"}
    population = {}
    for label, path in datasets.items():
        data = checked(path)
        prepared = inputs(root, path)
        population[label] = {
            "dataset": str(path),
            "dataset_manifest_sha256": checksum(path / "manifest.json"),
            "dataset_id": data["id"],
            "dataset_checksums": data["checksums"],
            "source_manifest_sha256": data["source_manifest_checksum"],
            "source_snapshot_id": data["source_snapshot_id"],
            "gold_manifest_sha256": data["gold_manifest_checksum"],
            "inputs": str(prepared),
            "inputs_manifest_sha256": checksum(prepared / "manifest.json"),
            "stations": station_ids(path),
            "activity_stations": activity_counts(path),
            "coverage": data["coverage"],
            "eligibility": data["eligibility"],
        }
    if "cohort128" in population and not set(
        population["cohort64"]["stations"]
    ).issubset(population["cohort128"]["stations"]):
        raise ValueError("Expansion lost original cohort IDs")
    configs = legacy_models.configurations(base["config"]["smoke"])
    variants = {"seasonal_naive": configs["seasonal_naive"]}
    for algorithm in ("lightgbm_bounded", "catboost_bounded"):
        for group in ([], ["A"], ["B"], ["A", "B"]):
            label = "+".join(group) or "base"
            variants[f"{algorithm}__{label}"] = {
                **configs[algorithm],
                "research_groups": group,
            }
    for label, group in (("base", []), ("A", ["A"])):
        variants[f"poisson_bounded__{label}"] = {
            **configs["poisson_bounded"],
            "research_groups": group,
        }
    windows = base["config"]["validation"]
    expected_windows = settings(base["config"]["smoke"])["validation"]
    if windows != expected_windows:
        raise ValueError("Frozen validation windows changed")
    assert_hashes(before)
    plan = {
        "version": VERSION,
        "code": recipe(),
        "schema": SCHEMA,
        "catalog": CATALOG,
        "populations": population,
        "expanded": expansion,
        "diagnostics": str(diagnostics),
        "diagnostics_manifest_sha256": checksum(diagnostics / "manifest.json"),
        "windows": windows,
        "seed": SEED,
        "threads": 2,
        "variants": variants,
        "primary_metric": "macro MAE, same validation folds per target/population",
        "hypotheses": HYPOTHESES,
        "decision_rule": DECISION_RULE,
        "group_effect_rule": GROUP_RULE,
        "ablation": "A+B vs A (remove B), A+B vs B (remove A), A/B vs base; same rows",
        "tuning": "none; feature groups at previous bounded configurations",
        "budgets": {
            "fit_seconds": 300,
            "inference_seconds": 300,
            "worker_wall_seconds": 330,
            "peak_rss_bytes": 2 * 1024**3,
            "cumulative_worker_seconds": 5400,
            "max_trials": len(population) * len(windows) * 2 * len(variants),
        },
        "critical_gates": "Majority wins/no >20% key-segment degradation n>=100; "
        "resources/no warnings; PIT/parity/oracle/row failure invalidates result",
        "critical_extra": ["peak_hour", "station:6753.08", "station:6230.02"],
        "evaluation_weather": outcome_weather(root, base),
        "superseded": superseded(root),
        "history": "approximate +45d only; B capped at min(origin,fit_as_of); "
        "NULL preserved; no weather/GBFS/station metadata/unpublished short lags",
        "uncertainty": {
            "method": "crossed station x NY target-day pigeonhole bootstrap; "
            "same weights across paired models; macro-fold estimand",
            "seed": SEED,
            "replicates": 500,
            "interval": 0.95,
        },
        "final_test_accessed": False,
        "test_policy": "September 2026 already consumed; not a new independent test; "
        "no test-driven selection/promotion; strict/forward evidence absent",
        "frozen_files": before,
        "environment": {
            p: importlib.metadata.version(p)
            for p in (
                "numpy",
                "pandas",
                "scikit-learn",
                "lightgbm",
                "catboost",
                "duckdb",
            )
        },
    }
    return create(
        root,
        "feature_protocols",
        identity([VERSION, plan]),
        lambda stage: {"kind": "feature_protocol", "plan": plan},
        activate=False,
    )


def activity_counts(dataset: Path) -> dict[str, int]:
    with duckdb.connect() as db:
        return {
            r[0]: r[1]
            for r in db.execute(
                "SELECT activity,count(*) FROM read_parquet(?) GROUP BY 1 ORDER BY 1",
                [str(dataset / "stations.parquet")],
            ).fetchall()
        }


def station_ids(dataset: Path) -> list[str]:
    with duckdb.connect() as db:
        return [
            r[0]
            for r in db.execute(
                "SELECT station_id FROM read_parquet(?) ORDER BY 1",
                [str(dataset / "stations.parquet")],
            ).fetchall()
        ]


def checked_protocol(path: Path) -> dict[str, Any]:
    report = checked(path)
    plan = report["plan"]
    if report["id"] != identity([VERSION, plan]) or plan["code"] != recipe():
        raise ValueError("Feature protocol/code mismatch; freeze before fitting")
    if plan["schema"] != SCHEMA or plan["final_test_accessed"] or plan["seed"] != SEED:
        raise ValueError("Invalid feature research protocol")
    assert_hashes(plan["frozen_files"])
    if plan["evaluation_weather"]:
        weather = plan["evaluation_weather"]
        assert_hashes({weather["daily_file"]: weather["sha256"]})
    for p in plan["populations"].values():
        assert_hashes(
            {
                str(Path(p["dataset"]) / "manifest.json"): p["dataset_manifest_sha256"],
                str(Path(p["inputs"]) / "manifest.json"): p["inputs_manifest_sha256"],
            }
        )
        data = checked(Path(p["dataset"]))
        checked(Path(p["inputs"]))
        if plan["windows"] != data["config"]["validation"] or any(
            last >= data["config"]["final"][0] for _, last in plan["windows"]
        ):
            raise ValueError("Test isolation / frozen folds failed")
    if plan["environment"] != {
        p: importlib.metadata.version(p) for p in plan["environment"]
    }:
        raise ValueError("Feature research environment changed")
    return report


def worker(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    before = time.monotonic()
    try:
        protocol = checked_protocol(Path(request["protocol_path"]))
        validate_request(request, protocol)
        assert_hashes(
            {
                request[field]: request[field + "_sha256"]
                for field in ("train_file", "evaluation_file")
            }
        )
        train, scoring = [
            read_frame(Path(request[field]))
            for field in ("train_file", "evaluation_file")
        ]
        train = train[train["prediction_type"] == request["prediction_type"]]
        scoring = scoring[scoring["prediction_type"] == request["prediction_type"]]
        for frame in (train, scoring):
            gate(frame, request["fit_asof"])
        if bool((train["label_available_at"] > utc(request["fit_asof"])).any()):
            raise ValueError("Training labels unavailable at fit time")
        if (
            train["target_hour_utc"].max() + timedelta(hours=1)
            >= scoring["target_hour_utc"].min()
        ):
            raise ValueError("Overlapping targets")
        if row_hash(scoring) != request["evaluation_rows_hash"]:
            raise ValueError("Same-row comparison failed")
        fitted, measure = feature_models.fit(train, request["parameters"])
        values, inference = feature_models.predict(fitted, scoring)
        legacy_models.save(stage / "model.joblib", fitted)
        restored = legacy_models.load(stage / "model.joblib")
        if feature_models.predict(restored, scoring)[0] != values:
            raise ValueError("Saved-model parity failed")
        # Outcome weather enters evaluation only after prediction.
        weather = request["evaluation_weather"]
        scoring = (
            attach(scoring, read_json(Path(weather["daily_file"])))
            if weather
            else scoring.copy()
        )
        scoring["predicted"] = values
        scoring["residual"] = scoring["predicted"] - scoring["actual"]
        save_frame(stage / "predictions.parquet", scoring)
        measure.update(
            {
                "inference_seconds": inference,
                "total_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            }
        )
        b = request["budgets"]
        ok = (
            measure["fit_seconds"] <= b["fit_seconds"]
            and inference <= b["inference_seconds"]
            and measure["peak_rss_bytes"] <= b["peak_rss_bytes"]
        )
        return {
            "status": "success" if ok else "failed",
            "error": None if ok else {"type": "ResourceBudgetExceeded"},
            "measurements": measure,
            "metrics": segmented(scoring, values, research=True),
            "diagnostics": error_analysis(scoring, values),
            "coverage": distribution(scoring),
            "feature_names": fitted["feature_names"],
            "parity_rows": len(values),
            "predictions": "predictions.parquet",
            "prediction_values_hash": identity(values),
            "evaluation_rows_hash": row_hash(scoring),
            "model_file": "model.joblib",
        }
    except Exception as error:
        return {
            "status": "failed",
            "error": {"type": type(error).__name__, "message": str(error)[:2000]},
            "measurements": {
                "total_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }


def trial(root: Path, request: dict[str, Any], *, isolated: bool = True) -> Path:
    def execute(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        write_json(stage / "request.json", request)
        if isolated:
            try:
                with (stage / "worker.log").open("w", encoding="utf-8") as log:
                    process = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "citibike.forecasting.feature_research",
                            "worker",
                            "--request",
                            str(stage / "request.json"),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                        timeout=request["budgets"]["worker_wall_seconds"],
                    )
                if process.returncode or not (stage / "outcome.json").exists():
                    raise RuntimeError(
                        f"Worker exit {process.returncode}; worker.log retained"
                    )
                result = read_json(stage / "outcome.json")
                (stage / "outcome.json").unlink()
            except (subprocess.TimeoutExpired, RuntimeError) as error:
                result = {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "measurements": {"peak_rss_bytes": None},
                }
        else:
            result = worker(request, stage)
        result["measurements"]["worker_wall_seconds"] = time.monotonic() - before
        return {
            "kind": "feature_trial",
            "request": request,
            "fold": request["fold"],
            "prediction_type": request["prediction_type"],
            "model_name": request["name"],
            "final_test_accessed": False,
            **result,
        }

    return create(
        root, "feature_trials", identity([VERSION, request]), execute, activate=False
    )


def validate_request(request: dict[str, Any], protocol: dict[str, Any]) -> None:
    """Reject substitutions before reading any scoring data."""
    plan = protocol["plan"]
    population = plan["populations"][request["population"]]
    path = Path(population["inputs"])
    split = checked(path)["splits"][request["fold"]]
    if (
        request["protocol_id"] != protocol["id"]
        or request["dataset_id"] != population["dataset_id"]
        or request["parameters"] != plan["variants"][request["name"]]
        or request["fit_asof"] != split["fit_asof"]
        or request["seed"] != plan["seed"]
        or request["budgets"] != plan["budgets"]
        or request["evaluation_weather"] != plan["evaluation_weather"]
        or request["prediction_type"] not in TYPES
    ):
        raise ValueError("Trial deviated from frozen protocol")
    for field in ("train_file", "evaluation_file"):
        if (
            Path(request[field]) != path / split[field]
            or request[field + "_sha256"] != split[field + "_sha256"]
        ):
            raise ValueError("Trial test isolation / input substitution rejected")
    if (
        request["evaluation_rows_hash"]
        != split["populations"][request["prediction_type"]]["evaluation_rows_hash"]
    ):
        raise ValueError("Trial population hash changed")


def run(root: Path, protocol_path: Path, *, isolated: bool = True) -> Path:
    report = checked_protocol(protocol_path)
    plan = report["plan"]
    results = []
    spent = 0.0
    for label, population in plan["populations"].items():
        path = Path(population["inputs"])
        for fold, split in enumerate(checked(path)["splits"]):
            for kind in TYPES:
                for name, config in plan["variants"].items():
                    request = {
                        "protocol_id": report["id"],
                        "protocol_path": str(protocol_path),
                        "population": label,
                        "dataset_id": population["dataset_id"],
                        "seed": SEED,
                        "fold": fold,
                        "prediction_type": kind,
                        "name": name,
                        "parameters": config,
                        "fit_asof": split["fit_asof"],
                        "budgets": plan["budgets"],
                        "evaluation_weather": plan["evaluation_weather"],
                        "evaluation_rows_hash": split["populations"][kind][
                            "evaluation_rows_hash"
                        ],
                        **{
                            field: str(path / split[field])
                            for field in ("train_file", "evaluation_file")
                        },
                        **{
                            field + "_sha256": split[field + "_sha256"]
                            for field in ("train_file", "evaluation_file")
                        },
                    }
                    if spent >= plan["budgets"]["cumulative_worker_seconds"]:
                        results.append(
                            {
                                "status": "not_run",
                                "request": request,
                                "reason": "Cumulative budget exhausted",
                            }
                        )
                        continue
                    outcome = trial(root, request, isolated=isolated)
                    result = checked(outcome)
                    spent += result["measurements"]["worker_wall_seconds"]
                    results.append(
                        {
                            "path": str(outcome),
                            "sha256": checksum(outcome / "manifest.json"),
                            "status": result["status"],
                        }
                    )
                    print(
                        label,
                        fold,
                        kind,
                        name,
                        result["status"],
                        result.get("metrics", {})
                        .get("all", {})
                        .get("mae", result.get("error")),
                        flush=True,
                    )
    checked_protocol(protocol_path)
    summary = {
        "kind": "feature_research",
        "protocol": str(protocol_path),
        "protocol_id": report["id"],
        "trials": results,
        "cumulative_worker_seconds": spent,
        "outcome_counts": {
            s: sum(r["status"] == s for r in results)
            for s in ("success", "failed", "not_run")
        },
        "final_test_accessed": False,
        "status": "completed"
        if all(r["status"] == "success" for r in results)
        and plan["expanded"]["status"] == "available"
        else "partial",
    }
    return create(
        root,
        "feature_outcomes",
        identity([VERSION, summary]),
        lambda stage: summary,
        activate=False,
    )


def main() -> None:
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["protocol", "run", "verify", "worker"])
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/f4-feature-research.json")
    )
    args = parser.parse_args()
    if args.command == "protocol":
        print(
            protocol(args.data_dir, args.dataset or active(args.data_dir, "datasets"))
        )
    elif args.command == "run":
        if args.protocol is None:
            parser.error("--protocol required; no final/test option exists")
        result = run(args.data_dir, args.protocol)
        print(result)
        if checked(result)["status"] != "completed":
            raise SystemExit(2)
    elif args.command == "worker":
        if args.request is None:
            parser.error("private --request required")
        write_json(
            args.request.parent / "outcome.json",
            worker(read_json(args.request), args.request.parent),
        )
    else:
        from citibike.forecasting.feature_research.verify import verify

        if args.result is None:
            parser.error("--result required")
        print(verify(args.result, args.output))


if __name__ == "__main__":
    main()
