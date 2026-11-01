"""Independent post-audit metrics, source-feature checks and paired uncertainty."""

import importlib
import math
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import duckdb

from citibike.features.contracts import NY, SEED, TYPES, utc
from citibike.features.research import CATALOG, DERIVED, SCHEMA, A, B, enrich
from citibike.forecasting import models
from citibike.forecasting.artifacts import active, checked, create
from citibike.forecasting.experiment import promotion
from citibike.forecasting.feature_research import (
    assert_hashes,
    checked_protocol,
    read_frame,
    row_hash,
)
from citibike.forecasting.feature_research import models as feature_models
from citibike.forecasting.verify import diagnostic_oracle, oracle_file
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json

FRAME = [
    "fold",
    "station_id",
    "forecast_origin_utc",
    "target_hour_utc",
    "horizon_h",
    "prediction_type",
    "actual",
    "predicted",
    "weekday",
    "local_hour",
    "activity",
]
REPLAY = {
    "seasonal_naive": "seasonal_naive",
    "poisson_bounded__base": "poisson_bounded",
    "lightgbm_bounded__base": "lightgbm_bounded",
    "catboost_bounded__base": "catboost_bounded",
}
TRIAL_SEGMENTS = ("all", "peak_hour", "new_known")
TRIAL_PREFIXES = ("actual:", "activity:")


def feature_oracle(frame_path: Path, dense: Path, fit_asof: str) -> dict[str, int]:
    """Direct source summation per unique cutoff, separate from ASOF state builder."""
    pd = importlib.import_module("pandas")

    frame = read_frame(frame_path)
    checks = 0
    # Calendar computed from Python datetimes, independently of pandas feature code.
    for row in frame.itertuples(index=False):
        local = row.target_hour_utc.to_pydatetime().astimezone(NY)
        expect = {
            "week_of_year": local.isocalendar().week,
            "weekday_am_peak": int(local.weekday() < 5 and local.hour in (7, 8, 9)),
            "weekday_pm_peak": int(
                local.weekday() < 5 and local.hour in (16, 17, 18, 19)
            ),
        }
        expect["holiday_peak"] = row.holiday * (
            expect["weekday_am_peak"] + expect["weekday_pm_peak"]
        )
        for field, value, period in (
            ("local_hour", local.hour, 24),
            ("weekday", local.weekday(), 7),
            ("month_index", local.month - 1, 12),
        ):
            expect[field + "_sin"] = math.sin(2 * math.pi * value / period)
            expect[field + "_cos"] = math.cos(2 * math.pi * value / period)
        for column, value in expect.items():
            if not math.isclose(float(getattr(row, column)), value, abs_tol=1e-10):
                raise ValueError("Independent calendar feature mismatch")
            checks += 1
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        db.read_parquet(str(dense)).create_view("raw")
        monthly = db.execute("""WITH long AS (
            SELECT station_id,month_key,hour_utc,label_available_at,
                'departures' prediction_type,departures y FROM raw
            UNION ALL SELECT station_id,month_key,hour_utc,label_available_at,
                'arrivals',arrivals FROM raw)
            SELECT station_id,prediction_type,month_key,
                max(label_available_at) available,
                sum(y) total,count(y) n FROM long WHERE y IS NOT NULL
            GROUP BY station_id,prediction_type,month_key ORDER BY month_key""").df()
    groups = {
        (str(station), str(kind)): g
        for (station, kind), g in monthly.groupby(
            ["station_id", "prediction_type"], sort=True
        )
    }
    # Each distinct key has identical B values across its target-hour rows.
    keys = ["station_id", "prediction_type", "forecast_origin_utc", "month"]
    for key, rows in frame.groupby(keys, sort=True):
        station, kind, origin, target_month = key
        cutoff = min(origin, pd.Timestamp(fit_asof))
        available = groups.get((str(station), str(kind)))
        if available is None:
            selected = monthly.iloc[:0]
        else:
            selected = available[available["available"] <= cutoff]
        expected: dict[str, Any] = dict.fromkeys(B)
        if len(selected):
            latest = selected.iloc[-1]
            expected["published_month_mean"] = latest["total"] / latest["n"]
            expected["published_month_n"] = latest["n"]
            if len(selected) > 1:
                previous = selected.iloc[-2]
                previous_mean = previous["total"] / previous["n"]
                last_year, last_month = map(int, latest["month_key"].split("-"))
                prev_year, prev_month = map(int, previous["month_key"].split("-"))
                gap = (last_year - prev_year) * 12 + last_month - prev_month
                expected["prior_month_mean"] = previous_mean
                expected["monthly_trend"] = (
                    expected["published_month_mean"] - previous_mean
                ) / gap
            seasonal = selected[
                selected["month_key"].str[-2:].astype(int) == target_month
            ]
            if len(seasonal):
                expected["seasonal_month_n"] = seasonal["n"].sum()
                expected["seasonal_month_mean"] = (
                    seasonal["total"].sum() / seasonal["n"].sum()
                )
        for column, value in expected.items():
            series = rows[column]
            if value is None:
                if not series.isna().all():
                    raise ValueError("Missing source became numeric observation")
            elif (
                series.isna().any()
                or not series.map(
                    lambda item, expected=value: math.isclose(
                        float(item), float(expected), rel_tol=1e-9, abs_tol=1e-9
                    )
                ).all()
            ):
                raise ValueError(f"Independent published feature mismatch: {column}")
            checks += len(rows)
    with duckdb.connect() as db:
        invalid = db.execute(
            """SELECT count(*) FROM read_parquet(?) WHERE
            feature_available_at>forecast_origin_utc
            OR max_event_at>=forecast_origin_utc
            OR research_cutoff>forecast_origin_utc OR research_cutoff>?::TIMESTAMPTZ
            OR b_available_at>research_cutoff OR b_max_event_at>=research_cutoff""",
            [str(frame_path), fit_asof],
        ).fetchone()
        if invalid != (0,):
            raise ValueError("Independent feature availability violation")
    return {"rows": len(frame), "feature_values": checks, "mismatches": 0}


