"""Small research extensions of the existing F4 adapters."""

import importlib
import time
from typing import Any

from citibike.decision_research import models
from citibike.processing.resources import peak_rss_bytes

QUANTILES = (0.1, 0.5, 0.9)


def configurations() -> dict[str, dict[str, Any]]:
    tree = {"n_estimators": 150, "num_leaves": 15, "learning_rate": 0.05}
    regular = {**tree, "num_leaves": 7, "min_child_samples": 100, "reg_lambda": 10}

    def config(algorithm: str, **params: Any) -> dict[str, Any]:
        return {"algorithm": algorithm, "params": params, "groups": [], "seed": 42}

    return {
        "seasonal_naive": config("seasonal_naive"),
        "poisson": config("poisson", alpha=1.0, max_iter=200),
        "poisson_regularized": config("poisson", alpha=10.0, max_iter=200),
        "lightgbm_poisson": config("lightgbm", **tree),
        "lightgbm_poisson_regularized": config("lightgbm", **regular),
        "lightgbm_l1": {**config("lightgbm", **tree), "loss": "regression_l1"},
        "lightgbm_l1_regularized": {
            **config("lightgbm", **regular),
            "loss": "regression_l1",
        },
        **{
            "lightgbm_l1_" + group: {
                **config("lightgbm", **tree),
                "loss": "regression_l1",
                "groups": [group],
            }
            for group in ("A", "P", "S")
        },
        "catboost": config("catboost", iterations=300, depth=6),
        "catboost_regularized": config(
            "catboost", iterations=300, depth=4, l2_leaf_reg=10
        ),
        "hurdle": config("hurdle", **tree),
        "hurdle_regularized": config("hurdle", **regular),
        "quantile_lightgbm": {**config("lightgbm", **tree), "task": "quantile"},
        "quantile_empirical": {**config("empirical"), "task": "quantile"},
        "quantile_poisson_sn": {**config("poisson_sn"), "task": "quantile"},
        "spike_lightgbm": {**config("lightgbm", **regular), "task": "spike"},
    }


def matrix(train: Any, evaluation: Any, groups: list[str]) -> tuple[Any, Any]:
    pd = importlib.import_module("pandas")
    columns = models.names(groups)
    x, z = train[columns].copy(), evaluation[columns].copy()
    levels = sorted(x["station_id"].unique())
    for frame in (x, z):
        frame["station_id"] = pd.Categorical(frame["station_id"], categories=levels)
    return x, z


def predictions(train: Any, evaluation: Any, config: dict[str, Any]) -> tuple[Any, Any]:
    """Return an output frame and measured resources. No validation-set fitting."""
    np = importlib.import_module("numpy")
    task = config.get("task", "point")
    start = time.monotonic()
    cpu_start = time.process_time()
    output = evaluation.copy()
    resource: dict[str, Any] = {}
    if task == "point" and "loss" not in config:
        fitted, resource = models.fit(train, config)
        values, seconds = models.predict(fitted, evaluation)
        output["predicted"] = values
        resource["inference_seconds"] = seconds
    else:
        fit_start = time.monotonic()
        x, z = matrix(train, evaluation, config["groups"])
        y = train["actual"].to_numpy(float)
        options = models.boosting(
            config["params"], config["seed"], config.get("loss", "quantile")
        )
        lgb = importlib.import_module("lightgbm")
        if task == "point":
            fitted = lgb.LGBMRegressor(**options).fit(x, y)
            resource["fit_seconds"] = time.monotonic() - fit_start
            before = time.monotonic()
            # Projection onto count support is frozen, never fitted on validation.
            raw = fitted.predict(z)
            resource["negative_raw_predictions"] = int((raw < 0).sum())
            output["predicted"] = np.maximum(raw, 0)
        elif task == "spike":
            labels = y >= 20
            fitted = None
            if labels.any() and not labels.all():
                options["objective"] = "binary"
                fitted = lgb.LGBMClassifier(**options).fit(x, labels.astype(int))
            resource["fit_seconds"] = time.monotonic() - fit_start
            before = time.monotonic()
            output["probability"] = (
                fitted.predict_proba(z)[:, 1] if fitted is not None else labels.mean()
            )
            output["baseline_probability"] = float(labels.mean())
        elif config["algorithm"] == "lightgbm":
            fitted_quantiles = [
                lgb.LGBMRegressor(**options, alpha=q).fit(x, y) for q in QUANTILES
            ]
            resource["fit_seconds"] = time.monotonic() - fit_start
            before = time.monotonic()
            raw = np.column_stack([m.predict(z) for m in fitted_quantiles])
            resource["raw_crossing_rows"] = int(
                (np.diff(raw, axis=1) < 0).any(axis=1).sum()
            )
            values = np.sort(np.maximum(raw, 0), axis=1)
            for i, q in enumerate(QUANTILES):
                output[f"q{q}"] = values[:, i]
            output["predicted"] = values[:, 1]
        elif config["algorithm"] == "poisson_sn":
            stats = importlib.import_module("scipy.stats")
            resource["fit_seconds"] = 0.0
            before = time.monotonic()
            for q in QUANTILES:
                output[f"q{q}"] = stats.poisson.ppf(q, evaluation["seasonal_naive"])
            output["predicted"] = output["q0.5"]
        elif config["algorithm"] == "empirical":
            keys = ["station_id", "weekday", "local_hour"]
            table = (
                train.groupby(keys, observed=True)["actual"]
                .quantile(QUANTILES)
                .unstack()
            )
            resource["fit_seconds"] = time.monotonic() - fit_start
            before = time.monotonic()
            joined = evaluation[keys].merge(
                table, how="left", on=keys, validate="many_to_one"
            )
            resource["unseen_cells"] = int(joined[0.5].isna().sum())
            for q in QUANTILES:
                output[f"q{q}"] = joined[q].fillna(float(np.quantile(y, q))).to_numpy()
            output["predicted"] = output["q0.5"]
        else:
            raise ValueError("Unknown probabilistic adapter")
        resource["inference_seconds"] = time.monotonic() - before
    fields = (
        ["probability", "baseline_probability"] if task == "spike" else ["predicted"]
    )
    if task == "quantile":
        fields += [f"q{q}" for q in QUANTILES]
    if (
        not np.isfinite(output[fields].to_numpy(float)).all()
        or (output[fields] < 0).any().any()
    ):
        raise ValueError("Invalid research prediction")
    resource.update(
        elapsed_seconds=time.monotonic() - start,
        adapter_cpu_seconds=time.process_time() - cpu_start,
        peak_rss_bytes=peak_rss_bytes(),
        training_rows=len(train),
    )
    return output, resource
