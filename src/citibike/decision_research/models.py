"""Decision research adapters, including the two-stage hurdle; research only."""

import importlib
import math
import time
import warnings
from typing import Any

from citibike.decision_research.features import D1_SCHEMA, GROUPS, SCHEMA
from citibike.features.contracts import CATALOG
from citibike.forecasting import models as legacy
from citibike.processing.resources import peak_rss_bytes

ALGORITHMS = ("seasonal_naive", "poisson", "lightgbm", "catboost", "hurdle")
ORDER = ("A", "C", "P", "S")
CATEGORICAL = [*legacy.GROUPS["calendar"], "station_id", "horizon_h"]


def names(groups: list[str]) -> list[str]:
    if groups != [g for g in ORDER if g in groups] or len(set(groups)) != len(groups):
        raise ValueError("Unknown or unordered research feature groups")
    return list(CATALOG) + [c for g in groups for c in GROUPS[g]]


def boosting(params: dict[str, Any], seed: int, objective: str) -> dict[str, Any]:
    return {
        "objective": objective,
        "random_state": seed,
        "n_jobs": 2,
        "deterministic": True,
        "force_col_wise": True,
        "verbosity": -1,
        **params,
    }


def fit_hurdle(
    x: Any, y: Any, params: dict[str, Any], seed: int, categorical: list[str]
) -> dict[str, Any]:
    """P(y>0) from a classifier, E[y|y>0] from a Poisson fit on positive rows."""
    lightgbm = importlib.import_module("lightgbm")
    positive = y > 0
    stages: dict[str, Any] = {"positive_share": float(positive.mean())}
    if not positive.all():
        stages["occurrence"] = lightgbm.LGBMClassifier(
            **boosting(params, seed, "binary")
        ).fit(x, positive.astype(int), categorical_feature=categorical)
    stages["intensity"] = lightgbm.LGBMRegressor(
        **boosting(params, seed, "poisson")
    ).fit(x[positive], y[positive], categorical_feature=categorical)
    return stages