def reproduce_features(path: Path, dense: Path, fit_asof: str) -> dict[str, Any]:
    """Rebuild the full batch and a one-origin serving-shaped request exactly."""
    np = importlib.import_module("numpy")

    stored = read_frame(path)
    base = stored.drop(columns=[*A, *B, *DERIVED])
    origin = stored["forecast_origin_utc"].max()
    report: dict[str, Any] = {"rows": len(stored)}
    for label, request, expected in (
        ("full_batch", base, stored),
        (
            "one_origin_request",
            base[base["forecast_origin_utc"] == origin],
            stored[stored["forecast_origin_utc"] == origin],
        ),
    ):
        # Row order must not matter: a served batch arrives in caller order.
        again = enrich(request.iloc[::-1].copy(), dense, fit_asof)
        if len(again) != len(expected) or row_hash(again) != row_hash(expected):
            raise ValueError("Feature rebuild changed its row population")
        different = sum(
            not np.array_equal(
                again[column].to_numpy(float),
                expected[column].to_numpy(float),
                equal_nan=True,
            )
            for column in [*A, *B]
        )
        if different:
            raise ValueError("Features are not deterministic or batch independent")
        report[label] = {"rows": len(again), "different_columns": 0}
    return report


def paired(left: Any, right: Any, *, replicates: int = 500) -> dict[str, Any]:
    """Crossed cluster bootstrap on paired errors; station and day both resampled."""
    np = importlib.import_module("numpy")

    columns = [
        "fold",
        "station_id",
        "forecast_origin_utc",
        "target_hour_utc",
        "horizon_h",
        "prediction_type",
        "actual",
    ]
    left = left.sort_values(columns).reset_index(drop=True)
    right = right.sort_values(columns).reset_index(drop=True)
    if not left[columns].equals(right[columns]):
        raise ValueError("Paired populations differ")
    delta = (
        (left["predicted"] - left["actual"]).abs()
        - (right["predicted"] - right["actual"]).abs()
    ).to_numpy(float)
    stations, station_levels = left["station_id"].factorize(sort=True)
    days, day_levels = (
        left["target_hour_utc"].dt.tz_convert(NY).dt.date.factorize(sort=True)
    )
    folds = left["fold"].to_numpy(int)
    fold_levels = sorted(set(folds))
    # Collapse hours within station/day/fold; retain n for row-weighted fold means.
    data = left[["fold"]].copy()
    data["station"] = stations
    data["day"] = days
    data["delta"] = delta
    aggregates = (
        data.groupby(["fold", "station", "day"], sort=True)["delta"]
        .agg(["sum", "count"])
        .reset_index()
    )
    rng = np.random.default_rng(SEED)
    station_index = aggregates["station"].to_numpy(int)
    day_index = aggregates["day"].to_numpy(int)
    fold_index, _ = aggregates["fold"].factorize(sort=True)
    sums = aggregates["sum"].to_numpy(float)
    counts = aggregates["count"].to_numpy(float)
    boot = []
    for _ in range(replicates):
        sw = rng.multinomial(
            len(station_levels), np.ones(len(station_levels)) / len(station_levels)
        )
        dw = rng.multinomial(
            len(day_levels), np.ones(len(day_levels)) / len(day_levels)
        )
        w = sw[station_index] * dw[day_index]
        denominators = np.bincount(fold_index, weights=w * counts)
        numerators = np.bincount(fold_index, weights=w * sums)
        if (denominators > 0).all():
            boot.append(float(np.mean(numerators / denominators)))
    differences = [float(delta[folds == f].mean()) for f in fold_levels]
    return {
        "macro_MAE_difference_left_minus_right": float(np.mean(differences)),
        "fold_differences": differences,
        "folds_left_better": sum(value < 0 for value in differences),
        "percentile_95": np.quantile(boot, [0.025, 0.975]).tolist() if boot else None,
        "replicates": len(boot),
        "seed": SEED,
        "stations": len(station_levels),
        "NY_target_days": len(day_levels),
        "rows": len(left),
        "method": "crossed station x target-day pigeonhole, paired weights; "
        "fixed validation population; exploratory, no multiple-search correction",
    }


