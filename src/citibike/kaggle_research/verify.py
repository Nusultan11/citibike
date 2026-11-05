"""Independent local arithmetic and membership audit of remote trial outputs."""

import importlib
import math
from pathlib import Path
from statistics import mean
from typing import Any

from citibike.decision_research.verify import bootstrap
from citibike.forecasting.evaluation_weather import attach
from citibike.forecasting.feature_research import read_frame
from citibike.ingestion.archive import checksum
from citibike.kaggle_research.study import KEY, check_bundle, validate_frames
from citibike.storage import identity, read_json, write_json


def metric(actual: list[float], predicted: list[float]) -> dict[str, Any]:
    if not actual or len(actual) != len(predicted):
        raise ValueError("Nonempty paired observations required")
    if any(not math.isfinite(v) or v < 0 for v in actual + predicted):
        raise ValueError("Invalid metric observation")
    errors = [p - y for p, y in zip(predicted, actual, strict=True)]
    absolute, total = math.fsum(map(abs, errors)), math.fsum(actual)
    return {
        "n": len(actual),
        "actual_sum": total,
        "mae": absolute / len(actual),
        "rmse": math.sqrt(math.fsum(e**2 for e in errors) / len(actual)),
        "bias": math.fsum(errors) / len(actual),
        "wape": absolute / total if total else None,
    }


def quantiles(frame: Any) -> dict[str, Any]:
    """Reference uses scalar comparisons/fsum, not worker NumPy operations."""
    rows = list(
        frame[["actual", "q0.1", "q0.5", "q0.9"]].itertuples(index=False, name=None)
    )
    if not rows:
        raise ValueError("Empty probabilistic population")
    if any(
        not 0 <= lo <= mid <= hi or not all(math.isfinite(v) for v in (y, lo, mid, hi))
        for y, lo, mid, hi in rows
    ):
        raise ValueError("Corrupt quantile interval")
    pinball = {
        str(q): math.fsum(max(q * (r[0] - r[i]), (q - 1) * (r[0] - r[i])) for r in rows)
        / len(rows)
        for i, q in enumerate((0.1, 0.5, 0.9), 1)
    }
    return {
        "n": len(rows),
        "pinball": pinball,
        "mean_pinball": mean(pinball.values()),
        "coverage_80": sum(lo <= y <= hi for y, lo, _, hi in rows) / len(rows),
        "mean_width": math.fsum(hi - lo for _, lo, _, hi in rows) / len(rows),
    }