def fit(frame: Any, config: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    algorithm, groups = config["algorithm"], config["groups"]
    if algorithm not in ALGORITHMS:
        raise ValueError("Unknown decision research algorithm")
    params, seed = dict(config["params"]), config["seed"]
    if algorithm == "seasonal_naive" and (groups or params):
        raise ValueError("Baseline cannot use experimental features or parameters")
    columns = names(groups)
    before = time.monotonic()
    fitted: dict[str, Any] = {
        "research_schema": SCHEMA,
        "feature_names": columns,
        "config": config,
        "algorithm": algorithm,
    }
    x = frame[columns].copy()
    y = frame["actual"].to_numpy(dtype=float)
    np = importlib.import_module("numpy")
    if not len(y) or not np.isfinite(y).all() or (y < 0).any():
        raise ValueError("Invalid decision research targets")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        limits = importlib.import_module("threadpoolctl").threadpool_limits
        with limits(limits=2):
            if algorithm == "seasonal_naive":
                fitted["model"] = None
            elif max(y) == 0:
                fitted["constant_zero"] = True
            elif algorithm == "poisson":
                fitted["model"] = poisson(columns, params)
                fitted["model"].fit(x, y)
            elif algorithm == "catboost":
                fitted["model"] = importlib.import_module("catboost").CatBoostRegressor(
                    loss_function="Poisson",
                    random_seed=seed,
                    thread_count=2,
                    allow_writing_files=False,
                    verbose=False,
                    **params,
                )
                fitted["model"].fit(x, y, cat_features=["station_id"])
            else:
                pd = importlib.import_module("pandas")
                fitted["station_levels"] = sorted(x["station_id"].unique().tolist())
                x["station_id"] = pd.Categorical(
                    x["station_id"], categories=fitted["station_levels"]
                )
                if algorithm == "lightgbm":
                    fitted["model"] = importlib.import_module("lightgbm").LGBMRegressor(
                        **boosting(params, seed, "poisson")
                    )
                    fitted["model"].fit(x, y, categorical_feature=["station_id"])
                else:
                    fitted.update(fit_hurdle(x, y, params, seed, ["station_id"]))
    return fitted, {
        "fit_seconds": time.monotonic() - before,
        "peak_rss_bytes": peak_rss_bytes(),
        "training_rows": len(frame),
        "warnings": [str(w.message) for w in caught],
        "missing_training_values": int(frame[columns].isna().sum().sum()),
    }


def poisson(columns: list[str], params: dict[str, Any]) -> Any:
    pre = importlib.import_module("sklearn.preprocessing")
    pipe = importlib.import_module("sklearn.pipeline")
    compose = importlib.import_module("sklearn.compose")
    impute = importlib.import_module("sklearn.impute")
    linear = importlib.import_module("sklearn.linear_model")
    categorical = [c for c in CATEGORICAL if c in columns]
    numeric = [c for c in columns if c not in categorical]
    # Train-only median and indicator; an imputed value is not an observation.
    continuous = pipe.make_pipeline(
        impute.SimpleImputer(
            strategy="median", add_indicator=True, keep_empty_features=True
        ),
        pre.StandardScaler(),
    )
    return pipe.make_pipeline(
        compose.ColumnTransformer(
            [
                ("categories", pre.OneHotEncoder(handle_unknown="ignore"), categorical),
                ("numeric", continuous, numeric),
            ]
        ),
        linear.PoissonRegressor(**params),
    )


def readable(fitted: dict[str, Any]) -> bool:
    """D1 changed only group P, so a D1-schema model without P reads new inputs."""
    if fitted["feature_names"] != names(fitted["config"]["groups"]):
        return False
    return fitted["research_schema"] == SCHEMA or (
        fitted["research_schema"] == D1_SCHEMA and "P" not in fitted["config"]["groups"]
    )


def predict(fitted: dict[str, Any], frame: Any) -> tuple[list[float], float]:
    if not readable(fitted):
        raise ValueError("Decision research saved-model schema mismatch")
    before = time.monotonic()
    x = frame[fitted["feature_names"]].copy()
    algorithm = fitted["algorithm"]
    if algorithm == "seasonal_naive":
        values = x["seasonal_naive"].tolist()
    elif fitted.get("constant_zero"):
        values = [0.0] * len(x)
    else:
        if algorithm in ("lightgbm", "hurdle"):
            pd = importlib.import_module("pandas")
            x["station_id"] = pd.Categorical(
                x["station_id"], categories=fitted["station_levels"]
            )
        if algorithm == "catboost":
            values = fitted["model"].predict(
                x, prediction_type="Exponent", thread_count=2
            )
        elif algorithm == "hurdle":
            values = hurdle_values(fitted, x)
        else:
            values = fitted["model"].predict(x)
    result = [float(v) for v in values]
    if any(not math.isfinite(v) or v < 0 for v in result):
        raise ValueError("Invalid decision research output; no silent clipping")
    return result, time.monotonic() - before


def hurdle_values(fitted: dict[str, Any], x: Any) -> Any:
    np = importlib.import_module("numpy")
    occurrence = (
        fitted["occurrence"].predict_proba(x)[:, 1]
        if "occurrence" in fitted
        else np.full(len(x), fitted["positive_share"])
    )
    # A positive count is at least one; this bound is part of the estimand.
    return occurrence * np.maximum(fitted["intensity"].predict(x), 1.0)


def occurrence_calibration(fitted: dict[str, Any], frame: Any) -> dict[str, Any] | None:
    """Mean predicted P(y>0) against the observed share, per decile of P."""
    if fitted["algorithm"] != "hurdle" or "occurrence" not in fitted:
        return None
    pd = importlib.import_module("pandas")
    x = frame[fitted["feature_names"]].copy()
    x["station_id"] = pd.Categorical(
        x["station_id"], categories=fitted["station_levels"]
    )
    data = pd.DataFrame(
        {
            "p": fitted["occurrence"].predict_proba(x)[:, 1],
            "y": (frame["actual"].to_numpy(dtype=float) > 0).astype(float),
        }
    )
    data["bin"] = pd.qcut(data["p"], 10, labels=False, duplicates="drop")
    groups = data.groupby("bin", sort=True).agg(
        n=("p", "size"), predicted=("p", "mean"), observed=("y", "mean")
    )
    return {
        "mean_predicted": float(data["p"].mean()),
        "mean_observed": float(data["y"].mean()),
        "expected_calibration_error": float(
            ((groups["predicted"] - groups["observed"]).abs() * groups["n"]).sum()
            / len(data)
        ),
        "deciles": groups.reset_index().to_dict("records"),
    }
