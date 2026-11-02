"""Validation-only ML and decision research. Never activates any F4/F5 pointer."""

import argparse
import functools
import hashlib
import importlib.metadata
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

import duckdb

from citibike.decision_research import models
from citibike.decision_research.features import (
    CATALOG,
    GROUPS,
    RULES,
    SCHEMA,
    VERSION,
    enrich,
    gate,
    train_filter,
)
from citibike.features.contracts import SEED, TYPES, utc
from citibike.features.dataset import build
from citibike.forecasting.artifacts import active, checked, create
from citibike.forecasting.evaluation_weather import attach
from citibike.forecasting.experiment import split_frames
from citibike.forecasting.feature_research import (
    distribution,
    outcome_weather,
    row_hash,
    save_frame,
)
from citibike.forecasting.metrics import error_analysis, metrics, segmented
from citibike.forecasting.models import configurations
from citibike.forecasting.models import load as load_model
from citibike.forecasting.models import save as save_model
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json

EVIDENCE = Path("docs/evidence/f4-decision-research.json")
HOLDOUT = [
    ["2025-10-20", "2025-11-03"],
    ["2026-01-15", "2026-01-29"],
    ["2026-04-15", "2026-04-29"],
    ["2026-07-15", "2026-07-29"],
]
SMOKE_HOLDOUT = [["2025-04-15", "2025-04-17"], ["2025-05-15", "2025-05-17"]]
FEATURE_SETS = [
    [],
    ["A"],
    ["C"],
    ["P"],
    ["S"],
    ["A", "C", "P"],
    ["A", "C", "S"],
    ["A", "P", "S"],
    ["C", "P", "S"],
    ["A", "C", "P", "S"],
]
TIE = 0.0025
SPACE: dict[str, dict[str, list[Any]]] = {
    "lightgbm": {
        "n_estimators": [100, 200, 400, 800],
        "learning_rate": [0.02, 0.05, 0.1],
        "num_leaves": [7, 15, 31, 63],
        "min_child_samples": [20, 50, 100, 200],
        "subsample": [0.7, 1.0],
        "colsample_bytree": [0.7, 1.0],
        "reg_lambda": [0.0, 1.0, 10.0],
    },
    "catboost": {
        "iterations": [300, 500, 1000],
        "depth": [4, 6, 7],
        "learning_rate": [0.03, 0.06, 0.1],
        "l2_leaf_reg": [1.0, 3.0, 10.0],
    },
}
HYPOTHESES = {
    "H1_cyclic": {
        "claim": "Bounded annual harmonics C cannot run off the training range",
        "expect": "LightGBM+C is not worse than LightGBM+A on the Dec fold; "
        "C gives no material gain over base on holdout",
        "check": "S1 fold T2 delta, S4 holdout ablation",
    },
    "H2_profile": {
        "claim": "Variability and shape P describe low/high demand stations",
        "expect": "P lowers MAE on history:thin and band:0, not necessarily overall",
        "check": "paired P vs base inside the segments, S4 ablation",
    },
    "H3_shrinkage": {
        "claim": "Pooling S protects stations with few cell labels",
        "expect": "S lowers activity:new and history:thin MAE against base",
        "check": "S4 ablation and segment deltas",
    },
    "H4_season_level": {
        "claim": "Year-ago seasonality carries the level that delayed labels lose",
        "expect": "Once season_index exists (holdout folds), bias magnitude falls",
        "check": "holdout fold bias, season_index missing share",
    },
    "H5_hurdle": {
        "claim": "Separate occurrence and intensity suit zero-heavy hours",
        "expect": "Hurdle beats single-stage LightGBM on activity:low and "
        "actual:zero, not on overall macro MAE",
        "check": "S3 paired hurdle vs lightgbm_selected",
    },
    "H6_preprocess": {
        "claim": "Training exclusions of suspect rows barely change fits",
        "expect": "Below 0.25% macro MAE change",
        "check": "S1 __pre variants on identical evaluation rows",
    },
    "H7_tuning": {
        "claim": "Bounded search is not the main lever",
        "expect": "Below 2% holdout macro MAE gain against the bounded configuration",
        "check": "S3 selected vs bounded configuration with the same features",
    },
    "H8_year_over_year": {
        "claim": "With the same calendar month in train, segment degradations shrink",
        "expect": "H2/H3 folds (Apr, Jul 2026) have fewer gate degradations than H0/H1",
        "check": "degraded segments per holdout fold",
    },
}
DECISION_RULE = (
    "Per target, on the four holdout folds only: a variant is a research candidate "
    "if in cohort64 AND cohort128 it wins the majority of folds against Seasonal "
    "Naive, has no >20% key-segment degradation (n>=100, including peak_hour and "
    "stations 6753.08, 6230.02), valid resources and no warnings, and its family-wise "
    "paired macro MAE interval against Seasonal Naive is below zero; lowest cohort64 "
    "macro MAE among those, lexical tie; otherwise seasonal_naive. cohort256 is a "
    "sensitivity report and cannot create a candidate"
)
SELECTION_RULE = (
    "Stage selections use macro MAE on tuning folds only. Feature set per algorithm: "
    "mean of both targets; within 0.25% of the best the fewest columns wins, then "
    "lexical. Preprocess: adopted only if it improves by >0.25%. Parameters per "
    "target: lowest macro MAE; the bounded configuration is kept unless beaten "
    "by >0.25%. Any failed fold excludes the variant. No second search round"
)
STOPPING = (
    "Fixed trial list; no adaptive extension. Exhausted cumulative budget records "
    "remaining trials as not_run and yields partial. Each fit, inference, wall and "
    "RSS budget failure is retained as a failed trial"
)
KEEP = [
    "station_id",
    "forecast_origin_utc",
    "target_hour_utc",
    "horizon_h",
    "prediction_type",
    "actual",
    "predicted",
    "residual",
    "weekday",
    "local_hour",
    "month",
    "holiday",
    "activity",
    "new_known",
    "feature_available_at",
    "max_event_at",
    "history_depth",
    "hist_band",
    "anomalous_id",
    "suspect_closure",
]


