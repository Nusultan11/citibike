"""Bounded adapters; optional ML libraries are imported only inside workers."""

import importlib
import math
import time
import warnings
from pathlib import Path
from typing import Any

from citibike.features.contracts import CATALOG, NUMERIC, SEED
from citibike.processing.resources import peak_rss_bytes


def configurations(smoke: bool) -> dict[str, dict[str, Any]]:
    return {
        "seasonal_naive": {"algorithm": "seasonal_naive"},
        "poisson_default": {"algorithm": "poisson", "alpha": 1.0, "max_iter": 100},
        "lightgbm_default": {
            "algorithm": "lightgbm",
            "n_estimators": 10 if smoke else 100,
            "num_leaves": 31,
            "learning_rate": 0.1,
        },
        "catboost_default": {
            "algorithm": "catboost",
            "iterations": 10 if smoke else 1000,
            "depth": 6,
        },
        "poisson_bounded": {"algorithm": "poisson", "alpha": 0.1, "max_iter": 200},
        "lightgbm_bounded": {
            "algorithm": "lightgbm",
            "n_estimators": 20 if smoke else 200,
            "num_leaves": 15,
            "learning_rate": 0.05,
        },
        "catboost_bounded": {
            "algorithm": "catboost",
            "iterations": 20 if smoke else 500,
            "depth": 4,
        },
    }


def fit(frame: Any, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    before = time.perf_counter()
    x = frame[list(CATALOG)].copy()
    y = frame["actual"].to_numpy(dtype=float)
    if len(y) == 0 or any(not math.isfinite(float(v)) or v < 0 for v in y):
        raise ValueError("Invalid/empty training targets")
    kind = config["algorithm"]
    fitted: dict[str, Any] = {
        "algorithm": kind,
        "feature_names": list(CATALOG),
        "config": config,
    }
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if kind == "seasonal_naive":
            fitted["model"] = None
        elif max(y) == 0:
            fitted["constant_zero"] = True
        else:
            limits = importlib.import_module("threadpoolctl").threadpool_limits
            with limits(limits=2):
                if kind == "poisson":
                    preprocessing = importlib.import_module("sklearn.preprocessing")
                    compose = importlib.import_module("sklearn.compose")
                    pipeline = importlib.import_module("sklearn.pipeline")
                    linear = importlib.import_module("sklearn.linear_model")
                    categorical = [
                        "station_id",
                        "utc_hour",
                        "local_hour",
                        "weekday",
                        "month",
                        "weekend",
                        "holiday",
                        "dst",
                        "horizon_h",
                    ]
                    numeric = [n for n in NUMERIC if n not in categorical]
                    transform = compose.ColumnTransformer(
                        [
                            (
                                "categories",
                                preprocessing.OneHotEncoder(handle_unknown="ignore"),
                                categorical,
                            ),
                            ("continuous", preprocessing.StandardScaler(), numeric),
                        ]
                    )
                    fitted["model"] = pipeline.make_pipeline(
                        transform,
                        linear.PoissonRegressor(
                            alpha=config["alpha"], max_iter=config["max_iter"]
                        ),
                    )
                    fitted["model"].fit(x, y)
                elif kind == "lightgbm":
                    pd = importlib.import_module("pandas")
                    fitted["station_levels"] = sorted(x["station_id"].unique().tolist())
                    x["station_id"] = pd.Categorical(
                        x["station_id"], categories=fitted["station_levels"]
                    )
                    options = {k: v for k, v in config.items() if k != "algorithm"}
                    fitted["model"] = importlib.import_module("lightgbm").LGBMRegressor(
                        objective="poisson",
                        random_state=SEED,
                        n_jobs=2,
                        deterministic=True,
                        force_col_wise=True,
                        verbosity=-1,
                        **options,
                    )
                    fitted["model"].fit(x, y, categorical_feature=["station_id"])
                elif kind == "catboost":
                    options = {k: v for k, v in config.items() if k != "algorithm"}
                    fitted["model"] = importlib.import_module(
                        "catboost"
                    ).CatBoostRegressor(
                        loss_function="Poisson",
                        random_seed=SEED,
                        thread_count=2,
                        allow_writing_files=False,
                        verbose=False,
                        **options,
                    )
                    fitted["model"].fit(x, y, cat_features=["station_id"])
                else:
                    raise ValueError("Unknown model algorithm")
    return fitted, {
        "fit_seconds": time.perf_counter() - before,
        "peak_rss_bytes": peak_rss_bytes(),
        "training_rows": len(frame),
        "training_station_levels": sorted(
            x["station_id"].astype(str).unique().tolist()
        ),
        "warnings": [str(w.message) for w in caught],
        "all_zero_training": bool(max(y) == 0),
    }


def predict(fitted: dict[str, Any], frame: Any) -> tuple[list[float], float]:
    before = time.perf_counter()
    if fitted["feature_names"] != list(CATALOG):
        raise ValueError("Model feature schema mismatch")
    x = frame[list(CATALOG)].copy()
    kind = fitted["algorithm"]
    if kind == "seasonal_naive":
        values = x["seasonal_naive"].tolist()
    elif fitted.get("constant_zero"):
        values = [0.0] * len(x)
    else:
        if kind == "lightgbm":
            pd = importlib.import_module("pandas")
            x["station_id"] = pd.Categorical(
                x["station_id"], categories=fitted["station_levels"]
            )
        if kind == "catboost":
            values = fitted["model"].predict(
                x, prediction_type="Exponent", thread_count=2
            )
        else:
            values = fitted["model"].predict(x)
    result = [float(v) for v in values]
    if any(not math.isfinite(v) or v < 0 for v in result):
        raise ValueError("Invalid model output; no silent clipping")
    return result, time.perf_counter() - before


def save(path: Path, fitted: dict[str, Any]) -> None:
    importlib.import_module("joblib").dump(fitted, path)


def load(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = importlib.import_module("joblib").load(path)
    return result
