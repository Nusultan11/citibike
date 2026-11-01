"""Research adapters, outside the frozen F4 executor hash namespace."""

import importlib
import time
import warnings
from typing import Any

from citibike.features.contracts import CATALOG, SEED
from citibike.features.research import SCHEMA, A, B
from citibike.forecasting import models
from citibike.processing.resources import peak_rss_bytes


def names(groups: list[str]) -> list[str]:
    if groups not in ([], ["A"], ["B"], ["A", "B"]):
        raise ValueError("Unknown research groups")
    return (
        list(CATALOG)
        + (list(A) if "A" in groups else [])
        + (list(B) if "B" in groups else [])
    )


def fit(frame: Any, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    groups = config.get("research_groups", [])
    columns = names(groups)
    options = {k: v for k, v in config.items() if k != "research_groups"}
    if not groups:
        return models.fit(frame, options)
    if options["algorithm"] == "seasonal_naive":
        raise ValueError("Baseline cannot use experimental features")
    before = time.monotonic()
    fitted: dict[str, Any] = {
        "research_schema": SCHEMA,
        "feature_names": columns,
        "config": config,
        "algorithm": options.pop("algorithm"),
    }
    x = frame[columns].copy()
    y = frame["actual"].to_numpy(dtype=float)
    np = importlib.import_module("numpy")
    if not len(y) or not np.isfinite(y).all() or (y < 0).any():
        raise ValueError("Invalid research targets")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        limits = importlib.import_module("threadpoolctl").threadpool_limits
        with limits(limits=2):
            if max(y) == 0:
                fitted["constant_zero"] = True
            elif fitted["algorithm"] == "poisson":
                pre = importlib.import_module("sklearn.preprocessing")
                pipe = importlib.import_module("sklearn.pipeline")
                compose = importlib.import_module("sklearn.compose")
                impute = importlib.import_module("sklearn.impute")
                linear = importlib.import_module("sklearn.linear_model")
                categorical = [
                    n
                    for n in models.GROUPS["calendar"] + ["station_id", "horizon_h"]
                    if n in columns
                ]
                numeric = [n for n in columns if n not in categorical]
                # Explicit train-only imputation; missing never becomes an observation.
                continuous = pipe.make_pipeline(
                    impute.SimpleImputer(
                        strategy="median", add_indicator=True, keep_empty_features=True
                    ),
                    pre.StandardScaler(),
                )
                fitted["model"] = pipe.make_pipeline(
                    compose.ColumnTransformer(
                        [
                            (
                                "categories",
                                pre.OneHotEncoder(handle_unknown="ignore"),
                                categorical,
                            ),
                            ("numeric", continuous, numeric),
                        ]
                    ),
                    linear.PoissonRegressor(**options),
                )
                fitted["model"].fit(x, y)
            elif fitted["algorithm"] == "lightgbm":
                pd = importlib.import_module("pandas")
                fitted["station_levels"] = sorted(x["station_id"].unique().tolist())
                x["station_id"] = pd.Categorical(
                    x["station_id"], categories=fitted["station_levels"]
                )
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
            elif fitted["algorithm"] == "catboost":
                fitted["model"] = importlib.import_module("catboost").CatBoostRegressor(
                    loss_function="Poisson",
                    random_seed=SEED,
                    thread_count=2,
                    allow_writing_files=False,
                    verbose=False,
                    **options,
                )
                fitted["model"].fit(x, y, cat_features=["station_id"])
            else:
                raise ValueError("Unknown research algorithm")
    return fitted, {
        "fit_seconds": time.monotonic() - before,
        "peak_rss_bytes": peak_rss_bytes(),
        "training_rows": len(frame),
        "warnings": [str(w.message) for w in caught],
        "missing_training_values": int(frame[columns].isna().sum().sum()),
        "empty_train_numeric_columns": [n for n in columns if frame[n].isna().all()],
        "imputation": "Poisson: train median+indicator; empty column 0+indicator; "
        "trees: native NaN. No imputed value is a source observation.",
    }


def predict(fitted: dict[str, Any], frame: Any) -> tuple[list[float], float]:
    if "research_schema" not in fitted:
        return models.predict(fitted, frame)
    if fitted["research_schema"] != SCHEMA or fitted["feature_names"] != names(
        fitted["config"]["research_groups"]
    ):
        raise ValueError("Research saved-model schema mismatch")
    before = time.monotonic()
    x = frame[fitted["feature_names"]].copy()
    if fitted.get("constant_zero"):
        values = [0.0] * len(x)
    else:
        if fitted["algorithm"] == "lightgbm":
            pd = importlib.import_module("pandas")
            x["station_id"] = pd.Categorical(
                x["station_id"], categories=fitted["station_levels"]
            )
        if fitted["algorithm"] == "catboost":
            values = fitted["model"].predict(
                x, prediction_type="Exponent", thread_count=2
            )
        else:
            values = fitted["model"].predict(x)
    result = [float(v) for v in values]
    np = importlib.import_module("numpy")
    if not np.isfinite(result).all() or any(v < 0 for v in result):
        raise ValueError("Invalid research output")
    return result, time.monotonic() - before