def settings(smoke: bool) -> dict[str, Any]:
    return {
        "holdout": SMOKE_HOLDOUT if smoke else HOLDOUT,
        "search_trials": {"lightgbm": 2, "catboost": 1, "hurdle": 1}
        if smoke
        else {"lightgbm": 20, "catboost": 6, "hurdle": 8},
        "poisson_alpha": [0.1, 1.0] if smoke else [0.01, 0.03, 0.1, 0.3, 1.0],
        "seeds": [43] if smoke else [43, 44],
        "budgets": {
            "fit_seconds": 300,
            "inference_seconds": 300,
            "worker_wall_seconds": 420,
            "peak_rss_bytes": 2 * 1024**3,
            "cumulative_worker_seconds": 32400,
        },
    }


def search_space(smoke: bool) -> dict[str, list[dict[str, Any]]]:
    """Seeded enumeration, written into the protocol before any fit."""
    configs = settings(smoke)["search_trials"]
    rng = random.Random(SEED)
    result: dict[str, list[dict[str, Any]]] = {}
    for algorithm, count in configs.items():
        space = SPACE["lightgbm" if algorithm == "hurdle" else algorithm]
        chosen: list[dict[str, Any]] = []
        while len(chosen) < count:
            params = {k: rng.choice(v) for k, v in space.items()}
            if "subsample" in params and params["subsample"] < 1:
                params["subsample_freq"] = 1
            if smoke:
                for field in ("n_estimators", "iterations"):
                    if field in params:
                        params[field] = 10
            if params not in chosen:
                chosen.append(params)
        result[algorithm] = chosen
    return result


def label(groups: list[str]) -> str:
    return "".join(groups) or "base"


def config(
    algorithm: str,
    groups: list[str],
    params: dict[str, Any],
    *,
    preprocess: bool = False,
    seed: int = SEED,
) -> dict[str, Any]:
    return {
        "algorithm": algorithm,
        "groups": groups,
        "params": params,
        "preprocess": preprocess,
        "seed": seed,
    }


def bounded(smoke: bool) -> dict[str, dict[str, Any]]:
    legacy = configurations(smoke)
    strip = {
        name: {k: v for k, v in legacy[f"{name}_bounded"].items() if k != "algorithm"}
        for name in ("lightgbm", "catboost", "poisson")
    }
    return {**strip, "hurdle": strip["lightgbm"]}


def feature_variants(smoke: bool) -> dict[str, dict[str, Any]]:
    params = bounded(smoke)
    variants = {"seasonal_naive": config("seasonal_naive", [], {})}
    for algorithm in ("lightgbm", "catboost"):
        for groups in FEATURE_SETS:
            name = f"{algorithm}__{label(groups)}"
            variants[name] = config(algorithm, groups, params[algorithm])
        full = FEATURE_SETS[-1]
        variants[f"{algorithm}__{label(full)}__pre"] = config(
            algorithm, full, params[algorithm], preprocess=True
        )
    for algorithm in ("poisson", "hurdle"):
        for groups in ([], FEATURE_SETS[-1]):
            name = f"{algorithm}__{label(groups)}"
            variants[name] = config(algorithm, groups, params[algorithm])
    return variants


def read_frame(path: Path) -> Any:
    """Same order as the earlier study; a larger limit for the 4x train budget."""
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
        return db.execute(
            "SELECT * FROM read_parquet(?) ORDER BY "
            "prediction_type,station_id,forecast_origin_utc,horizon_h",
            [str(path)],
        ).df()


