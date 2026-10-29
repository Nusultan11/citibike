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
from citibike.forecasting.metrics import segmented
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