def close(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(float(left), float(right), rel_tol=1e-10, abs_tol=1e-10)


def segment_masks(frame: Any) -> dict[str, Any]:
    peak = (frame["weekday"] < 5) & frame["local_hour"].isin([7, 8, 9, 16, 17, 18, 19])
    masks = {
        "all": frame["horizon_h"] > 0,
        "peak_hour": peak,
        "off_peak": ~peak,
        "new_known": frame["new_known"],
        "anomalous_id": frame["anomalous_id"],
        "ambiguous_id": frame["ambiguous_id"],
        "suspect_zero_run": frame["suspect_closure"],
    }
    for field in ("station_id", "horizon_h", "activity", "history_depth"):
        for value in sorted(frame[field].unique()):
            masks[f"{field}:{value}"] = frame[field] == value
    for name in ("cold", "heat", "heavy_rain", "observed"):
        masks["weather:" + name] = frame["eval_weather_" + name]
    for season, months in {
        "winter": [12, 1, 2],
        "spring": [3, 4, 5],
        "summer": [6, 7, 8],
        "autumn": [9, 10, 11],
    }.items():
        masks["season:" + season] = frame["month"].isin(months)
    return masks


def spike_metrics(frame: Any) -> dict[str, Any]:
    import numpy as np

    average_precision_score = importlib.import_module(
        "sklearn.metrics"
    ).average_precision_score

    y = (frame["actual"].to_numpy(float) >= 20).astype(float)
    result = {
        "n": len(y),
        "positives": int(y.sum()),
        "prevalence": float(y.mean()),
        "label": "published station-hour count>=20; not an operational incident",
    }
    for column in ("probability", "baseline_probability"):
        p = frame[column].to_numpy(float)
        if not np.isfinite(p).all() or (p < 0).any() or (p > 1).any():
            raise ValueError("Invalid spike probability")
        clipped = np.clip(p, 1e-15, 1 - 1e-15)
        result[column] = {
            "brier": float(np.mean((p - y) ** 2)),
            "log_loss": float(
                -np.mean(y * np.log(clipped) + (1 - y) * np.log1p(-clipped))
            ),
            "average_precision": float(average_precision_score(y, p))
            if y.any()
            else None,
            "mean_probability": float(p.mean()),
        }
    return result


def new_tasks(
    joined: dict[tuple[str, str], Any],
    cells: list[dict[str, Any]],
    spikes: list[dict[str, Any]],
    family: int,
) -> dict[str, Any]:
    np = importlib.import_module("numpy")
    probability: list[dict[str, Any]] = []
    spike_results: list[dict[str, Any]] = []
    for target in ("departures", "arrivals"):
        names = ("quantile_lightgbm", "quantile_empirical", "quantile_poisson_sn")
        if not all((n, target) in joined for n in names):
            probability.append(
                {
                    "target": target,
                    "decision": "INCONCLUSIVE",
                    "reason": "Incomplete probabilistic trials",
                }
            )
            continue
        loss_frames = {}
        for name in names:
            frame = joined[name, target].copy()
            losses = []
            for q in (0.1, 0.5, 0.9):
                residual = frame["actual"].to_numpy(float) - frame[f"q{q}"].to_numpy(
                    float
                )
                losses.append(np.maximum(q * residual, (q - 1) * residual))
            frame["predicted"] = np.mean(losses, axis=0)
            frame["actual"] = 0.0
            loss_frames[name] = frame
        uncertainty = []
        for ref in names[1:]:
            stats = bootstrap(
                loss_frames[names[0]],
                loss_frames[ref],
                replicates=5000,
                alpha=0.05 / family,
            )
            stats["macro_pinball_difference"] = stats.pop(
                "macro_mae_difference_left_minus_right"
            )
            uncertainty.append({"reference": ref, **stats})
        candidate_cells = [
            c for c in cells if c["target"] == target and c["name"] == names[0]
        ]
        baseline_cells = {
            (c["name"], c["fold"]): c for c in cells if c["target"] == target
        }
        wins = {
            ref: sum(
                c["mean_pinball"] < baseline_cells[ref, c["fold"]]["mean_pinball"]
                for c in candidate_cells
            )
            for ref in names[1:]
        }
        pooled = quantiles(joined[names[0], target])
        degraded = [
            {"fold": c["fold"], "segment": s, "coverage": m["coverage_80"]}
            for c in candidate_cells
            for s, m in c["segments"].items()
            if (
                s in ("peak_hour", "off_peak", "new_known")
                or s.startswith(("activity:", "horizon_h:", "season:", "weather:"))
            )
            and m["coverage_80"] < 0.7
        ]
        passed = (
            min(wins.values()) >= 3
            and abs(pooled["coverage_80"] - 0.8) <= 0.05
            and not degraded
        )
        probability.append(
            {
                "target": target,
                "decision": "ACCEPTED FOR RESEARCH" if passed else "REJECTED",
                "wins": wins,
                "pooled": pooled,
                "undercovered_segments": degraded,
                "uncertainty": uncertainty,
                "operational_intervals": "NOT VERIFIED",
            }
        )
        key = ("spike_lightgbm", target)
        if key in joined:
            left, right = joined[key].copy(), joined[key].copy()
            y = (left["actual"].to_numpy(float) >= 20).astype(float)
            left["predicted"] = (left["probability"].to_numpy(float) - y) ** 2
            right["predicted"] = (
                right["baseline_probability"].to_numpy(float) - y
            ) ** 2
            left["actual"] = right["actual"] = 0.0
            stats = bootstrap(left, right, replicates=5000, alpha=0.05 / family)
            stats["macro_brier_difference"] = stats.pop(
                "macro_mae_difference_left_minus_right"
            )
            parts = [s for s in spikes if s["target"] == target]
            wins_count = sum(
                s["probability"]["brier"] < s["baseline_probability"]["brier"]
                for s in parts
            )
            spike_results.append(
                {
                    "target": target,
                    "wins": wins_count,
                    "decision": "REJECTED" if wins_count < 3 else "INCONCLUSIVE",
                    "uncertainty": stats,
                    "reason": (
                        "Reused validation; count spike labels are not incidents"
                    ),
                }
            )
    return {
        "probabilistic": probability,
        "spike": spike_results,
        "regimes": {
            "decision": "ACCEPTED FOR RESEARCH",
            "scope": (
                "Descriptive train-only history_depth/hist_band/activity "
                "slices; no learned latent-state or operational claim"
            ),
        },
        "anomalies": {
            "decision": "INCONCLUSIVE",
            "scope": (
                "Existing station-hour/CUSUM diagnostics; real incident "
                "precision lacks independent labels"
            ),
        },
        "risk_utility": {
            "decision": "INCONCLUSIVE",
            "reason": (
                "New validation origins have no matching valid historical "
                "GBFS/capacity; existing recorded-as-of F5 output is a "
                "separate scenario population. L1 medians are not Poisson "
                "means."
            ),
        },
    }


def verify(bundle: Path, result: Path, output: Path) -> Path:
    pd = importlib.import_module("pandas")

    plan = check_bundle(bundle)
    report = read_json(result / "result.json")
    if report["protocol_id"] != plan["id"]:
        raise ValueError("Remote protocol mismatch")
    if report["environment"]["packages"] != plan["environment"]["packages"]:
        raise ValueError("Remote runtime drift")
    hashes = read_json(result / "checksums.json")
    actual_files = {
        p.relative_to(result).as_posix()
        for p in result.rglob("*")
        if p.is_file() and p.name != "checksums.json"
    }
    if actual_files != set(hashes):
        raise ValueError("Incomplete or unexpected remote artifacts")
    for name, sha in hashes.items():
        if (result / name).resolve().is_relative_to(
            result.resolve()
        ) is False or checksum(result / name) != sha:
            raise ValueError("Remote artifact checksum mismatch")
    expected = {
        (name, s["fold"], target)
        for name in plan["configurations"]
        for s in plan["splits"]
        for target in ("departures", "arrivals")
    }
    observed = [(t["name"], t["fold"], t["prediction_type"]) for t in report["trials"]]
    if set(observed) != expected or len(observed) != len(expected):
        raise ValueError("Missing or duplicated trial accounting")
    frames = {}
    pit_rows = 0
    for split in plan["splits"]:
        train, evaluation = (
            read_frame(bundle / split[f]) for f in ("train_file", "evaluation_file")
        )
        validate_frames(train, evaluation, split)
        pit_rows += len(train) + len(evaluation)
        frames[split["fold"]] = evaluation
    scores: list[dict[str, Any]] = []
    all_frames: dict[tuple[str, str], list[Any]] = {}
    probabilistic: list[dict[str, Any]] = []
    spikes: list[dict[str, Any]] = []
    checked_rows = 0
    for trial in report["trials"]:
        name, fold, target = trial["name"], trial["fold"], trial["prediction_type"]
        request = {
            "protocol_id": plan["id"],
            "name": name,
            "fold": fold,
            "prediction_type": target,
        }
        path = result / identity(request)
        if trial["path"] != path.name or read_json(path / "trial.json") != trial:
            raise ValueError("Trial manifest substitution")
        if trial["status"] != "success":
            continue
        if (
            trial["request"] != request
            or trial["config"] != plan["configurations"][name]
        ):
            raise ValueError("Remote parameters or seed changed")
        frame = read_frame(path / "predictions.parquet")
        source = attach(frames[fold], read_json(bundle / "weather-daily.json"))
        source["ambiguous_id"] = source["station_id"].isin(plan["ambiguous_ids"])
        source = source[source["prediction_type"] == target]
        source = source.sort_values(KEY).reset_index(drop=True)
        frame = frame.sort_values(KEY).reset_index(drop=True)
        if not frame[[*KEY, "actual"]].equals(source[[*KEY, "actual"]]):
            raise ValueError("Remote labels or paired population changed")
        metadata = [
            c
            for c in frame
            if c
            not in (
                "predicted",
                "probability",
                "baseline_probability",
                "q0.1",
                "q0.5",
                "q0.9",
            )
        ]
        if not frame[metadata].equals(source[metadata]):
            raise ValueError("Remote segment metadata changed")
        frame["fold"] = fold
        checked_rows += len(frame)
        all_frames.setdefault((name, target), []).append(frame)
        if "predicted" in frame:
            segments = {
                segment: metric(
                    part["actual"].astype(float).tolist(),
                    part["predicted"].astype(float).tolist(),
                )
                for segment, mask in segment_masks(frame).items()
                if not (part := frame[mask]).empty
            }
            for key, value in segments["all"].items():
                if not close(value, trial["metrics"]["point"]["all"][key]):
                    raise ValueError("Remote point metric mismatch")
            scores.append(
                {"name": name, "fold": fold, "target": target, "segments": segments}
            )
        if "q0.1" in frame:
            stats = quantiles(frame)
            for key in ("mean_pinball", "coverage_80", "mean_width"):
                if not close(stats[key], trial["metrics"]["probabilistic"][key]):
                    raise ValueError("Remote probabilistic metric mismatch")
            probabilistic.append(
                {
                    "name": name,
                    "fold": fold,
                    "target": target,
                    **stats,
                    "segments": {
                        segment: quantiles(frame[mask])
                        for segment, mask in segment_masks(frame).items()
                        if int(mask.sum()) >= 100
                    },
                }
            )
        if "probability" in frame:
            spikes.append(
                {"name": name, "fold": fold, "target": target, **spike_metrics(frame)}
            )
    joined = {
        key: pd.concat(parts, ignore_index=True)
        for key, parts in all_frames.items()
        if len(parts) == 4
    }
    point_names = [
        name
        for name, config in plan["configurations"].items()
        if config.get("task", "point") == "point"
    ]
    pairs = [
        (name, "seasonal_naive", "baseline")
        for name in point_names
        if name != "seasonal_naive"
    ]
    pairs += [("lightgbm_l1", "lightgbm_poisson", "loss")]
    pairs += [
        ("lightgbm_l1_" + g, "lightgbm_l1", "feature:" + g) for g in ("A", "P", "S")
    ]
    pairs += [
        (name + "_regularized", name, "regularization")
        for name in ("poisson", "lightgbm_poisson", "lightgbm_l1", "catboost", "hurdle")
    ]
    family = 2 * len(pairs) + 6
    comparisons = []
    for target in ("departures", "arrivals"):
        for left, right, kind in pairs:
            if (left, target) not in joined or (right, target) not in joined:
                comparisons.append(
                    {
                        "left": left,
                        "right": right,
                        "target": target,
                        "kind": kind,
                        "decision": "INCONCLUSIVE",
                        "reason": "Incomplete trials",
                    }
                )
                continue
            stats = bootstrap(
                joined[left, target],
                joined[right, target],
                replicates=5000,
                alpha=0.05 / family,
            )
            comparisons.append(
                {
                    "left": left,
                    "right": right,
                    "target": target,
                    "kind": kind,
                    **stats,
                    "decision": "INCONCLUSIVE",
                    "reason": (
                        "Reused validation; four fold sign-flip p cannot establish a "
                        "new confirmatory improvement"
                    ),
                }
            )
    tables, decisions = [], []
    for target in ("departures", "arrivals"):
        baseline = {
            s["fold"]: s
            for s in scores
            if s["target"] == target and s["name"] == "seasonal_naive"
        }
        eligible = []
        for name in point_names:
            parts = [s for s in scores if s["name"] == name and s["target"] == target]
            if len(parts) != 4 or len(baseline) != 4:
                continue
            degraded = []
            wins = 0
            for part in parts:
                ref = baseline[part["fold"]]["segments"]
                wins += part["segments"]["all"]["mae"] < ref["all"]["mae"]
                for segment, value in part["segments"].items():
                    key_slice = segment in (
                        "peak_hour",
                        "off_peak",
                        "new_known",
                        "station_id:6753.08",
                        "station_id:6230.02",
                    ) or segment.startswith(
                        ("activity:", "horizon_h:", "season:", "weather:")
                    )
                    if (
                        key_slice
                        and value["n"] >= 100
                        and value["mae"] > 1.2 * ref[segment]["mae"] + 1e-12
                    ):
                        degraded.append(
                            {
                                "fold": part["fold"],
                                "segment": segment,
                                "mae": value["mae"],
                                "baseline_mae": ref[segment]["mae"],
                            }
                        )
            macro = {
                key: mean(p["segments"]["all"][key] for p in parts)
                for key in ("mae", "rmse", "bias", "wape")
            }
            tables.append(
                {
                    "name": name,
                    "target": target,
                    "macro": macro,
                    "wins": wins,
                    "degraded": degraded,
                    "decision": "ACCEPTED FOR RESEARCH"
                    if name == "seasonal_naive" or (not degraded and wins >= 3)
                    else "REJECTED",
                }
            )
            if wins >= 3 and not degraded and name != "seasonal_naive":
                eligible.append((macro["mae"], name))
        decisions.append(
            {
                "target": target,
                "exploratory_research_candidate": min(eligible)[1]
                if eligible
                else "seasonal_naive",
                "operational_champion": "seasonal_naive",
                "promotion": "NOT VERIFIED",
            }
        )
    evidence = {
        "protocol_id": plan["id"],
        "status": "VERIFIED"
        if all(t["status"] == "success" for t in report["trials"])
        else "PARTIAL",
        "verification": {
            "prediction_rows": checked_rows,
            "input_pit_rows": pit_rows,
            "mismatches": 0,
            "method": (
                "Source membership/actual match, scalar fsum "
                "metrics/quantiles, all artifact hashes; not an external "
                "scientific acceptance"
            ),
        },
        "plan": plan,
        "trial_outcomes": report["trials"],
        "scores": scores,
        "model_comparison": tables,
        "comparisons": comparisons,
        "family_size": family,
        "probabilistic": probabilistic,
        "spikes": spikes,
        "new_tasks": new_tasks(joined, probabilistic, spikes, family),
        "decisions": decisions,
        "resources": {
            "wall_seconds": report["wall_seconds"],
            "environment": report["environment"],
        },
        "limitations": [
            "Approximate retrospective +45d; first publication UNKNOWN",
            "T0-T3 reused, H0-H3 and September 2026 are not new tests",
            (
                "No strict historical PIT, train/serve promotion parity or "
                "independent forward/shadow"
            ),
            "Cohort64 is not citywide; origin00UTC confounds horizon and local hour",
            "Resource RSS is per-worker measured high-water, not OS memory quota",
            (
                "Research quantiles are projected to count support, "
                "calibration is measured not guaranteed"
            ),
            "No operational incident labels or hidden demand",
            "F8 remains PARTIAL; active pointers and production unchanged",
        ],
    }
    write_json(output, evidence)
    return output