def code_hash(path: Path) -> str:
    """Line endings are normalised, so a clean checkout hashes identically."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def recipe() -> dict[str, str]:
    """Bind fit code; verify, analytics and readiness are hashed in evidence."""
    base = Path(__file__).parents[1]
    files = [
        *sorted(Path(__file__).parent.glob("*.py")),
        base / "features/contracts.py",
        base / "features/dataset.py",
        base / "features/research/__init__.py",
        base / "forecasting/artifacts.py",
        base / "forecasting/evaluation_weather.py",
        base / "forecasting/experiment.py",
        base / "forecasting/metrics.py",
        base / "forecasting/models.py",
    ]
    skipped = ("verify.py", "analytics.py", "readiness.py", "lineage.py")
    return {
        p.relative_to(base).as_posix(): code_hash(p)
        for p in files
        if p.parent != Path(__file__).parent or p.name not in skipped
    }


def protected(root: Path, gold: Path) -> dict[str, str]:
    """All prior F4/F5 files, published F2/F3 pointers and every Gold input file."""
    files = [
        p
        for folder in (root / "f4", root / "f5")
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    ]
    files += [
        p for p in (root / "f2/active.json", root / "f3/active.json") if p.exists()
    ]
    files.append(gold / "manifest.json")
    report = read_json(gold / "manifest.json")
    for month in report["months"]:
        files += sorted(p for p in Path(month["path"]).glob("*") if p.is_file())
    snapshot = Path(str(report.get("source_snapshot"))) / "manifest.json"
    if snapshot.exists():
        files.append(snapshot)
        parts = [Path(part["path"]) for part in read_json(snapshot)["partitions"]]
        files += [
            p / "silver.parquet" for p in parts if (p / "silver.parquet").exists()
        ]
    return {p.as_posix(): checksum(p) for p in files}


def assert_hashes(files: dict[str, str]) -> None:
    for file, expected in files.items():
        if checksum(Path(file)) != expected:
            raise ValueError(f"Protected input changed: {file}")


def folds(data: dict[str, Any], *, tuning: bool) -> list[dict[str, str]]:
    """Tuning folds are the four reused windows; holdout windows were never scored."""
    plan = settings(data["config"]["smoke"])
    rows = (
        [
            {"id": f"T{i}", "start": a, "end": b}
            for i, (a, b) in enumerate(data["config"]["validation"])
        ]
        if tuning
        else []
    )
    rows += [
        {"id": f"H{i}", "start": a, "end": b}
        for i, (a, b) in enumerate(plan["holdout"])
    ]
    final = data["config"]["final"][0]
    if any(f["end"] > final for f in rows):
        raise ValueError("Fold reaches the consumed final test window")
    return rows


def inputs(
    work: Path, dataset: Path, *, tuning: bool, train_multiplier: int = 1
) -> Path:
    data = checked(dataset)
    plan = folds(data, tuning=tuning)
    limit = data["config"]["max_train_rows"] * train_multiplier
    identifier = identity([VERSION, data["id"], SCHEMA, recipe(), plan, limit])

    def prepare(stage: Path) -> dict[str, Any]:
        splits = []
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
            for fold in plan:
                fit_asof = utc(fold["start"] + "T00:00:00+00:00")
                train, evaluation, split = split_frames(
                    db, dataset, fit_asof, utc(fold["end"] + "T00:00:00+00:00"), limit
                )
                train, limits = enrich(
                    train, dataset / "dense.parquet", fit_asof.isoformat()
                )
                evaluation, _ = enrich(
                    evaluation, dataset / "dense.parquet", fit_asof.isoformat(), limits
                )
                split["fold"] = fold["id"]
                split["band_thresholds"] = limits
                for name, frame in (("train", train), ("evaluation", evaluation)):
                    filename = f"{fold['id']}-{name}.parquet"
                    save_frame(stage / filename, frame)
                    split[name + "_file"] = filename
                    split[name + "_file_sha256"] = checksum(stage / filename)
                    split[name + "_missing_fraction"] = {
                        column: float(frame[column].isna().mean())
                        for column in [*GROUPS["P"], *GROUPS["S"]]
                    }
                    split[name + "_flagged_rows"] = {
                        flag: int(frame[flag].sum())
                        for flag in ("anomalous_id", "suspect_closure")
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
            "kind": "decision_inputs",
            "dataset_id": data["id"],
            "splits": splits,
            "schema": SCHEMA,
            "recipe": recipe(),
            "max_train_rows": limit,
            "final_test_accessed": False,
        }

    return create(work, "inputs", identifier, prepare, activate=False)


def trials_for(
    stage: str,
    population: str,
    fold_ids: list[str],
    variants: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "stage": stage,
            "population": population,
            "fold": fold,
            "prediction_type": kind,
            "name": name,
            "config": variant,
        }
        for fold in fold_ids
        for kind in TYPES
        for name, variant in variants.items()
    ]


def macro(results: list[dict[str, Any]], name: str, kind: str) -> float | None:
    """A variant with any non-success fold has no score."""
    rows = [r for r in results if r["name"] == name and r["prediction_type"] == kind]
    if not rows or any(r["status"] != "success" for r in rows):
        return None
    return float(mean(r["mae"] for r in rows))


def both(results: list[dict[str, Any]], name: str) -> float | None:
    values = [macro(results, name, kind) for kind in TYPES]
    return None if None in values else mean(v for v in values if v is not None)


def select_features(
    results: list[dict[str, Any]], variants: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    selection: dict[str, dict[str, Any]] = {}
    for algorithm in ("lightgbm", "catboost", "hurdle", "poisson"):
        scored = []
        for name, variant in variants.items():
            value = both(results, name)
            if (
                variant["algorithm"] == algorithm
                and not variant["preprocess"]
                and value is not None
            ):
                columns = len(models.names(variant["groups"]))
                scored.append((value, columns, label(variant["groups"]), variant))
        if not scored:
            continue
        best = min(item[0] for item in scored)
        near = [item for item in scored if item[0] <= best * (1 + TIE)]
        chosen = min(near, key=lambda item: (item[1], item[2]))
        selection[algorithm] = {
            "groups": chosen[3]["groups"],
            "score": chosen[0],
            "best_score": best,
            "preprocess": False,
        }
        full = label(FEATURE_SETS[-1])
        plain = both(results, f"{algorithm}__{full}")
        filtered = both(results, f"{algorithm}__{full}__pre")
        if plain and filtered and filtered < plain * (1 - TIE):
            selection[algorithm]["preprocess"] = True
    return selection


def search_variants(
    selection: dict[str, dict[str, Any]], smoke: bool
) -> dict[str, dict[str, Any]]:
    variants: dict[str, dict[str, Any]] = {}
    params = bounded(smoke)
    space = search_space(smoke)
    for algorithm, chosen in selection.items():
        groups, preprocess = chosen["groups"], chosen["preprocess"]
        candidates = (
            [
                {**params["poisson"], "alpha": a}
                for a in settings(smoke)["poisson_alpha"]
            ]
            if algorithm == "poisson"
            else space[algorithm]
        )
        variants[f"{algorithm}__bounded"] = config(
            algorithm, groups, params[algorithm], preprocess=preprocess
        )
        for number, values in enumerate(candidates):
            variants[f"{algorithm}__search{number:02d}"] = config(
                algorithm, groups, values, preprocess=preprocess
            )
    return variants


def select_params(
    results: list[dict[str, Any]], variants: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    selection: dict[str, dict[str, Any]] = {}
    for kind in TYPES:
        for algorithm in sorted({v["algorithm"] for v in variants.values()}):
            scored = sorted(
                (value, name)
                for name, variant in variants.items()
                if variant["algorithm"] == algorithm
                and (value := macro(results, name, kind)) is not None
            )
            if not scored:
                continue
            reference = macro(results, f"{algorithm}__bounded", kind)
            name = scored[0][1]
            if reference is not None and scored[0][0] >= reference * (1 - TIE):
                name = f"{algorithm}__bounded"
            selection[f"{algorithm}:{kind}"] = {
                "name": name,
                "score": macro(results, name, kind),
                "reference": reference,
                "config": variants[name],
            }
    return selection


def holdout_variants(
    selected: dict[str, dict[str, Any]], kind: str, smoke: bool
) -> dict[str, dict[str, Any]]:
    params = bounded(smoke)
    variants = {
        "seasonal_naive": config("seasonal_naive", [], {}),
        "lightgbm_reference": config("lightgbm", [], params["lightgbm"]),
        "catboost_reference": config("catboost", [], params["catboost"]),
        "catboost_reference_A": config("catboost", ["A"], params["catboost"]),
    }
    for algorithm in ("poisson", "lightgbm", "catboost", "hurdle"):
        key = f"{algorithm}:{kind}"
        if key in selected:
            variants[f"{algorithm}_selected"] = selected[key]["config"]
    return variants


def ablation_variants(
    selected: dict[str, dict[str, Any]], kind: str
) -> dict[str, dict[str, Any]]:
    variants = {}
    for algorithm in ("lightgbm", "catboost"):
        key = f"{algorithm}:{kind}"
        if key not in selected:
            continue
        chosen = selected[key]["config"]
        groups = chosen["groups"]
        sets = [[g for g in groups if g != item] for item in groups]
        sets += [[], FEATURE_SETS[-1]]
        for option in sets:
            if option != groups:
                variants[f"{algorithm}_selected__{label(option)}"] = {
                    **chosen,
                    "groups": option,
                }
    return variants


def seed_variants(
    selected: dict[str, dict[str, Any]], kind: str, seeds: list[int]
) -> dict[str, dict[str, Any]]:
    return {
        f"{algorithm}_selected__seed{seed}": {**selected[key]["config"], "seed": seed}
        for algorithm in ("lightgbm", "catboost", "hurdle")
        for seed in seeds
        if (key := f"{algorithm}:{kind}") in selected
    }


def provenance(item: dict[str, Any]) -> str:
    """Disclose what each trial can and cannot count as."""
    if item["fold"].startswith("H"):
        return "new_fit_on_never_scored_window"
    variant = item["config"]
    if variant["algorithm"] == "seasonal_naive":
        return "repeat_of_F4_baseline"
    if item["population"] in ("cohort64", "cohort128") and variant["groups"] in (
        [],
        ["A"],
    ):
        return "repeat_of_prior_study_on_reused_window"
    return "new_fit_on_reused_window"


def populations_of(root: Path, dataset: Path, *, smoke: bool) -> dict[str, Path]:
    """cohort128 is reused read-only; cohort256 is built in the research root."""
    result = {"cohort64": dataset}
    if smoke:
        return result
    base = checked(dataset)
    result["cohort128"] = active(root / "f4/feature_expansion", "datasets")
    result["cohort256"] = build(
        research_root(root),
        Path(base["gold_path"]),
        station_count=256,
        smoke=False,
    )
    return result


def research_root(root: Path) -> Path:
    return root / "decision_research"


def freeze(
    root: Path,
    dataset: Path,
    readiness: Path | None,
    *,
    populations: dict[str, Path] | None = None,
    corrects: Path | None = None,
) -> Path:
    work = research_root(root)
    base = checked(dataset)
    smoke = base["config"]["smoke"]
    before = protected(root, Path(base["gold_path"]))
    correction = correction_of(corrects) if corrects else None
    if correction:
        before.update(correction.pop("files"))
    cohorts = populations or populations_of(root, dataset, smoke=smoke)
    population: dict[str, Any] = {}
    for name, path in cohorts.items():
        data = checked(path)
        prepared = inputs(work, path, tuning=name == "cohort64")
        population[name] = {
            "dataset": str(path),
            "dataset_manifest_sha256": checksum(path / "manifest.json"),
            "dataset_id": data["id"],
            "inputs": str(prepared),
            "inputs_manifest_sha256": checksum(prepared / "manifest.json"),
            "coverage": data["coverage"],
            "eligibility": data["eligibility"],
        }
    if "cohort256" in cohorts:
        path = cohorts["cohort256"]
        prepared = inputs(work, path, tuning=False, train_multiplier=4)
        population["cohort256x4"] = {
            **population["cohort256"],
            "inputs": str(prepared),
            "inputs_manifest_sha256": checksum(prepared / "manifest.json"),
        }
    assert_hashes(before)
    final = base["config"]["final"]
    plan = {
        "version": VERSION,
        "data_mode": base["data_mode"],
        "smoke": smoke,
        "code": recipe(),
        "schema": SCHEMA,
        "catalog": CATALOG,
        "preprocessing_rules": RULES,
        "feature_sets": FEATURE_SETS,
        "populations": population,
        "corrects": correction,
        "tuning_folds": folds(base, tuning=True)[: len(base["config"]["validation"])],
        "holdout_folds": folds(base, tuning=False),
        "final_window_not_used": final,
        "seed": SEED,
        "extra_seeds": settings(smoke)["seeds"],
        "threads_per_worker": 2,
        "feature_variants": feature_variants(smoke),
        "search_space": SPACE,
        "search_trials": search_space(smoke),
        "poisson_alpha": settings(smoke)["poisson_alpha"],
        "budgets": settings(smoke)["budgets"],
        "trial_upper_bound": 0,
        "stopping": STOPPING,
        "selection_rule": SELECTION_RULE,
        "tie_tolerance": TIE,
        "decision_rule": DECISION_RULE,
        "critical_extra": ["peak_hour", "station:6753.08", "station:6230.02"],
        "primary_metric": "macro MAE over the same folds per target and population",
        "hypotheses": HYPOTHESES,
        "readiness": str(readiness) if readiness else None,
        "readiness_manifest_sha256": checksum(readiness / "manifest.json")
        if readiness
        else None,
        "evaluation_weather": outcome_weather(root, base),
        "uncertainty": {
            "method": "crossed station x NY target-day pigeonhole bootstrap, paired "
            "weights, macro-fold estimand",
            "seed": SEED,
            "replicates_primary": 4000,
            "replicates_secondary": 1000,
            "familywise": "Bonferroni over candidate x target x cohort64/128 against "
            "Seasonal Naive, plus Holm on bootstrap sign p-values",
            "fold_level": "exact sign-flip test on fold differences; 4 folds cannot "
            "reach p<0.125, reported only",
        },
        "provenance": "Tuning folds T0-T3 were scored by F4 and both later studies; "
        "their trials are not new evidence. Holdout folds H0-H3 were never scored "
        "by any model in this repository, but they lie inside the F2 EDA period and "
        "inside the training data of the frozen final models, and the hypotheses "
        "were written after reading prior results. They are unseen validation "
        "windows, not an independent test.",
        "test_policy": "September 2026 already consumed, unused here. No new "
        "independent period exists; no promotion, no operational claim",
        "final_test_accessed": False,
        "protected_files": before,
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
    plan["trial_upper_bound"] = trial_bound(plan)
    return create(
        work,
        "protocols",
        identity([VERSION, plan]),
        lambda stage: {"kind": "decision_protocol", "plan": plan},
        activate=False,
    )


def correction_of(outcome_path: Path) -> dict[str, Any]:
    """The defective run stays immutable; only its D1-free trials may be replayed."""
    outcome = checked(outcome_path)
    protocol_path = Path(outcome["protocol"])
    files = {
        str(protocol_path / "manifest.json"): checksum(protocol_path / "manifest.json"),
        str(outcome_path / "manifest.json"): checksum(outcome_path / "manifest.json"),
    }
    for rows in outcome["stages"].values():
        for row in rows:
            if row.get("path"):
                files[str(Path(row["path"]) / "manifest.json")] = row["sha256"]
    return {
        "defect": "D1: cell_std was 0 instead of NULL without cell history; only "
        "feature group P is affected",
        "protocol": str(protocol_path),
        "protocol_id": outcome["protocol_id"],
        "outcome": str(outcome_path),
        "outcome_id": outcome["id"],
        "replay_rule": "A successful old trial whose features exclude P is replayed: "
        "its saved model predicts the corrected evaluation rows and the prediction "
        "hash must equal the old one. Every trial with P, and every request without "
        "an old match, is fitted again",
        "files": files,
    }


def replay_index(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Old successful trials without group P, keyed like Runner.many."""
    index: dict[str, dict[str, Any]] = {}
    if not plan.get("corrects"):
        return index
    outcome = checked(Path(plan["corrects"]["outcome"]))
    for rows in outcome["stages"].values():
        for row in rows:
            if row["status"] != "success":
                continue
            path = Path(row["path"])
            old = checked(path)
            request = old["request"]
            if "P" in request["config"]["groups"]:
                continue
            index[trial_key(request)] = {
                "trial": str(path),
                "manifest_sha256": row["sha256"],
                "prediction_values_hash": old["prediction_values_hash"],
                "evaluation_rows_hash": old["evaluation_rows_hash"],
            }
    return index


