"""Rolling validation, frozen selection, and a separately unlocked final test."""

import importlib.metadata
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.build import configured
from citibike.features.contracts import MODE, SCHEMA, SEED, TYPES, code_hash, utc
from citibike.forecasting import models
from citibike.forecasting.artifacts import checked, create
from citibike.forecasting.evaluation_weather import attach, labels
from citibike.forecasting.metrics import error_analysis, segmented
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json


def split_frames(
    db: duckdb.DuckDBPyConnection,
    path: Path,
    start: datetime,
    end: datetime,
    limit: int,
) -> tuple[Any, Any, dict[str, Any]]:
    db.read_parquet(str(path / "examples.parquet")).create_view("input", replace=True)
    # Sample whole station/origin blocks, never random time split. Labels must
    # have become available at fold fit time; a 24h embargo is also explicit.
    train = db.execute(
        """WITH blocks AS(SELECT station_id,forecast_origin_utc,
        sha256(station_id||forecast_origin_utc::VARCHAR||'42') rank_key
        FROM input WHERE eligibility='eligible' AND label_available_at<=?
        AND target_hour_utc<?-INTERVAL '24 hours'
        GROUP BY 1,2 HAVING count(*) FILTER(WHERE prediction_type='departures')=24
            AND count(*) FILTER(WHERE prediction_type='arrivals')=24
        ORDER BY rank_key LIMIT ?)
        SELECT i.* FROM input i JOIN blocks USING(station_id,forecast_origin_utc)
        WHERE eligibility='eligible' ORDER BY prediction_type,station_id,
            forecast_origin_utc,horizon_h""",
        [start, start, max(1, limit // 24)],
    ).df()
    evaluation = db.execute(
        """SELECT * FROM input WHERE eligibility='eligible'
        AND forecast_origin_utc>=? AND forecast_origin_utc<?
        ORDER BY prediction_type,station_id,forecast_origin_utc,horizon_h""",
        [start, end],
    ).df()
    if train.empty or evaluation.empty:
        raise ValueError("Empty train/evaluation population; no fabricated metrics")
    if (
        train["target_hour_utc"].max() + timedelta(hours=1)
        >= evaluation["target_hour_utc"].min()
    ):
        raise ValueError("Overlapping train/evaluation target intervals")
    for part in (train, evaluation):
        if bool(
            (
                (part["feature_available_at"] > part["forecast_origin_utc"])
                | (part["max_event_at"] >= part["forecast_origin_utc"])
            ).any()
        ):
            raise ValueError("Future training/evaluation feature detected")
    coverage = db.execute(
        """SELECT eligibility,count(*) FROM input
        WHERE forecast_origin_utc>=? AND forecast_origin_utc<? GROUP BY 1""",
        [start, end],
    ).fetchall()
    return (
        train,
        evaluation,
        {
            "fit_asof": start.isoformat(),
            "origin_start": start.isoformat(),
            "origin_end": end.isoformat(),
            "train_target_max": str(train["target_hour_utc"].max()),
            "evaluation_target_min": str(evaluation["target_hour_utc"].min()),
            "label_available_max": str(train["label_available_at"].max()),
            "train_rows": len(train),
            "evaluation_rows": len(evaluation),
            "eligibility": dict(coverage),
            "embargo_hours": 24,
            "sampling": "hash station/origin blocks",
            "availability_mode": MODE,
        },
    )


def evaluate(
    stage: Path,
    dataset: Path,
    config: dict[str, dict[str, Any]],
    windows: list[list[str]],
    max_rows: int,
    weather: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    results = []
    splits = []
    dataset_id = checked(dataset)["id"]
    recipe = code_hash(Path(__file__).parent)
    environment = {
        p: importlib.metadata.version(p)
        for p in ("numpy", "pandas", "scikit-learn", "lightgbm", "catboost", "duckdb")
    }
    with configured(stage.parent / "evaluation.duckdb") as db:
        for fold, (first, last) in enumerate(windows):
            start, end = utc(first + "T00:00:00+00:00"), utc(last + "T00:00:00+00:00")
            train, validation, split = split_frames(db, dataset, start, end, max_rows)
            splits.append(split)
            db.register("training_rows", train)
            training_file = f"{fold}-train.parquet"
            db.execute(
                "COPY training_rows TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
                [str(stage / training_file)],
            )
            db.unregister("training_rows")
            split["training_file"] = training_file
            for kind in TYPES:
                fitting = train[train["prediction_type"] == kind]
                scoring = validation[validation["prediction_type"] == kind]
                if fitting.empty or scoring.empty:
                    raise ValueError("Missing prediction-type evaluation")
                rows_hash = identity(
                    scoring[
                        [
                            "station_id",
                            "forecast_origin_utc",
                            "target_hour_utc",
                            "horizon_h",
                            "actual",
                        ]
                    ]
                    .astype(str)
                    .values.tolist()
                )
                for name, parameters in config.items():
                    before = time.perf_counter()
                    fitted, measurement = models.fit(fitting, parameters)
                    values, inference_seconds = models.predict(fitted, scoring)
                    # Outcome weather enters evaluation only after prediction.
                    scored = attach(scoring, weather or {})
                    model_id = identity(
                        [
                            dataset_id,
                            fold,
                            kind,
                            name,
                            parameters,
                            split["fit_asof"],
                            SCHEMA,
                            SEED,
                            recipe,
                            environment,
                        ]
                    )
                    model_file = stage / f"{fold}-{kind}-{name}.joblib"
                    models.save(model_file, fitted)
                    predictions = scored[
                        [
                            "station_id",
                            "forecast_origin_utc",
                            "target_hour_utc",
                            "horizon_h",
                            "prediction_type",
                            "actual",
                            "weekday",
                            "local_hour",
                            "month",
                            "holiday",
                            "activity",
                            "new_known",
                            "feature_available_at",
                            "max_event_at",
                            "eval_weather_cold",
                            "eval_weather_heat",
                            "eval_weather_heavy_rain",
                            "eval_weather_observed",
                        ]
                    ].copy()
                    predictions["predicted"] = values
                    predictions["residual"] = (
                        predictions["predicted"] - predictions["actual"]
                    )
                    predictions["model_name"] = name
                    db.register("predictions", predictions)
                    prediction_file = f"{fold}-{kind}-{name}.parquet"
                    db.execute(
                        "COPY predictions TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
                        [str(stage / prediction_file)],
                    )
                    db.unregister("predictions")
                    result: dict[str, Any] = {
                        "fold": fold,
                        "prediction_type": kind,
                        "model_name": name,
                        "model_id": model_id,
                        "parameters": parameters,
                        "predictions": prediction_file,
                        "model_file": model_file.name,
                        "evaluation_rows_hash": rows_hash,
                        "metrics": segmented(scored, values),
                        "measurements": {
                            **measurement,
                            "inference_seconds": inference_seconds,
                            "total_seconds": time.perf_counter() - before,
                        },
                    }
                    results.append(result)
                    print(
                        f"Fold {fold} {kind} {name}: MAE "
                        f"{result['metrics']['all']['mae']:.4f}",
                        flush=True,
                    )
    return {"results": results, "splits": splits}


def backtest(root: Path, dataset: Path) -> Path:
    data = checked(dataset)
    config = models.configurations(data["config"]["smoke"])
    recipe = code_hash(Path(__file__).parent)
    weather, weather_report = labels(root, data)
    environment = {
        p: importlib.metadata.version(p)
        for p in ("numpy", "pandas", "scikit-learn", "lightgbm", "catboost", "duckdb")
    }
    identifier = identity(
        ["validation", data["id"], config, recipe, environment, weather_report]
    )

    def run(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        evidence = evaluate(
            stage,
            dataset,
            config,
            data["config"]["validation"],
            data["config"]["max_train_rows"],
            weather,
        )
        return {
            "kind": "validation",
            "dataset_id": data["id"],
            "dataset_path": str(dataset),
            "feature_schema": SCHEMA,
            "availability_mode": MODE,
            "recipe": recipe,
            "seed": SEED,
            "evaluation_weather": weather_report,
            "configuration": config,
            **evidence,
            "final_test_accessed": False,
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
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(root, "experiments", identifier, run)


def promotion(
    candidate: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    *,
    strict_validation: bool,
) -> dict[str, Any]:
    by_fold = {r["fold"]: r for r in baseline}
    wins = sum(
        r["metrics"]["all"]["mae"] < by_fold[r["fold"]]["metrics"]["all"]["mae"]
        for r in candidate
    )
    degraded = []
    for r in candidate:
        b = by_fold[r["fold"]]["metrics"]
        for name, m in r["metrics"].items():
            if (
                not name.startswith(("activity:", "horizon:", "season:", "weather:"))
                and name != "new_known"
            ):
                continue
            if name in b and m["n"] >= 100 and m["mae"] > b[name]["mae"] * 1.2 + 1e-9:
                degraded.append(f"{r['fold']}:{name}")
    resources = all(
        r["measurements"]["fit_seconds"] <= 300
        and r["measurements"]["inference_seconds"] <= 300
        and r["measurements"]["peak_rss_bytes"] <= 2 * 1024**3
        and not r["measurements"]["warnings"]
        for r in candidate
    )
    return {
        "wins": wins,
        "windows": len(candidate),
        "majority": wins > len(candidate) / 2,
        "degraded_segments": degraded,
        "resources_and_fit_ok": resources,
        "strict_availability_evidence": strict_validation,
        "promote": wins > len(candidate) / 2
        and not degraded
        and resources
        and strict_validation,
        "reason": (
            "All gates required; retrospective scores do not prove operational uplift"
        ),
    }


def select(root: Path, experiment: Path) -> Path:
    report = checked(experiment)
    if report["final_test_accessed"] or report["kind"] != "validation":
        raise ValueError("Selection requires validation-only experiment")
    decisions = {}
    for kind in TYPES:
        rows = [r for r in report["results"] if r["prediction_type"] == kind]
        baseline = [r for r in rows if r["model_name"] == "seasonal_naive"]
        gates = {}
        means = {}
        best = {}
        for algorithm in ("poisson", "lightgbm", "catboost"):
            names = sorted(
                {
                    r["model_name"]
                    for r in rows
                    if r["parameters"]["algorithm"] == algorithm
                }
            )
            for name in names:
                candidates = [r for r in rows if r["model_name"] == name]
                means[name] = sum(r["metrics"]["all"]["mae"] for r in candidates) / len(
                    candidates
                )
                gates[name] = promotion(candidates, baseline, strict_validation=False)
            best[algorithm] = min(names, key=lambda n: (means[n], n))
        eligible = [n for n, g in gates.items() if g["promote"]]
        champion = (
            min(eligible, key=lambda n: (means[n], n)) if eligible else "seasonal_naive"
        )
        decisions[kind] = {
            "champion": champion,
            "best_retrospective_per_algorithm": best,
            "gates": gates,
            "validation_mean_mae": means,
            "decision_basis": (
                "validation only; unknown historical publication timeline"
            ),
            "operational_fallback": "seasonal_naive; unavailable if no valid profile",
        }
    identifier = identity([report["id"], decisions])
    return create(
        root,
        "selections",
        identifier,
        lambda stage: {
            "kind": "selection",
            "experiment_id": report["id"],
            "experiment_path": str(experiment),
            "dataset_path": report["dataset_path"],
            "dataset_id": report["dataset_id"],
            "feature_schema": SCHEMA,
            "availability_mode": MODE,
            "decisions": decisions,
            "final_test_accessed": False,
            "locked_before_final_test": True,
        },
    )


def final_test(root: Path, selection: Path) -> Path:
    chosen = checked(selection)
    dataset = Path(chosen["dataset_path"])
    data = checked(dataset)
    experiment = checked(Path(chosen["experiment_path"]))
    weather, weather_report = labels(root, data)
    if weather_report != experiment["evaluation_weather"]:
        raise ValueError(
            "Final evaluation weather differs from frozen validation provenance"
        )
    config = {
        n: experiment["configuration"][n]
        for n in [
            "seasonal_naive",
            *sorted(
                {
                    name
                    for decision in chosen["decisions"].values()
                    for name in decision["best_retrospective_per_algorithm"].values()
                }
            ),
        ]
    }
    identifier = identity(
        ["untouched-final", chosen["id"], data["id"], config, weather_report]
    )
    lock = (
        root
        / "f4/test_locks"
        / f"{identity([data['gold_publication_id'], data['config']['final']])}.json"
    )
    if lock.exists() and read_json(lock)["selection_id"] != chosen["id"]:
        raise ValueError("Final test already locked to another selection; no retuning")
    if not lock.exists():
        write_json(
            lock,
            {"selection_id": chosen["id"], "locked_at": datetime.now(UTC).isoformat()},
        )

    def run(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        # Selection immutable and committed before any final-score access.
        result = evaluate(
            stage,
            dataset,
            config,
            [data["config"]["final"]],
            data["config"]["max_train_rows"],
            weather,
        )
        write_json(stage / "selection-lock.json", chosen)
        return {
            "kind": "final_test",
            "selection_id": chosen["id"],
            "selection_created_at": chosen["created_at"],
            "dataset_id": data["id"],
            "dataset_path": str(dataset),
            "availability_mode": MODE,
            "selection_changed_by_test": False,
            "evaluation_weather": weather_report,
            **result,
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(root, "final_tests", identifier, run)


RESEARCH_VERSION = "f4-research-1"
ROW_KEY = [
    "station_id",
    "forecast_origin_utc",
    "target_hour_utc",
    "horizon_h",
    "actual",
]


def research_recipe() -> dict[str, str]:
    return {
        name: checksum(Path(__file__).with_name(name))
        for name in ("models.py", "experiment.py", "metrics.py", "artifacts.py")
    }


def research_protocol(root: Path, dataset: Path) -> Path:
    """Persist the bounded plan BEFORE any new fitting; never change selection."""
    data = checked(dataset)
    configurations = models.configurations(data["config"]["smoke"])
    for name in ("poisson_bounded", "lightgbm_bounded", "catboost_bounded"):
        for group in models.GROUPS:
            configurations[f"{name}__without_{group}"] = {
                **configurations[name],
                "drop_groups": [group],
            }
    frozen = {}
    for name in (
        "datasets",
        "experiments",
        "selections",
        "final_tests",
        "models",
        "forecasts",
        "evaluation_weather",
    ):
        pointer = root / "f4" / f"active_{name}.json"
        if pointer.exists():
            frozen[str(pointer)] = checksum(pointer)
            manifest = Path(read_json(pointer)["path"]) / "manifest.json"
            frozen[str(manifest)] = checksum(manifest)
    for lock in (root / "f4/test_locks").glob("*.json"):
        frozen[str(lock)] = checksum(lock)
    eda_path = Path("docs/evidence/f2-history.json")
    if data["data_mode"] == "real_official":
        eda = read_json(eda_path)
        if eda["snapshot_id"] != data["source_snapshot_id"]:
            raise ValueError("Accepted F2 EDA snapshot mismatch")
        eda_source = {
            "path": str(eda_path),
            "sha256": checksum(eda_path),
            "version": eda["research"]["version"],
        }
    else:
        eda_source = {
            "data_mode": "controlled_fixture",
            "thresholds": "same fixed bins; not real evidence",
        }
    evaluation_weather: dict[str, Any] = {}
    if data["data_mode"] == "real_official":
        weather_pointer = root / "f4/active_evaluation_weather.json"
        weather_path = Path(read_json(weather_pointer)["path"])
        weather = checked(weather_path)
        evaluation_weather = {
            "daily_file": str(weather_path / "daily.json"),
            "sha256": checksum(weather_path / "daily.json"),
            "policy": weather["policy"],
            "use": "Outcome diagnostics only AFTER prediction; not features",
        }
    windows = data["config"]["validation"]
    if any(
        utc(last + "T00:00:00+00:00")
        >= utc(data["config"]["final"][0] + "T00:00:00+00:00")
        for _, last in windows
    ):
        raise ValueError("Research windows overlap frozen final test")
    plan = {
        "version": RESEARCH_VERSION,
        "dataset_path": str(dataset),
        "dataset_id": data["id"],
        "dataset_manifest_sha256": checksum(dataset / "manifest.json"),
        "source_snapshot_id": data["source_snapshot_id"],
        "gold_publication_id": data["gold_publication_id"],
        "feature_schema": SCHEMA,
        "catalog_groups": models.GROUPS,
        "structural_features_retained": ["horizon_h"],
        "baseline_ablation": (
            "Not applicable: station/weekday/hour/profile define "
            "Seasonal Naive semantics"
        ),
        "ablation_limitation": (
            "Profiles encode station/calendar information indirectly; "
            "dropping explicit columns is not a causal removal of all group information"
        ),
        "windows": windows,
        "max_train_rows": data["config"]["max_train_rows"],
        "configurations": configurations,
        "seed": SEED,
        "threads": 2,
        "data_mode": data["data_mode"],
        "availability_mode": MODE,
        "f2_eda": eda_source,
        "evaluation_weather": evaluation_weather,
        "executor_sha256": research_recipe(),
        "frozen_files": frozen,
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
        "budgets": {
            "fit_seconds": 300,
            "worker_wall_seconds": 330,
            "peak_rss_bytes": 2 * 1024**3,
            "cumulative_worker_seconds": 5400,
            "max_trials": len(windows) * 2 * (len(configurations) + 1) + 2,
            "rss_enforcement": (
                "Measured child process high-water checked at completion; "
                "worker wall timeout is hard; OOM/crash retained as failure"
            ),
        },
        "stopping": (
            "Fixed default+one bounded config/algorithm; all declared single-group "
            "ablations; no adaptive parameter search/early stopping/final access. "
            "Budget exhaustion explicit PARTIAL; no silent omissions."
        ),
        "stress": {
            "eligible": ["poisson_bounded", "lightgbm_bounded", "catboost_bounded"],
            "selection": (
                "At most one per target: majority validation wins and resource gates, "
                "minimum macro MAE, lexical tie; no final scores"
            ),
            "training_fraction": 0.5,
            "evaluation": "same complete eligible rows/hashes, all validation folds",
        },
        "repeatability": (
            "Repeat fold0 lightgbm_bounded for both targets, independently refit; "
            "exact prediction-values hash"
        ),
        "promotion": (
            "Existing majority/no >20% key-segment degradation/resource gates; "
            "strict availability and forward/shadow UNKNOWN. "
            "No published/frozen selection mutation."
        ),
        "final_test_accessed": False,
    }
    identifier = identity([RESEARCH_VERSION, plan])
    return create(
        root,
        "research_protocols",
        identifier,
        lambda stage: {"kind": "research_protocol", "plan": plan},
        activate=False,
    )


def checked_protocol(path: Path) -> dict[str, Any]:
    protocol = checked(path)
    plan = protocol["plan"]
    if (
        protocol["id"] != identity([RESEARCH_VERSION, plan])
        or plan["executor_sha256"] != research_recipe()
    ):
        raise ValueError("Research protocol/code mismatch; freeze a new protocol")
    if (
        checksum(Path(plan["dataset_path"]) / "manifest.json")
        != plan["dataset_manifest_sha256"]
    ):
        raise ValueError("Research dataset manifest changed")
    data = checked(Path(plan["dataset_path"]))
    if (
        plan["windows"] != data["config"]["validation"]
        or plan["final_test_accessed"]
        or plan["seed"] != SEED
        or plan["feature_schema"] != SCHEMA
    ):
        raise ValueError("Research must use declared validation windows/schema/seed")
    if plan["environment"] != {
        p: importlib.metadata.version(p) for p in plan["environment"]
    }:
        raise ValueError("Research environment changed")
    for file, expected in plan["frozen_files"].items():
        if checksum(Path(file)) != expected:
            raise ValueError("Frozen final/selection/serving state changed")
    if (
        plan["f2_eda"].get("path")
        and checksum(Path(plan["f2_eda"]["path"])) != plan["f2_eda"]["sha256"]
    ):
        raise ValueError("Accepted F2 evidence changed")
    return protocol


def research_inputs(root: Path, protocol: dict[str, Any]) -> Path:
    plan = protocol["plan"]
    dataset = Path(plan["dataset_path"])
    identifier = identity(["research-inputs", protocol["id"]])

    def build_inputs(stage: Path) -> dict[str, Any]:
        splits = []
        with configured(stage.parent / "research-inputs.duckdb") as db:
            for fold, (first, last) in enumerate(plan["windows"]):
                train, validation, split = split_frames(
                    db,
                    dataset,
                    utc(first + "T00:00:00+00:00"),
                    utc(last + "T00:00:00+00:00"),
                    plan["max_train_rows"],
                )
                reduced, _, _ = split_frames(
                    db,
                    dataset,
                    utc(first + "T00:00:00+00:00"),
                    utc(last + "T00:00:00+00:00"),
                    max(24, len(train) // 4),
                )
                for name, frame in (
                    ("train", train),
                    ("validation", validation),
                    ("stress", reduced),
                ):
                    filename = f"{fold}-{name}.parquet"
                    db.register("frame", frame)
                    db.execute(
                        "COPY (SELECT * FROM frame ORDER BY prediction_type,station_id,"
                        "forecast_origin_utc,horizon_h) TO ? "
                        "(FORMAT PARQUET,COMPRESSION ZSTD)",
                        [str(stage / filename)],
                    )
                    db.unregister("frame")
                    split[name + "_file"] = filename
                split["populations"] = {
                    kind: {
                        "training_rows": int((train["prediction_type"] == kind).sum()),
                        "evaluation_rows": int(
                            (validation["prediction_type"] == kind).sum()
                        ),
                        "evaluation_rows_hash": identity(
                            validation.loc[
                                validation["prediction_type"] == kind, ROW_KEY
                            ]
                            .astype(str)
                            .values.tolist()
                        ),
                    }
                    for kind in TYPES
                }
                splits.append(split)
        return {
            "kind": "research_inputs",
            "protocol_id": protocol["id"],
            "dataset_id": plan["dataset_id"],
            "splits": splits,
            "final_test_accessed": False,
        }

    return create(root, "research_inputs", identifier, build_inputs, activate=False)


def trial_worker(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    """One fit, score and saved-model parity; called in a bounded subprocess."""
    before = time.monotonic()
    try:
        for field in ("train_file", "evaluation_file"):
            if checksum(Path(request[field])) != request[field + "_sha256"]:
                raise ValueError("Trial input checksum mismatch")
        with duckdb.connect() as db:
            db.execute("SET threads=2; SET TimeZone='UTC'; SET memory_limit='512MB'")
            train = db.execute(
                "SELECT * FROM read_parquet(?) WHERE prediction_type=? "
                "ORDER BY station_id,forecast_origin_utc,horizon_h",
                [request["train_file"], request["prediction_type"]],
            ).df()
            scoring = db.execute(
                "SELECT * FROM read_parquet(?) WHERE prediction_type=? "
                "ORDER BY station_id,forecast_origin_utc,horizon_h",
                [request["evaluation_file"], request["prediction_type"]],
            ).df()
        for frame in (train, scoring):
            if bool(
                (
                    (frame["feature_available_at"] > frame["forecast_origin_utc"])
                    | (frame["max_event_at"] >= frame["forecast_origin_utc"])
                ).any()
            ):
                raise ValueError("Future trial feature detected")
        if bool((train["label_available_at"] > utc(request["fit_asof"])).any()):
            raise ValueError("Unavailable training label")
        if (
            train["target_hour_utc"].max() + timedelta(hours=1)
            >= scoring["target_hour_utc"].min()
        ):
            raise ValueError("Trial target overlap")
        row_hash = identity(scoring[ROW_KEY].astype(str).values.tolist())
        if row_hash != request["evaluation_rows_hash"]:
            raise ValueError("Trial population changed")
        fitted, measurement = models.fit(train, request["parameters"])
        values, inference = models.predict(fitted, scoring)
        models.save(stage / "model.joblib", fitted)
        restored = models.load(stage / "model.joblib")
        parity, _ = models.predict(restored, scoring)
        if values != parity:
            raise ValueError("Train/serve saved-model parity mismatch")
        weather = request.get("evaluation_weather", {})
        if weather:
            weather_file = Path(weather["daily_file"])
            if checksum(weather_file) != weather["sha256"]:
                raise ValueError("Trial evaluation weather checksum mismatch")
            scoring = attach(scoring, read_json(weather_file))
        predictions = scoring[
            [c for c in scoring.columns if c not in models.GROUPS["demand_profile"]]
        ].copy()
        predictions["predicted"] = values
        predictions["residual"] = predictions["predicted"] - predictions["actual"]
        with duckdb.connect() as db:
            db.register("predictions", predictions)
            db.execute(
                "COPY predictions TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
                [str(stage / "predictions.parquet")],
            )
        measurement.update(
            {
                "inference_seconds": inference,
                "total_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            }
        )
        budget = request["budgets"]
        resource_ok = (
            measurement["fit_seconds"] <= budget["fit_seconds"]
            and inference <= 300
            and measurement["peak_rss_bytes"] <= budget["peak_rss_bytes"]
        )
        return {
            "status": "success" if resource_ok else "failed",
            "error": None
            if resource_ok
            else {
                "type": "ResourceBudgetExceeded",
                "message": "Measured trial fit/inference/RSS gate exceeded",
            },
            "metrics": segmented(scoring, values, research=True),
            "diagnostics": error_analysis(scoring, values),
            "measurements": measurement,
            "feature_names": fitted["feature_names"],
            "prediction_values_hash": identity(values),
            "parity_rows": len(values),
            "evaluation_rows_hash": row_hash,
            "predictions": "predictions.parquet",
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


def research_trial(
    root: Path, request: dict[str, Any], *, isolated: bool = True
) -> Path:
    """Publish success OR failure immediately; failed attempts survive run errors."""
    import subprocess
    import sys

    identifier = identity(["research-trial", request])

    def execute_trial(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        write_json(stage / "request.json", request)
        if isolated:
            try:
                with (stage / "worker.log").open("w", encoding="utf-8") as log:
                    outcome = subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "citibike.forecasting.experiment",
                            "--trial-request",
                            str(stage / "request.json"),
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=request["budgets"]["worker_wall_seconds"],
                        check=False,
                    )
                if outcome.returncode or not (stage / "worker-result.json").exists():
                    raise RuntimeError(
                        f"Worker exit code {outcome.returncode}; retained worker.log"
                    )
                result = read_json(stage / "worker-result.json")
                (stage / "worker-result.json").unlink()
            except (subprocess.TimeoutExpired, RuntimeError) as error:
                result = {
                    "status": "failed",
                    "error": {"type": type(error).__name__, "message": str(error)},
                    "measurements": {"peak_rss_bytes": None},
                }
        else:
            result = trial_worker(request, stage)
        result["measurements"]["worker_wall_seconds"] = time.monotonic() - before
        return {
            "kind": "research_trial",
            "request": request,
            "model_name": request["name"],
            "fold": request["fold"],
            "prediction_type": request["prediction_type"],
            "parameters": request["parameters"],
            "model_id": identifier,
            "final_test_accessed": False,
            **result,
        }

    return create(root, "trials", identifier, execute_trial, activate=False)


def research(root: Path, protocol_path: Path, *, isolated: bool = True) -> Path:
    protocol = checked_protocol(protocol_path)
    plan = protocol["plan"]
    inputs_path = research_inputs(root, protocol)
    inputs = checked(inputs_path)
    results: list[dict[str, Any]] = []
    spent = 0.0
    baseline_time = time.monotonic()

    def run(
        fold: int, kind: str, name: str, parameters: dict[str, Any], purpose: str
    ) -> None:
        nonlocal spent
        split = inputs["splits"][fold]
        train = (
            inputs_path / split["stress_file" if purpose == "stress" else "train_file"]
        )
        scoring = inputs_path / split["validation_file"]
        request = {
            "protocol_id": protocol["id"],
            "dataset_id": plan["dataset_id"],
            "inputs_id": inputs["id"],
            "seed": SEED,
            "environment": plan["environment"],
            "fold": fold,
            "prediction_type": kind,
            "name": name,
            "parameters": parameters,
            "purpose": purpose,
            "fit_asof": split["fit_asof"],
            "budgets": plan["budgets"],
            "evaluation_weather": plan["evaluation_weather"],
            "train_file": str(train),
            "train_file_sha256": checksum(train),
            "evaluation_file": str(scoring),
            "evaluation_file_sha256": checksum(scoring),
            "evaluation_rows_hash": split["populations"][kind]["evaluation_rows_hash"],
        }
        if spent >= plan["budgets"]["cumulative_worker_seconds"]:
            results.append(
                {
                    "status": "not_run",
                    "request": request,
                    "reason": "Cumulative worker budget exhausted",
                }
            )
            return
        path = research_trial(root, request, isolated=isolated)
        trial = checked(path)
        spent += trial["measurements"]["worker_wall_seconds"]
        results.append({"path": str(path), "id": trial["id"], **trial})
        print(
            f"Research fold{fold} {kind} {name}: {trial['status']} "
            + str(
                trial.get("metrics", {}).get("all", {}).get("mae", trial.get("error"))
            ),
            flush=True,
        )

    for fold in range(len(plan["windows"])):
        for kind in TYPES:
            for name, parameters in plan["configurations"].items():
                run(
                    fold,
                    kind,
                    name,
                    parameters,
                    "ablation" if "__without_" in name else "bounded_search",
                )
    stress_decisions = {}
    for kind in TYPES:
        base = [
            r
            for r in results
            if r.get("model_name") == "seasonal_naive"
            and r.get("prediction_type") == kind
            and r["status"] == "success"
        ]
        promising = []
        if len(base) == len(plan["windows"]):
            for name in plan["stress"]["eligible"]:
                rows = [
                    r
                    for r in results
                    if r.get("model_name") == name
                    and r.get("prediction_type") == kind
                    and r["status"] == "success"
                ]
                if len(rows) == len(base):
                    gate = promotion(rows, base, strict_validation=False)
                    if gate["majority"] and gate["resources_and_fit_ok"]:
                        promising.append(
                            (
                                sum(r["metrics"]["all"]["mae"] for r in rows)
                                / len(rows),
                                name,
                            )
                        )
        name = min(promising)[1] if promising else None
        stress_decisions[kind] = {
            "candidate": name,
            "basis": (
                "validation macro MAE, majority and resources only; "
                "existing final/selection not consulted"
            ),
        }
        if name:
            for fold in range(len(plan["windows"])):
                run(
                    fold,
                    kind,
                    name + "__stress_half_train",
                    plan["configurations"][name],
                    "stress",
                )
        run(
            0,
            kind,
            "lightgbm_bounded__repeat",
            plan["configurations"]["lightgbm_bounded"],
            "repeat",
        )
    checked_protocol(protocol_path)
    identifier = identity(
        ["research-summary", protocol["id"], [r.get("id", r) for r in results]]
    )
    summary = {
        "kind": "research",
        "protocol_id": protocol["id"],
        "protocol_path": str(protocol_path),
        "dataset_id": plan["dataset_id"],
        "inputs_path": str(inputs_path),
        "trials": [
            {k: r[k] for k in ("path", "id", "status") if k in r}
            | (
                {"request": r["request"], "reason": r["reason"]}
                if r["status"] == "not_run"
                else {}
            )
            for r in results
        ],
        "stress_decisions": stress_decisions,
        "final_test_accessed": False,
        "status": "completed"
        if all(r["status"] == "success" for r in results)
        else "partial",
        "outcome_counts": {
            s: sum(r["status"] == s for r in results)
            for s in ("success", "failed", "not_run")
        },
        "measurements": {
            "invocation_seconds": time.monotonic() - baseline_time,
            "cumulative_worker_seconds": spent,
            "parent_peak_rss_bytes": peak_rss_bytes(),
        },
    }
    return create(root, "research", identifier, lambda stage: summary)


if __name__ == "__main__":
    import argparse
    import os

    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "2"
    parser = argparse.ArgumentParser(description="Private bounded trial worker")
    parser.add_argument("--trial-request", type=Path, required=True)
    arguments = parser.parse_args()
    write_json(
        arguments.trial_request.parent / "worker-result.json",
        trial_worker(
            read_json(arguments.trial_request), arguments.trial_request.parent
        ),
    )