def group_verdict(comparison: dict[str, Any]) -> str:
    """The frozen group-effect rule: interval and fold majority must agree."""
    interval = comparison["percentile_95"]
    if interval is None:
        return "inconclusive"
    folds = len(comparison["fold_differences"])
    better = comparison["folds_left_better"]
    if interval[1] < 0 and better > folds / 2:
        return "improves"
    if interval[0] > 0 and folds - better > folds / 2:
        return "degrades"
    return "inconclusive"


def passes(gate: dict[str, Any]) -> bool:
    return bool(
        gate["majority"]
        and not gate["degraded_segments"]
        and gate["resources_and_fit_ok"]
    )


def decide(scores: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    """Validation-only rule from the protocol; final scores never reach this code."""
    rows = {(s["population"], s["variant"]): s for s in scores if s["target"] == kind}
    populations = sorted({population for population, _ in rows})
    eligible, blockers = [], {}
    for name in sorted({variant for _, variant in rows} - {"seasonal_naive"}):
        failed = {
            population: {
                "wins": rows[(population, name)]["gate"]["wins"],
                "windows": rows[(population, name)]["gate"]["windows"],
                "degraded_segments": len(
                    rows[(population, name)]["gate"]["degraded_segments"]
                ),
                "resources_and_fit_ok": rows[(population, name)]["gate"][
                    "resources_and_fit_ok"
                ],
            }
            if (population, name) in rows
            else "no complete score"
            for population in populations
            if (population, name) not in rows
            or not passes(rows[(population, name)]["gate"])
        }
        if failed:
            blockers[name] = failed
        else:
            eligible.append((rows[("cohort64", name)]["macro_mae"], name))
    return {
        "target": kind,
        "research_candidate": min(eligible)[1] if eligible else "seasonal_naive",
        "eligible": [name for _, name in sorted(eligible)],
        "blockers": blockers,
        "lowest_macro_mae": {
            population: min(
                (s["macro_mae"], name)
                for (p, name), s in rows.items()
                if p == population
            )[1]
            for population in populations
        },
        "operational": "unchanged baseline; strict/forward NOT VERIFIED",
    }


def station_metrics(path: Path) -> list[dict[str, Any]]:
    with duckdb.connect() as db:
        cur = db.execute(
            """SELECT station_id,count(*) n,avg(actual) actual_mean,
            avg((actual=0)::INT) zero_fraction,avg(abs(predicted-actual)) mae,
            sum(abs(predicted-actual))/nullif(sum(actual),0) wape,
            sqrt(avg(pow(predicted-actual,2))) rmse,avg(predicted-actual) bias,
            avg(abs(predicted-actual)) FILTER(WHERE actual>=20) outcome_peak_mae,
            avg(abs(predicted-actual)) FILTER(WHERE weekday<5 AND local_hour IN
                (7,8,9,16,17,18,19)) predefined_peak_mae
            FROM read_parquet(?) GROUP BY 1 ORDER BY 1""",
            [str(path)],
        )
        names = [c[0] for c in cur.description]
        return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def frame_metrics(part: Any) -> dict[str, Any]:
    error = part["predicted"] - part["actual"]
    peak = (part["weekday"] < 5) & part["local_hour"].isin([7, 8, 9, 16, 17, 18, 19])
    tail = part["actual"] >= 20
    total = float(part["actual"].sum())
    return {
        "n": len(part),
        "stations": int(part["station_id"].nunique()),
        "zero_fraction": round(float((part["actual"] == 0).mean()), 6),
        "mae": round(float(error.abs().mean()), 6),
        "wape": round(float(error.abs().sum()) / total, 6) if total else None,
        "rmse": round(float(((error**2).mean()) ** 0.5), 6),
        "bias": round(float(error.mean()), 6),
        "peak_hour_mae": round(float(error[peak].abs().mean()), 6)
        if bool(peak.any())
        else None,
        "actual_ge20_n": int(tail.sum()),
        "actual_ge20_mae": round(float(error[tail].abs().mean()), 6)
        if bool(tail.any())
        else None,
    }


def macro(rows: list[dict[str, Any]], segment: str, field: str) -> float | None:
    values = [
        r["metrics"][segment][field]
        for r in rows
        if segment in r["metrics"] and r["metrics"][segment][field] is not None
    ]
    return round(mean(values), 6) if values else None


def station_stability(frame: Any, reference: Any) -> dict[str, int]:
    def errors(data: Any) -> Any:
        return (
            (data["predicted"] - data["actual"])
            .abs()
            .groupby(data["station_id"])
            .mean()
        )

    difference = errors(frame) - errors(reference)
    return {
        "stations": len(difference),
        "better": int((difference < 0).sum()),
        "worse": int((difference > 0).sum()),
    }


def frozen_replay(
    root: Path, plan: dict[str, Any], grouped: dict[tuple[str, str, str], Any]
) -> list[dict[str, Any]]:
    """Base variants must reproduce the frozen F4 validation predictions."""
    pd = importlib.import_module("pandas")

    experiment = active(root, "experiments")
    report = checked(experiment)
    if report["dataset_id"] != plan["populations"]["cohort64"]["dataset_id"]:
        raise ValueError("Frozen replay dataset mismatch")
    keys = ["fold", "station_id", "forecast_origin_utc", "horizon_h"]
    rows = []
    for kind in TYPES:
        for name, frozen_name in REPLAY.items():
            frame = grouped.get(("cohort64", kind, name))
            if frame is None:
                continue
            frozen = []
            for result in report["results"]:
                if (
                    result["model_name"] == frozen_name
                    and result["prediction_type"] == kind
                ):
                    part = read_frame(experiment / result["predictions"])
                    part["fold"] = result["fold"]
                    frozen.append(part[[*keys, "predicted"]])
            merged = frame[[*keys, "predicted"]].merge(
                pd.concat(frozen, ignore_index=True),
                on=keys,
                how="outer",
                suffixes=("", "_frozen"),
                indicator=True,
            )
            if bool((merged["_merge"] != "both").any()):
                raise ValueError("Frozen replay row membership mismatch")
            delta = (merged["predicted"] - merged["predicted_frozen"]).abs()
            rows.append(
                {
                    "target": kind,
                    "variant": name,
                    "frozen_model": frozen_name,
                    "rows": len(merged),
                    "exact_rows": int((delta == 0).sum()),
                    "max_absolute_difference": float(delta.max()),
                }
            )
    return rows


def superseded_replay(
    root: Path, plan: dict[str, Any], trials: list[dict[str, Any]]
) -> dict[str, Any]:
    """Refit equality against the earlier run of the same model inputs."""
    old = plan["superseded"]
    if not old:
        return {"status": "not_applicable"}
    summary = checked(root / "f4/feature_outcomes" / old["outcome"])
    current = {
        tuple(
            t["request"][k] for k in ("population", "fold", "prediction_type", "name")
        ): t.get("prediction_values_hash")
        for t in trials
        if "request" in t
    }
    compared, different = 0, []
    for item in summary["trials"]:
        trial = read_json(Path(item["path"]) / "manifest.json")
        request = trial["request"]
        key = tuple(
            request[k] for k in ("population", "fold", "prediction_type", "name")
        )
        compared += 1
        if current.get(key) != trial["prediction_values_hash"]:
            different.append(key)
    return {
        "status": "compared",
        "protocol": old["protocol"],
        "outcome": old["outcome"],
        "reason": old["reason"],
        "compared_trials": compared,
        "identical_prediction_hashes": compared - len(different),
        "different": different,
    }


def root_cause_summary(plan: dict[str, Any]) -> dict[str, Any]:
    path = Path(plan["diagnostics"])
    rows: Any = read_json(path / "diagnostics.json")
    return {
        "artifact": str(path),
        "manifest_sha256": plan["diagnostics_manifest_sha256"],
        "hypotheses": plan["hypotheses"],
        "post_hoc_note": "actual>=20 is an outcome slice; not miscalibration evidence",
        "rows": [
            {
                "fold": r["fold"],
                "target": r["target"],
                "model": r["model"],
                **{
                    label: {
                        k: round(r["metrics"][segment][k], 4)
                        for k in ("n", "mae", "bias")
                    }
                    for label, segment in (
                        ("all", "all"),
                        ("peak_hour", "peak_hour"),
                        ("actual_ge20", "actual:peak_ge20"),
                    )
                    if segment in r["metrics"]
                },
                "train_mean": round(r["train"]["actual_mean"], 4),
                "validation_mean": round(r["validation"]["actual_mean"], 4),
                "count_bin_TV": round(r["train_validation_count_bin_TV"], 4),
                "concentration": r["concentration"],
                "coverage_shift": r["coverage_shift"],
                "stations": {
                    station: {
                        "eligible_before_block_sampling": v[
                            "eligible_before_block_sampling"
                        ],
                        "train_n": v["train"]["n"],
                        "train_mean": v["train"].get("actual_mean"),
                        "validation_mean": v["validation"].get("actual_mean"),
                        "mae": v["metrics"].get("all", {}).get("mae"),
                        "bias": v["metrics"].get("all", {}).get("bias"),
                    }
                    for station, v in r["stations"].items()
                },
            }
            for r in rows
        ],
    }


def verify(result_path: Path, output: Path) -> Path:
    pd = importlib.import_module("pandas")

    summary = checked(result_path)
    protocol_path = Path(summary["protocol"])
    protocol = checked_protocol(protocol_path)
    plan = protocol["plan"]
    root = result_path.parents[2]
    if summary["protocol_id"] != protocol["id"] or summary["final_test_accessed"]:
        raise ValueError("Mixed protocol/test access")
    audits = []
    reproduction = []
    final_rows = 0
    for label, population in plan["populations"].items():
        inputs = Path(population["inputs"])
        dense = Path(population["dataset"]) / "dense.parquet"
        final_start = checked(Path(population["dataset"]))["config"]["final"][0]
        for split in checked(inputs)["splits"]:
            for field in ("train_file", "evaluation_file"):
                file = inputs / split[field]
                audit = feature_oracle(file, dense, split["fit_asof"])
                audits.append({"file": str(file), **audit})
                reproduction.append(
                    {
                        "population": label,
                        "file": str(file),
                        **reproduce_features(file, dense, split["fit_asof"]),
                    }
                )
                with duckdb.connect() as db:
                    count = db.execute(
                        "SELECT count(*) FROM read_parquet(?) "
                        "WHERE target_hour_utc>=?::TIMESTAMPTZ",
                        [str(file), final_start + "T00:00:00+00:00"],
                    ).fetchone()
                assert count is not None
                final_rows += count[0]
    if final_rows:
        raise ValueError("Final-window rows reached research inputs")
    trials: list[dict[str, Any]] = []
    full_frames: dict[tuple[str, str, str], list[Any]] = {}
    oracle_rows = 0
    station_reports: list[dict[str, Any]] = []
    for item in summary["trials"]:
        if item["status"] == "not_run":
            trials.append(item)
            continue
        path = Path(item["path"])
        assert_hashes({str(path / "manifest.json"): item["sha256"]})
        result = checked(path)
        request = result["request"]
        if request["protocol_id"] != protocol["id"] or result["final_test_accessed"]:
            raise ValueError("Trial protocol/test isolation mismatch")
        if utc(result["created_at"]) < utc(protocol["created_at"]):
            raise ValueError("Trial preceded protocol")
        if result["status"] == "success":
            predictions = path / result["predictions"]
            oracle_rows += oracle_file(predictions, result["metrics"])
            diagnostic_oracle(predictions, result["diagnostics"])
            kind = request["prediction_type"]
            with duckdb.connect() as db:
                keys = "station_id,forecast_origin_utc,target_hour_utc,horizon_h,actual"
                db.read_parquet(str(predictions)).create_view("p")
                db.read_parquet(request["evaluation_file"]).create_view("reference_all")
                db.sql(
                    f"SELECT * FROM reference_all WHERE prediction_type='{kind}'"
                ).create_view("reference")
                for left, right in (("p", "reference"), ("reference", "p")):
                    if db.execute(
                        f"SELECT count(*) FROM(SELECT {keys} FROM {left} "
                        f"EXCEPT ALL SELECT {keys} FROM {right})"
                    ).fetchone() != (0,):
                        raise ValueError("Independent same-row membership failure")
                if db.execute(
                    "SELECT count(*) FROM read_parquet(?) WHERE "
                    "label_available_at>?::TIMESTAMPTZ OR "
                    "target_hour_utc>=?::TIMESTAMPTZ-INTERVAL '24 hours'",
                    [request["train_file"], request["fit_asof"], request["fit_asof"]],
                ).fetchone() != (0,):
                    raise ValueError("Independent train availability/embargo failure")
            frame = read_frame(predictions)
            fitted = models.load(path / "model.joblib")
            expected_names = feature_models.names(
                request["parameters"].get("research_groups", [])
            )
            if fitted["feature_names"] != expected_names:
                raise ValueError("Ablation feature matrix mismatch")
            if (
                feature_models.predict(fitted, frame.head(48))[0]
                != frame.head(48)["predicted"].tolist()
            ):
                raise ValueError("Independent saved-model parity failure")
            frame["fold"] = request["fold"]
            key = (request["population"], kind, request["name"])
            full_frames.setdefault(key, []).append(frame[FRAME].copy())
            station_reports.append(
                {
                    "trial": str(path),
                    "population": request["population"],
                    "fold": request["fold"],
                    "target": kind,
                    "variant": request["name"],
                    "stations": station_metrics(predictions),
                }
            )
            result["extra_critical"] = {
                "station:" + r["station_id"]: r
                for r in station_reports[-1]["stations"]
                if "station:" + r["station_id"] in plan["critical_extra"]
            }
            if "peak_hour" in result["metrics"]:
                result["extra_critical"]["peak_hour"] = result["metrics"]["peak_hour"]
        trials.append(
            {
                "path": str(path),
                "sha256": item["sha256"],
                "request": request,
                **{
                    key: result.get(key)
                    for key in (
                        "status",
                        "error",
                        "metrics",
                        "coverage",
                        "measurements",
                        "extra_critical",
                        "prediction_values_hash",
                    )
                },
            }
        )
    verifier = checksum(Path(__file__))
    # Per-station outcomes remain ignored and immutable.
    detail_path = create(
        root,
        "feature_audits",
        identity(["audit", summary["id"], plan["code"], verifier]),
        lambda stage: save_details(stage, station_reports),
        activate=False,
    )
    comparisons, group_effects, scores, decisions = [], [], [], []
    grouped = {
        key: pd.concat(frames, ignore_index=True) for key, frames in full_frames.items()
    }
    for population in plan["populations"]:
        for kind in TYPES:
            baseline = [
                r
                for r in trials
                if r.get("request", {}).get("population") == population
                and r.get("request", {}).get("prediction_type") == kind
                and r.get("request", {}).get("name") == "seasonal_naive"
                and r["status"] == "success"
            ]
            candidates = []
            for name in plan["variants"]:
                rows = [
                    r
                    for r in trials
                    if r.get("request", {}).get("population") == population
                    and r.get("request", {}).get("prediction_type") == kind
                    and r.get("request", {}).get("name") == name
                    and r["status"] == "success"
                ]
                if len(rows) != len(plan["windows"]) or len(baseline) != len(rows):
                    continue
                converted = [{**r, "fold": r["request"]["fold"]} for r in rows]
                base_converted = [{**r, "fold": r["request"]["fold"]} for r in baseline]
                gate_result = promotion(
                    converted, base_converted, strict_validation=False
                )
                for r, b in zip(rows, baseline, strict=True):
                    for segment, metric in r.get("extra_critical", {}).items():
                        reference_metric = b.get("extra_critical", {}).get(segment)
                        if (
                            reference_metric
                            and metric["n"] >= 100
                            and metric["mae"] > 1.2 * reference_metric["mae"] + 1e-9
                        ):
                            gate_result["degraded_segments"].append(
                                f"{r['request']['fold']}:{segment}"
                            )
                values = [r["metrics"]["all"]["mae"] for r in rows]
                key = (population, kind, name)
                score = {
                    "population": population,
                    "target": kind,
                    "variant": name,
                    "macro_mae": mean(values),
                    "fold_mae": values,
                    "fold_std": pstdev(values),
                    "macro_wape": macro(rows, "all", "wape"),
                    "macro_rmse": macro(rows, "all", "rmse"),
                    "macro_bias": macro(rows, "all", "bias"),
                    "fold_bias": [r["metrics"]["all"]["bias"] for r in rows],
                    "macro_peak_hour_mae": macro(rows, "peak_hour", "mae"),
                    "macro_peak_hour_bias": macro(rows, "peak_hour", "bias"),
                    "macro_actual_ge20_mae": macro(rows, "actual:peak_ge20", "mae"),
                    "macro_actual_ge20_bias": macro(rows, "actual:peak_ge20", "bias"),
                    "macro_zero_mae": macro(rows, "actual:zero", "mae"),
                    "zero_fraction": round(
                        mean(r["coverage"]["zero_fraction"] for r in rows), 6
                    ),
                    "evaluation_rows": sum(r["metrics"]["all"]["n"] for r in rows),
                    "horizon_macro_mae": [
                        macro(rows, f"horizon:{h}", "mae") for h in range(1, 25)
                    ],
                    "activity_macro_mae": {
                        a: macro(rows, f"activity:{a}", "mae")
                        for a in ("low", "middle", "high", "new")
                    },
                    "season_macro_mae": {
                        s: macro(rows, f"season:{s}", "mae")
                        for s in ("winter", "spring", "summer", "autumn")
                    },
                    "runtime": {
                        "max_fit_seconds": max(
                            r["measurements"]["fit_seconds"] for r in rows
                        ),
                        "max_inference_seconds": max(
                            r["measurements"]["inference_seconds"] for r in rows
                        ),
                        "mean_worker_wall_seconds": mean(
                            r["measurements"]["worker_wall_seconds"] for r in rows
                        ),
                        "max_peak_rss_bytes": max(
                            r["measurements"]["peak_rss_bytes"] for r in rows
                        ),
                    },
                    "gate": gate_result,
                }
                if ("seasonal_naive" in name) is False and (
                    population,
                    kind,
                    "seasonal_naive",
                ) in grouped:
                    score["stations_vs_seasonal_naive"] = station_stability(
                        grouped[key], grouped[(population, kind, "seasonal_naive")]
                    )
                scores.append(score)
                if name != "seasonal_naive" and passes(gate_result):
                    candidates.append((score["macro_mae"], name))
                references = ["seasonal_naive"]
                if name.endswith("__A+B"):
                    references += [
                        name.replace("__A+B", "__A"),
                        name.replace("__A+B", "__B"),
                    ]
                if "__" in name and not name.endswith("__base"):
                    references += [name.split("__")[0] + "__base"]
                for reference in references:
                    other = (population, kind, reference)
                    if key == other or key not in grouped or other not in grouped:
                        continue
                    comparison = {
                        "population": population,
                        "target": kind,
                        "left": name,
                        "right": reference,
                        **paired(grouped[key], grouped[other]),
                    }
                    comparisons.append(comparison)
                    if reference != "seasonal_naive":
                        group_effects.append(
                            {
                                **{
                                    k: comparison[k]
                                    for k in ("population", "target", "left", "right")
                                },
                                "verdict": group_verdict(comparison),
                            }
                        )
            decisions.append(
                {
                    "population": population,
                    "target": kind,
                    "validation_candidate": min(candidates)[1]
                    if candidates
                    else "seasonal_naive",
                    "operational": "unchanged baseline; strict/forward NOT VERIFIED",
                }
            )
    expansion = []
    original = set(plan["populations"]["cohort64"]["stations"])
    for (population, kind, name), frame in grouped.items():
        if population != "cohort128":
            continue
        baseline_frame = grouped.get((population, kind, "seasonal_naive"))
        for label, inside in (("overlap64", True), ("added64", False)):
            part = frame[frame["station_id"].isin(original) == inside]
            if part.empty:
                continue
            entry: dict[str, Any] = {
                "population": label,
                "target": kind,
                "variant": name,
                **frame_metrics(part),
                "metrics_by_fold": [
                    {"fold": int(f), **frame_metrics(g)}
                    for f, g in part.groupby("fold", sort=True)
                ],
                "metrics_by_activity": {
                    str(a): frame_metrics(g)
                    for a, g in part.groupby("activity", sort=True)
                },
            }
            if baseline_frame is not None and name != "seasonal_naive":
                entry["paired_against_seasonal_naive"] = paired(
                    part,
                    baseline_frame[
                        baseline_frame["station_id"].isin(original) == inside
                    ],
                )
            original_frame = grouped.get(("cohort64", kind, name))
            if inside and original_frame is not None:
                entry["paired_against_original_training"] = paired(part, original_frame)
            expansion.append(entry)
    assert_hashes(plan["frozen_files"])
    report = {
        "version": plan["version"],
        "status": summary["status"],
        "result": str(result_path),
        "result_manifest_sha256": checksum(result_path / "manifest.json"),
        "protocol": str(protocol_path),
        "protocol_id": protocol["id"],
        "protocol_created_at": protocol["created_at"],
        "protocol_manifest_sha256": checksum(protocol_path / "manifest.json"),
        "verifier_sha256": verifier,
        "code": plan["code"],
        "environment": plan["environment"],
        "seed": plan["seed"],
        "windows": plan["windows"],
        "primary_metric": plan["primary_metric"],
        "decision_rule": plan["decision_rule"],
        "group_effect_rule": plan["group_effect_rule"],
        "tuning": plan["tuning"],
        "availability": plan["history"],
        "test_policy": plan["test_policy"],
        "evaluation_weather": plan["evaluation_weather"],
        "root_cause": root_cause_summary(plan),
        "schema": SCHEMA,
        "catalog": CATALOG,
        "populations": plan["populations"],
        "budgets": plan["budgets"],
        "outcome_counts": summary["outcome_counts"],
        "cumulative_worker_seconds": summary["cumulative_worker_seconds"],
        "trials": [compact_trial(t) for t in trials],
        "scores": scores,
        "paired_comparisons": comparisons,
        "group_effects": group_effects,
        "expanded_populations": expansion,
        "decisions": decisions,
        "target_decisions": [decide(scores, kind) for kind in TYPES],
        "frozen_replay": frozen_replay(root, plan, grouped),
        "superseded_replay": superseded_replay(root, plan, trials),
        "independent_audit": {
            "prediction_rows": oracle_rows,
            "feature_inputs": audits,
            "feature_reproduction": reproduction,
            "final_window_input_rows": final_rows,
            "metric_mismatches": 0,
            "PIT_mismatches": 0,
            "protected_files": len(plan["frozen_files"]),
            "station_metrics": str(detail_path),
            "station_metrics_manifest_sha256": checksum(detail_path / "manifest.json"),
        },
        "final_test_accessed": False,
        "operational_promotion": False,
        "limitations": [
            "+45d is approximate, historical first publication unknown",
            "September 2026 was already used; no new independent final test",
            "No strict historical PIT / forward / shadow evidence created",
            "Post-hoc actual>=20 slice is not operational miscalibration",
            "Crossed bootstrap exploratory; no multiple-comparison correction",
            "One seed; fold variance only, not optimiser seed variance",
            "Cohort64/128 are different populations; no citywide claim",
            "RSS is process high-water, not OS quota; shared host is not SLA",
        ],
    }
    write_json(output, report)
    return output


def save_details(stage: Path, stations: list[dict[str, Any]]) -> dict[str, Any]:
    write_json(stage / "station-metrics.json", stations)
    return {"kind": "feature_independent_station_metrics", "trials": len(stations)}


def rounded(metric: dict[str, Any]) -> dict[str, Any]:
    return {
        k: round(v, 6) if isinstance(v, float) else v
        for k, v in metric.items()
        if k != "availability_mode"
    }


def compact_trial(trial: dict[str, Any]) -> dict[str, Any]:
    return {
        **{
            k: trial[k]
            for k in ("path", "sha256", "status", "error", "measurements", "coverage")
            if k in trial
        },
        "request": {
            k: trial["request"][k]
            for k in (
                "population",
                "fold",
                "prediction_type",
                "name",
                "evaluation_rows_hash",
            )
        },
        "metrics": {
            k: rounded(v)
            for k, v in (trial.get("metrics") or {}).items()
            if k in TRIAL_SEGMENTS or k.startswith(TRIAL_PREFIXES)
        },
    }