def trial_key(item: dict[str, Any]) -> str:
    return identity(
        [
            item["population"],
            item["fold"],
            item["prediction_type"],
            item["config"],
            item.get("robustness", False),
        ]
    )


def checked_protocol(path: Path, *, full: bool = True) -> dict[str, Any]:
    report = checked(path)
    plan = report["plan"]
    if report["id"] != identity([VERSION, plan]) or plan["code"] != recipe():
        raise ValueError("Decision protocol/code mismatch; freeze before fitting")
    if plan["schema"] != SCHEMA or plan["final_test_accessed"] or plan["seed"] != SEED:
        raise ValueError("Invalid decision research protocol")
    final = plan["final_window_not_used"][0]
    if any(f["end"] > final for f in plan["tuning_folds"] + plan["holdout_folds"]):
        raise ValueError("Test lock: fold reaches the consumed final window")
    if full:
        assert_hashes(plan["protected_files"])
        for p in plan["populations"].values():
            assert_hashes(
                {
                    str(Path(p["dataset"]) / "manifest.json"): p[
                        "dataset_manifest_sha256"
                    ],
                    str(Path(p["inputs"]) / "manifest.json"): p[
                        "inputs_manifest_sha256"
                    ],
                }
            )
            checked(Path(p["dataset"]))
            checked(Path(p["inputs"]))
        if plan["environment"] != {
            p: importlib.metadata.version(p) for p in plan["environment"]
        }:
            raise ValueError("Decision research environment changed")
    return report


def remote_proof(protocol_id: str) -> dict[str, Any]:
    """The protocol id must already be public at the exact local HEAD."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    head = git("rev-parse", "HEAD")
    remote = git("ls-remote", "origin", "refs/heads/main").split()[0]
    if head != remote:
        raise ValueError("Protocol commit is not on origin/main; run refused")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Tracked changes after the protocol commit; run refused")
    if protocol_id not in git("show", f"HEAD:{EVIDENCE.as_posix()}"):
        raise ValueError("Protocol id is absent from committed evidence; run refused")
    return {
        "head": head,
        "remote_main": remote,
        "committed_at": git("show", "-s", "--format=%cI", "HEAD"),
        "checked_at": datetime.now(UTC).isoformat(),
    }


@functools.cache
def splits_of(path: Path) -> list[dict[str, Any]]:
    splits: list[dict[str, Any]] = checked(path)["splits"]
    return splits


def request_for(
    item: dict[str, Any], report: dict[str, Any], path: Path
) -> dict[str, Any]:
    plan = report["plan"]
    population = plan["populations"][item["population"]]
    inputs_path = Path(population["inputs"])
    split = next(s for s in splits_of(inputs_path) if s["fold"] == item["fold"])
    return {
        "protocol_id": report["id"],
        "protocol_path": str(path),
        "population": item["population"],
        "dataset_id": population["dataset_id"],
        "fold": item["fold"],
        "prediction_type": item["prediction_type"],
        "config": item["config"],
        "fit_asof": split["fit_asof"],
        "budgets": plan["budgets"],
        "evaluation_weather": plan["evaluation_weather"],
        "evaluation_rows_hash": split["populations"][item["prediction_type"]][
            "evaluation_rows_hash"
        ],
        "robustness": item.get("robustness", False),
        "replay": item.get("replay"),
        **{f: str(inputs_path / split[f]) for f in ("train_file", "evaluation_file")},
        **{
            f + "_sha256": split[f + "_sha256"]
            for f in ("train_file", "evaluation_file")
        },
    }


def validate_request(request: dict[str, Any], protocol: dict[str, Any]) -> None:
    """Reject substitutions before reading any scoring data."""
    plan = protocol["plan"]
    population = plan["populations"][request["population"]]
    path = Path(population["inputs"])
    split = next(s for s in checked(path)["splits"] if s["fold"] == request["fold"])
    variant = request["config"]
    if (
        request["protocol_id"] != protocol["id"]
        or request["dataset_id"] != population["dataset_id"]
        or request["fit_asof"] != split["fit_asof"]
        or request["budgets"] != plan["budgets"]
        or request["evaluation_weather"] != plan["evaluation_weather"]
        or request["prediction_type"] not in TYPES
        or variant["algorithm"] not in models.ALGORITHMS
        or variant["seed"] not in (plan["seed"], *plan["extra_seeds"])
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


def extra_metrics(scoring: Any, values: list[float], train: Any) -> dict[str, Any]:
    """Station history, band and flag segments on the identical evaluation rows."""
    masks = {
        "history:" + depth: scoring["history_depth"] == depth
        for depth in ("none", "thin", "deep")
    }
    for band in range(4):
        masks[f"band:{band}"] = scoring["hist_band"] == band
    masks["band:missing"] = scoring["hist_band"].isna()
    masks["flag:anomalous_id"] = scoring["anomalous_id"]
    masks["flag:suspect_closure"] = scoring["suspect_closure"]
    masks["flag:clean"] = ~(scoring["anomalous_id"] | scoring["suspect_closure"])
    masks["unseen_station"] = ~scoring["station_id"].isin(train["station_id"])
    series = scoring["actual"].astype(float)
    return {
        name: metrics(
            series[mask].tolist(),
            [v for v, m in zip(values, mask.tolist(), strict=True) if m],
        )
        for name, mask in masks.items()
        if bool(mask.any())
    }


def robustness(fitted: dict[str, Any], scoring: Any, values: list[float]) -> Any:
    """History columns vanish for a hashed fifth of stations; no refit."""
    columns = [c for c in fitted["feature_names"] if c in {*GROUPS["P"], *GROUPS["S"]}]
    if not columns:
        return None
    stations = sorted(scoring["station_id"].unique().tolist())
    chosen = {
        s
        for s in stations
        if int(hashlib.sha256(f"{s}42".encode()).hexdigest(), 16) % 5 == 0
    }
    frame = scoring.copy()
    mask = frame["station_id"].isin(chosen)
    if not bool(mask.any()):
        return None
    frame.loc[mask, columns] = float("nan")
    changed, _ = models.predict(fitted, frame)
    actual = scoring["actual"].astype(float)
    selected = mask.tolist()

    def mae(series: list[float]) -> float:
        rows: list[float] = [
            abs(p - a)
            for p, a, s in zip(series, actual.tolist(), selected, strict=True)
            if s
        ]
        return sum(rows) / len(rows)

    return {
        "stations_blanked": len(chosen),
        "rows": int(mask.sum()),
        "columns": columns,
        "mae_intact": mae(values),
        "mae_history_blanked": mae(changed),
    }


def worker(request: dict[str, Any], stage: Path) -> dict[str, Any]:
    before = time.monotonic()
    try:
        protocol = checked_protocol(Path(request["protocol_path"]), full=False)
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
        kind = request["prediction_type"]
        train = train[train["prediction_type"] == kind]
        scoring = scoring[scoring["prediction_type"] == kind]
        for frame in (train, scoring):
            gate(frame, request["fit_asof"])
        if bool((train["label_available_at"] > utc(request["fit_asof"])).any()):
            raise ValueError("Training labels unavailable at fit time")
        if (
            train["target_hour_utc"].max() + timedelta(hours=1)
            >= scoring["target_hour_utc"].min()
        ):
            raise ValueError("Overlapping targets")
        final = protocol["plan"]["final_window_not_used"][0] + "T00:00:00+00:00"
        if bool((scoring["target_hour_utc"] >= utc(final)).any()):
            raise ValueError("Test lock: evaluation row inside the final window")
        if row_hash(scoring) != request["evaluation_rows_hash"]:
            raise ValueError("Same-row comparison failed")
        variant = request["config"]
        kept = train_filter(train, preprocess=variant["preprocess"])
        if request.get("replay"):
            fitted, measure = replayed(request)
        else:
            fitted, measure = models.fit(kept, variant)
        measure["train_rows_excluded"] = len(train) - len(kept)
        values, inference = models.predict(fitted, scoring)
        if request.get("replay") and (
            identity(values) != request["replay"]["prediction_values_hash"]
        ):
            raise ValueError("Replay changed predictions; the trial must be refitted")
        save_model(stage / "model.joblib", fitted)
        restored = load_model(stage / "model.joblib")
        if models.predict(restored, scoring)[0] != values:
            raise ValueError("Saved-model parity failed")
        # Outcome weather enters evaluation only after prediction.
        weather = request["evaluation_weather"]
        evaluated = (
            attach(scoring, read_json(Path(weather["daily_file"])))
            if weather
            else scoring.copy()
        )
        evaluated["predicted"] = values
        evaluated["residual"] = evaluated["predicted"] - evaluated["actual"]
        columns = [c for c in evaluated if c in KEEP or c.startswith("eval_weather_")]
        save_frame(stage / "predictions.parquet", evaluated[columns])
        measure.update(
            {
                "inference_seconds": inference,
                "total_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            }
        )
        budget = request["budgets"]
        ok = (
            measure["fit_seconds"] <= budget["fit_seconds"]
            and inference <= budget["inference_seconds"]
            and measure["peak_rss_bytes"] <= budget["peak_rss_bytes"]
        )
        return {
            "status": "success" if ok else "failed",
            "error": None if ok else {"type": "ResourceBudgetExceeded"},
            "measurements": measure,
            "metrics": segmented(evaluated, values, research=True),
            "extra_metrics": extra_metrics(evaluated, values, kept),
            "diagnostics": error_analysis(evaluated, values),
            "coverage": distribution(evaluated),
            "feature_names": fitted["feature_names"],
            "occurrence_calibration": models.occurrence_calibration(fitted, scoring),
            "robustness": robustness(fitted, scoring, values)
            if request["robustness"]
            else None,
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


def replayed(request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load the old model instead of fitting; its resource record is kept."""
    replay = request["replay"]
    path = Path(replay["trial"])
    assert_hashes({str(path / "manifest.json"): replay["manifest_sha256"]})
    old = checked(path)
    if (
        trial_key(old["request"]) != trial_key(request)
        or old["evaluation_rows_hash"] != request["evaluation_rows_hash"]
        or "P" in request["config"]["groups"]
    ):
        raise ValueError("Replay source does not match this trial")
    fitted = load_model(path / old["model_file"])
    if not models.readable(fitted):
        raise ValueError("Replay model is not readable on corrected inputs")
    measure = {
        **{k: v for k, v in old["measurements"].items() if k != "worker_wall_seconds"},
        "replayed_from": str(path),
        "replayed_fit_seconds": old["measurements"]["fit_seconds"],
    }
    return fitted, measure


def trial(work: Path, request: dict[str, Any], *, isolated: bool = True) -> Path:
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
                            "citibike.decision_research",
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
            "kind": "decision_trial",
            "request": request,
            "final_test_accessed": False,
            **result,
        }

    return create(work, "trials", identity([VERSION, request]), execute, activate=False)


class Runner:
    """Runs one fixed list at a time; budget exhaustion is recorded, not hidden."""

    def __init__(
        self,
        work: Path,
        report: dict[str, Any],
        path: Path,
        *,
        jobs: int,
        isolated: bool,
    ) -> None:
        self.work, self.report, self.path = work, report, path
        self.jobs, self.isolated = jobs, isolated
        self.spent = 0.0
        self.counted: set[Path] = set()
        self.replays = replay_index(report["plan"])

    def one(self, item: dict[str, Any]) -> dict[str, Any]:
        item = {**item, "replay": self.replays.get(trial_key(item))}
        request = request_for(item, self.report, self.path)
        row = {k: v for k, v in item.items() if k not in ("config", "replay")}
        row["provenance"] = provenance(item)
        row["replayed"] = item["replay"] is not None
        budget = request["budgets"]["cumulative_worker_seconds"]
        if self.spent >= budget:
            return {**row, "status": "not_run", "reason": "Cumulative budget exhausted"}
        outcome = trial(self.work, request, isolated=self.isolated)
        result = checked(outcome)
        if outcome not in self.counted:
            self.counted.add(outcome)
            self.spent += result["measurements"]["worker_wall_seconds"]
        summary = {
            **row,
            "path": str(outcome),
            "sha256": checksum(outcome / "manifest.json"),
            "status": result["status"],
            "mae": result["metrics"]["all"]["mae"]
            if result["status"] == "success"
            else None,
        }
        print(
            item["stage"],
            item["population"],
            item["fold"],
            item["prediction_type"],
            item["name"],
            summary["status"],
            round(self.spent),
            flush=True,
        )
        return summary

    def many(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Same request twice in a list runs once, so parallel writers cannot race."""
        keys = [trial_key(i) for i in items]
        unique = dict(zip(keys, items, strict=True))
        with ThreadPoolExecutor(self.jobs) as pool:
            done = dict(zip(unique, pool.map(self.one, unique.values()), strict=True))
        return [
            {**done[key], "stage": item["stage"], "name": item["name"]}
            for key, item in zip(keys, items, strict=True)
        ]


def run(
    root: Path,
    protocol_path: Path,
    *,
    jobs: int = 1,
    isolated: bool = True,
    proof: dict[str, Any] | None = None,
) -> Path:
    work = research_root(root)
    report = checked_protocol(protocol_path)
    plan = report["plan"]
    if plan["data_mode"] == "real_official" and proof is None:
        raise ValueError("Real run requires a confirmed remote protocol commit")
    smoke = plan["smoke"]
    runner = Runner(work, report, protocol_path, jobs=jobs, isolated=isolated)
    tuning = [f["id"] for f in plan["tuning_folds"]]
    holdout = [f["id"] for f in plan["holdout_folds"]]
    variants = plan["feature_variants"]
    stages: dict[str, list[dict[str, Any]]] = {}
    stages["S1_features"] = runner.many(
        trials_for("S1_features", "cohort64", tuning, variants)
    )
    chosen = select_features(stages["S1_features"], variants)
    searched = search_variants(chosen, smoke)
    stages["S2_search"] = runner.many(
        trials_for("S2_search", "cohort64", tuning, searched)
    )
    selected = select_params(stages["S2_search"], searched)
    items: list[dict[str, Any]] = []
    ablation: list[dict[str, Any]] = []
    seeds: list[dict[str, Any]] = []
    for kind in TYPES:
        offered = holdout_variants(selected, kind, smoke)
        for name in plan["populations"]:
            options = offered
            if name == "cohort256x4":
                options = {
                    k: v
                    for k, v in offered.items()
                    if k in ("seasonal_naive", "lightgbm_selected")
                }
            for item in trials_for("S3_holdout", name, holdout, options):
                if item["prediction_type"] == kind:
                    item["robustness"] = name == "cohort64" and item["name"].endswith(
                        "_selected"
                    )
                    items.append(item)
        for stage, bucket, options in (
            ("S4_ablation", ablation, ablation_variants(selected, kind)),
            (
                "S5_seeds",
                seeds,
                seed_variants(selected, kind, plan["extra_seeds"]),
            ),
        ):
            bucket.extend(
                item
                for item in trials_for(stage, "cohort64", holdout, options)
                if item["prediction_type"] == kind
            )
    stages["S3_holdout"] = runner.many(items)
    stages["S4_ablation"] = runner.many(ablation)
    stages["S5_seeds"] = runner.many(seeds)
    checked_protocol(protocol_path)
    every = [row for rows in stages.values() for row in rows]
    summary = {
        "kind": "decision_outcome",
        "protocol": str(protocol_path),
        "protocol_id": report["id"],
        "remote_proof": proof,
        "stages": stages,
        "feature_selection": chosen,
        "parameter_selection": selected,
        "cumulative_worker_seconds": runner.spent,
        "outcome_counts": {
            s: sum(r["status"] == s for r in every)
            for s in ("success", "failed", "not_run")
        },
        "final_test_accessed": False,
        "status": "completed"
        if all(r["status"] == "success" for r in every)
        else "partial",
    }
    return create(
        work,
        "outcomes",
        identity([VERSION, summary]),
        lambda stage: summary,
        activate=False,
    )


def trial_bound(plan: dict[str, Any]) -> int:
    """Upper bound written next to the budgets; dynamic stages take their maximum."""
    tuning, holdout = len(plan["tuning_folds"]), len(plan["holdout_folds"])
    search = sum(len(v) for v in plan["search_trials"].values())
    search += len(plan["poisson_alpha"]) + 4
    cohorts = len(plan["populations"])
    per_fold = (
        8 * max(cohorts - 1, 1) + 2 * (cohorts > 1) + 12 + 3 * len(plan["extra_seeds"])
    )
    return 2 * (tuning * (len(plan["feature_variants"]) + search) + holdout * per_fold)


def dataset_of(args: argparse.Namespace) -> Path:
    return Path(args.dataset or active(args.data_dir, "datasets"))


def main() -> None:
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[variable] = "2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "readiness",
            "lineage",
            "freeze",
            "run",
            "analytics",
            "verify",
            "worker",
        ],
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--readiness", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--analytics", type=Path)
    parser.add_argument("--lineage", type=Path)
    parser.add_argument("--corrects", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--output", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    if args.command == "worker":
        if args.request is None:
            parser.error("private --request required")
        write_json(
            args.request.parent / "outcome.json",
            worker(read_json(args.request), args.request.parent),
        )
    elif args.command == "lineage":
        from citibike.decision_research.lineage import lineage

        print(lineage(args.data_dir, dataset_of(args)))
    elif args.command == "readiness":
        from citibike.decision_research.readiness import readiness

        print(readiness(args.data_dir, dataset_of(args)))
    elif args.command == "freeze":
        if args.readiness is None:
            parser.error("--readiness required: audit precedes the protocol")
        from citibike.decision_research.verify import protocol_evidence

        path = freeze(
            args.data_dir, dataset_of(args), args.readiness, corrects=args.corrects
        )
        protocol_evidence(path, args.output)
        print(path)
    elif args.command == "run":
        if args.protocol is None:
            parser.error("--protocol required; no final/test option exists")
        report = checked_protocol(args.protocol)
        result = run(
            args.data_dir,
            args.protocol,
            jobs=args.jobs,
            proof=remote_proof(report["id"]),
        )
        print(result)
        if checked(result)["status"] != "completed":
            raise SystemExit(2)
    elif args.command == "analytics":
        if args.result is None or args.lineage is None:
            parser.error("--result and --lineage required")
        from citibike.decision_research.analytics import analytics

        print(analytics(args.data_dir, args.result, args.lineage))
    else:
        from citibike.decision_research.verify import verify

        if args.result is None or args.analytics is None:
            parser.error("--result and --analytics required")
        print(verify(args.result, args.analytics, args.output))
