"""Independent stdlib error oracle, split audit, Silver labels and serving proof."""

import math
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import httpx

from citibike.analytics.build import configured
from citibike.features.contracts import MODE, SCHEMA, utc
from citibike.forecasting.artifacts import active, checked
from citibike.forecasting.evaluation_weather import POLICY, summarize
from citibike.ingestion.archive import checksum
from citibike.storage import read_json, write_json


def oracle_file(path: Path, expected: dict[str, Any]) -> int:
    # Deliberately independent of forecasting.metrics / pandas / sklearn.
    accum: dict[str, list[float]] = {}
    total = 0
    research_slices = any(k.startswith("actual:") for k in expected)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        cursor = db.execute("SELECT * FROM read_parquet(?)", [str(path)])
        names = [c[0] for c in cursor.description]
        while batch := cursor.fetchmany(4096):
            for values in batch:
                row = dict(zip(names, values, strict=True))
                actual, pred = float(row["actual"]), float(row["predicted"])
                if not math.isfinite(pred) or pred < 0:
                    raise ValueError("Invalid independent output")
                local = row["target_hour_utc"].astimezone(ZoneInfo("America/New_York"))
                if row["weekday"] != local.weekday() or row["local_hour"] != local.hour:
                    raise ValueError("Independent calendar mismatch")
                if (
                    row["feature_available_at"] > row["forecast_origin_utc"]
                    or row["max_event_at"] >= row["forecast_origin_utc"]
                ):
                    raise ValueError("Independent future feature audit failed")
                error = pred - actual
                if not math.isclose(error, float(row["residual"]), abs_tol=1e-10):
                    raise ValueError("Residual mismatch")
                labels = [
                    "all",
                    f"horizon:{row['horizon_h']}",
                    f"activity:{row['activity']}",
                    "weekday" if local.weekday() < 5 else "weekend",
                ]
                season = (
                    "winter"
                    if local.month in (12, 1, 2)
                    else "spring"
                    if local.month in (3, 4, 5)
                    else "summer"
                    if local.month in (6, 7, 8)
                    else "autumn"
                )
                labels.append("season:" + season)
                if research_slices:
                    labels += ["actual:zero" if actual == 0 else "actual:nonzero"]
                    if 1 <= actual <= 4:
                        labels.append("actual:rare_1_4")
                    elif 5 <= actual <= 19:
                        labels.append("actual:middle_5_19")
                    elif actual >= 20:
                        labels.append("actual:peak_ge20")
                    labels.append("target_month:" + local.strftime("%Y-%m"))
                if row["new_known"]:
                    labels.append("new_known")
                if row["holiday"]:
                    labels.append("holiday")
                for name in ("cold", "heat", "heavy_rain", "observed"):
                    if row.get("eval_weather_" + name):
                        labels.append("weather:" + name)
                if local.weekday() < 5 and local.hour in (7, 8, 9, 16, 17, 18, 19):
                    labels.append("peak_hour")
                # Independently derive US DST transition Sundays.
                if local.weekday() == 6 and (
                    (local.month == 3 and 8 <= local.day <= 14)
                    or (local.month == 11 and local.day <= 7)
                ):
                    labels.append("dst_transition")
                for label in labels:
                    a = accum.setdefault(label, [0, 0, 0, 0, 0])
                    a[0] += 1
                    a[1] += actual
                    a[2] += abs(error)
                    a[3] += error * error
                    a[4] += error
                total += 1
    if set(accum) != set(expected):
        raise ValueError("Independent metric segment mismatch")
    for label, (n, actual_sum, absolute, squares, bias) in accum.items():
        metric_values = {
            "n": n,
            "actual_sum": actual_sum,
            "mae": absolute / n,
            "wape": absolute / actual_sum if actual_sum else None,
            "rmse": math.sqrt(squares / n),
            "bias": bias / n,
        }
        for key, value in metric_values.items():
            wanted = expected[label][key]
            if value is None:
                if wanted is not None:
                    raise ValueError("WAPE zero denominator mismatch")
            elif not math.isclose(value, wanted, rel_tol=1e-8, abs_tol=1e-8):
                raise ValueError(f"Independent {label}/{key} metric mismatch")
    return total


def diagnostic_oracle(path: Path, expected: dict[str, Any]) -> int:
    """SQL audit of pandas diagnostics; no call to metrics/error_analysis."""
    checked_groups = 0
    with duckdb.connect() as db:
        db.read_parquet(str(path)).create_view("predictions")
        for field in ("station_id", "local_hour"):
            rows = db.execute(f"""SELECT {field}::VARCHAR,count(*),sum(actual),
                avg(abs(predicted-actual)),
                sqrt(avg(pow(predicted-actual,2))),avg(predicted-actual)
                FROM predictions GROUP BY 1 ORDER BY 4 DESC,1""").fetchall()
            by_key = {r[0]: r for r in rows}
            if len(expected[field]) != min(10, len(rows)):
                raise ValueError("Diagnostic top-group coverage mismatch")
            for given in expected[field]:
                row = by_key[given["key"]]
                values = dict(
                    zip(
                        ("n", "actual_sum", "mae", "rmse", "bias"), row[1:], strict=True
                    )
                )
                if any(
                    not math.isclose(
                        float(given[k]), float(v), rel_tol=1e-8, abs_tol=1e-8
                    )
                    for k, v in values.items()
                ):
                    raise ValueError("Independent station/hour error mismatch")
                if given["mae"] + 1e-9 < rows[min(9, len(rows) - 1)][3]:
                    raise ValueError("Diagnostic top group not maximal")
                checked_groups += 1
        histogram = dict(
            db.execute("""SELECT CASE WHEN actual=0 THEN '0'
            WHEN actual=1 THEN '1' WHEN actual<5 THEN '2-4'
            WHEN actual<10 THEN '5-9' WHEN actual<20 THEN '10-19'
            ELSE '20+' END bin,count(*) FROM predictions GROUP BY 1""").fetchall()
        )
        if any(
            histogram.get(k, 0) != v for k, v in expected["actual_histogram"].items()
        ):
            raise ValueError("Independent count distribution mismatch")
        maximum = db.execute(
            "SELECT max(abs(predicted-actual)) FROM predictions"
        ).fetchone()
        for row in expected["largest_residuals"]:
            found = db.execute(
                """SELECT actual,predicted,residual FROM predictions
                WHERE station_id=? AND forecast_origin_utc=?::TIMESTAMPTZ
                AND target_hour_utc=?::TIMESTAMPTZ AND horizon_h=?""",
                [
                    row["station_id"],
                    row["forecast_origin_utc"],
                    row["target_hour_utc"],
                    int(row["horizon_h"]),
                ],
            ).fetchall()
            if len(found) != 1 or any(
                not math.isclose(float(row[k]), v, rel_tol=1e-8, abs_tol=1e-8)
                for k, v in zip(
                    ("actual", "predicted", "residual"), found[0], strict=True
                )
            ):
                raise ValueError("Independent largest residual mismatch")
        if (
            maximum
            and expected["largest_residuals"]
            and not math.isclose(
                abs(float(expected["largest_residuals"][0]["residual"])),
                maximum[0],
                rel_tol=1e-8,
                abs_tol=1e-8,
            )
        ):
            raise ValueError("Largest residual not maximal")
    return checked_groups


def research_evidence(root: Path) -> dict[str, Any]:
    """Audit new validation only; do not select/publish/open final-test outcomes."""
    from statistics import mean, pstdev

    from citibike.features.contracts import TYPES
    from citibike.forecasting import models
    from citibike.forecasting.experiment import checked_protocol, promotion

    summary_path = active(root, "research")
    summary = checked(summary_path)
    protocol = checked_protocol(Path(summary["protocol_path"]))
    plan = protocol["plan"]
    inputs_path = Path(summary["inputs_path"])
    inputs = checked(inputs_path)
    reports = []
    oracle_rows = 0
    diagnostic_groups = 0
    for item in summary["trials"]:
        if item["status"] == "not_run":
            reports.append(item)
            continue
        path = Path(item["path"])
        trial = checked(path)
        request = trial["request"]
        if (
            request["protocol_id"] != protocol["id"]
            or request["dataset_id"] != plan["dataset_id"]
        ):
            raise ValueError("Mixed research trial provenance")
        if utc(trial["created_at"]) < utc(protocol["created_at"]):
            raise ValueError("Trial preceded frozen protocol")
        if trial["final_test_accessed"] or trial["status"] != item["status"]:
            raise ValueError("Research final/state audit mismatch")
        if trial["status"] == "success":
            predictions = path / trial["predictions"]
            oracle_rows += oracle_file(predictions, trial["metrics"])
            diagnostic_groups += diagnostic_oracle(predictions, trial["diagnostics"])
            split = inputs["splits"][trial["fold"]]
            if (
                trial["metrics"]["all"]["n"]
                != split["populations"][trial["prediction_type"]]["evaluation_rows"]
            ):
                raise ValueError("Research sample coverage mismatch")
            if (
                trial["evaluation_rows_hash"]
                != split["populations"][trial["prediction_type"]][
                    "evaluation_rows_hash"
                ]
            ):
                raise ValueError("Research evaluation population hash mismatch")
            with duckdb.connect() as db:
                db.execute("SET TimeZone='UTC'")
                db.read_parquet(str(predictions)).create_view("predictions")
                db.read_parquet(request["evaluation_file"]).create_view("reference")
                # EXCEPT ALL, not the executor's serialized row-hash helper.
                columns = (
                    "station_id,forecast_origin_utc,target_hour_utc,horizon_h,actual"
                )
                for left, right in (
                    ("predictions", "reference"),
                    ("reference", "predictions"),
                ):
                    value = db.execute(
                        f"""SELECT count(*) FROM(SELECT {columns} FROM {left}
                        WHERE prediction_type=? EXCEPT ALL SELECT {columns} FROM {right}
                        WHERE prediction_type=?)""",
                        [trial["prediction_type"]] * 2,
                    ).fetchone()
                    if value != (0,):
                        raise ValueError("Independent research row membership mismatch")
                train = db.execute(
                    """SELECT count(*) FROM read_parquet(?)
                    WHERE feature_available_at>forecast_origin_utc
                    OR max_event_at>=forecast_origin_utc OR label_available_at>?
                    OR target_hour_utc+INTERVAL '1 hour'>=?::TIMESTAMPTZ""",
                    [
                        request["train_file"],
                        utc(split["fit_asof"]),
                        split["evaluation_target_min"],
                    ],
                ).fetchone()
                if train != (0,):
                    raise ValueError(
                        "Independent research train availability/embargo failure"
                    )
                frame = db.execute(
                    "SELECT * FROM reference WHERE prediction_type=? "
                    "ORDER BY station_id,forecast_origin_utc,horizon_h LIMIT 32",
                    [trial["prediction_type"]],
                ).df()
                expected = db.execute(
                    "SELECT predicted FROM predictions "
                    "ORDER BY station_id,forecast_origin_utc,horizon_h LIMIT 32"
                ).fetchall()
            fitted = models.load(path / trial["model_file"])
            excluded = {
                f
                for group in request["parameters"].get("drop_groups", [])
                for f in plan["catalog_groups"][group]
            }
            if (
                excluded & set(fitted["feature_names"])
                or "horizon_h" not in fitted["feature_names"]
            ):
                raise ValueError("Ablated features entered model matrix")
            values, _ = models.predict(fitted, frame)
            if any(
                not math.isclose(v, e[0], abs_tol=1e-10, rel_tol=1e-10)
                for v, e in zip(values, expected, strict=True)
            ):
                raise ValueError("Independent saved-model parity mismatch")
            budget = plan["budgets"]
            if (
                trial["measurements"]["fit_seconds"] > budget["fit_seconds"]
                or trial["measurements"]["peak_rss_bytes"] > budget["peak_rss_bytes"]
            ):
                raise ValueError("Successful trial exceeded resource gate")
        elif not trial.get("error"):
            raise ValueError("Failed trial error outcome missing")
        reports.append({"path": str(path), **trial})
    full = [
        r
        for r in reports
        if r.get("request", {}).get("purpose") in ("ablation", "bounded_search")
    ]
    expected_count = len(plan["windows"]) * len(TYPES) * len(plan["configurations"])
    if len(full) != expected_count:
        raise ValueError("Research trial coverage incomplete")
    comparisons = []
    ablation = []
    decisions = {}
    distributions = []
    repeats = []
    for kind in TYPES:
        baseline = [
            r
            for r in full
            if r.get("model_name") == "seasonal_naive"
            and r.get("prediction_type") == kind
            and r["status"] == "success"
        ]
        means = {}
        gates = {}
        for name in plan["configurations"]:
            rows = [
                r
                for r in full
                if r.get("model_name") == name
                and r.get("prediction_type") == kind
                and r["status"] == "success"
            ]
            if len(rows) != len(plan["windows"]):
                comparisons.append(
                    {
                        "prediction_type": kind,
                        "name": name,
                        "status": "incomplete",
                        "successful_folds": len(rows),
                    }
                )
                continue
            scores = [r["metrics"]["all"]["mae"] for r in rows]
            means[name] = mean(scores)
            gate = (
                promotion(rows, baseline, strict_validation=False)
                if len(baseline) == len(rows)
                else None
            )
            gates[name] = gate
            comparisons.append(
                {
                    "prediction_type": kind,
                    "name": name,
                    "status": "success",
                    "macro_metrics": {
                        metric: mean(
                            r["metrics"]["all"][metric]
                            for r in rows
                            if r["metrics"]["all"][metric] is not None
                        )
                        if any(r["metrics"]["all"][metric] is not None for r in rows)
                        else None
                        for metric in ("mae", "wape", "rmse", "bias")
                    },
                    "fold_mae": scores,
                    "fold_bias": [r["metrics"]["all"]["bias"] for r in rows],
                    "mae_std": pstdev(scores),
                    "mae_min": min(scores),
                    "mae_max": max(scores),
                    "fold_evaluation_rows": [r["metrics"]["all"]["n"] for r in rows],
                    "gate": gate,
                }
            )
            if "__without_" in name:
                original = name.split("__without_")[0]
                matched = [
                    r
                    for r in full
                    if r.get("model_name") == original
                    and r.get("prediction_type") == kind
                    and r["status"] == "success"
                ]
                if len(matched) == len(rows):
                    original_by_fold = {r["fold"]: r for r in matched}
                    ablation.append(
                        {
                            "prediction_type": kind,
                            "name": name,
                            "reference": original,
                            "group": name.split("__without_")[1],
                            "mae_delta_by_fold": [
                                r["metrics"]["all"]["mae"]
                                - original_by_fold[r["fold"]]["metrics"]["all"]["mae"]
                                for r in rows
                            ],
                            "identical_populations": all(
                                r["evaluation_rows_hash"]
                                == original_by_fold[r["fold"]]["evaluation_rows_hash"]
                                for r in rows
                            ),
                        }
                    )
        eligible = [
            n
            for n, g in gates.items()
            if n != "seasonal_naive"
            and g
            and g["majority"]
            and not g["degraded_segments"]
            and g["resources_and_fit_ok"]
        ]
        decisions[kind] = {
            "score_leader": min(means, key=lambda n: (means[n], n)) if means else None,
            "research_gate_candidate": min(eligible, key=lambda n: (means[n], n))
            if eligible
            else "seasonal_naive",
            "operational_fallback": "seasonal_naive; unavailable on invalid inputs",
            "operational_promotion": (
                "NOT VERIFIED: first-publication availability/forward/shadow absent"
            ),
            "published_selection_changed": False,
        }
        for r in baseline:
            distributions.append(
                {
                    "fold": r["fold"],
                    "prediction_type": kind,
                    "histogram": r["diagnostics"]["actual_histogram"],
                    "slices": {
                        k: v
                        for k, v in r["metrics"].items()
                        if k.startswith(("actual:", "target_month:"))
                    },
                }
            )
        first = next(
            (
                r
                for r in full
                if r.get("model_name") == "lightgbm_bounded"
                and r.get("prediction_type") == kind
                and r.get("fold") == 0
            ),
            None,
        )
        repeated = next(
            (
                r
                for r in reports
                if r.get("model_name") == "lightgbm_bounded__repeat"
                and r.get("prediction_type") == kind
            ),
            None,
        )
        ok = bool(
            first
            and repeated
            and first["status"] == repeated["status"] == "success"
            and first["prediction_values_hash"] == repeated["prediction_values_hash"]
        )
        repeats.append({"prediction_type": kind, "exact_prediction_values_repeat": ok})
        if summary["status"] == "completed" and not ok:
            raise ValueError("Research independent refit not reproducible")
    # Actual histogram TV uses an overlap form, independent of F2's L1 formula.
    shifts = []
    for kind in TYPES:
        rows = sorted(
            (r for r in distributions if r["prediction_type"] == kind),
            key=lambda r: r["fold"],
        )
        for a, b in zip(rows, rows[1:], strict=False):
            x, y = a["histogram"], b["histogram"]
            shifts.append(
                {
                    "prediction_type": kind,
                    "from_fold": a["fold"],
                    "to_fold": b["fold"],
                    "total_variation": 1
                    - sum(
                        min(
                            x.get(k, 0) / sum(x.values()), y.get(k, 0) / sum(y.values())
                        )
                        for k in x.keys() | y.keys()
                    ),
                }
            )
    pooled_slices = []
    worst_groups = []
    for kind in TYPES:
        selected = {"seasonal_naive", decisions[kind]["score_leader"]}
        for name in sorted(n for n in selected if n):
            rows = [
                r
                for r in full
                if r.get("prediction_type") == kind
                and r.get("model_name") == name
                and r["status"] == "success"
            ]
            if not rows:
                continue
            for label in (
                "actual:zero",
                "actual:nonzero",
                "actual:rare_1_4",
                "actual:middle_5_19",
                "actual:peak_ge20",
            ):
                slices = [r["metrics"][label] for r in rows if label in r["metrics"]]
                n = sum(m["n"] for m in slices)
                total = math.fsum(m["actual_sum"] for m in slices)
                absolute = math.fsum(m["mae"] * m["n"] for m in slices)
                pooled_slices.append(
                    {
                        "prediction_type": kind,
                        "name": name,
                        "slice": label,
                        "n": n,
                        "actual_sum": total,
                        "mae": absolute / n if n else None,
                        "wape": absolute / total if total else None,
                        "rmse": math.sqrt(
                            math.fsum(m["rmse"] ** 2 * m["n"] for m in slices) / n
                        )
                        if n
                        else None,
                        "bias": math.fsum(m["bias"] * m["n"] for m in slices) / n
                        if n
                        else None,
                    }
                )
            with duckdb.connect() as db:
                db.read_parquet(
                    [str(Path(r["path"]) / r["predictions"]) for r in rows]
                ).create_view("pooled")
                for label, predicate in {
                    "actual:zero": "actual=0",
                    "actual:nonzero": "actual>0",
                    "actual:rare_1_4": "actual BETWEEN 1 AND 4",
                    "actual:middle_5_19": "actual BETWEEN 5 AND 19",
                    "actual:peak_ge20": "actual>=20",
                }.items():
                    value = db.execute(f"""SELECT count(*),coalesce(sum(actual),0),
                        avg(abs(predicted-actual)),sum(abs(predicted-actual))/
                        nullif(sum(actual),0),sqrt(avg(pow(predicted-actual,2))),
                        avg(predicted-actual) FROM pooled
                        WHERE {predicate}""").fetchone()
                    assert value is not None
                    given = next(
                        r
                        for r in pooled_slices
                        if r["prediction_type"] == kind
                        and r["name"] == name
                        and r["slice"] == label
                    )
                    for field, wanted in zip(
                        ("n", "actual_sum", "mae", "wape", "rmse", "bias"),
                        value,
                        strict=True,
                    ):
                        actual = given[field]
                        if (actual is None) != (wanted is None) or (
                            actual is not None
                            and not math.isclose(
                                actual, wanted, rel_tol=1e-8, abs_tol=1e-8
                            )
                        ):
                            raise ValueError("Independent pooled error slice mismatch")
            for field in ("station_id", "local_hour"):
                groups = [
                    {"fold": r["fold"], **m}
                    for r in rows
                    for m in r["diagnostics"][field]
                ]
                for group in sorted(
                    groups, key=lambda m: (-m["mae"], m["fold"], m["key"])
                )[:3]:
                    worst_groups.append(
                        {"prediction_type": kind, "name": name, "field": field, **group}
                    )
    stress_comparison = []
    for r in reports:
        if r.get("request", {}).get("purpose") != "stress" or r["status"] != "success":
            continue
        reference = r["model_name"].split("__stress_")[0]
        original = next(
            (
                v
                for v in full
                if v.get("model_name") == reference
                and v.get("fold") == r["fold"]
                and v.get("prediction_type") == r["prediction_type"]
                and v["status"] == "success"
            ),
            None,
        )
        if original:
            stress_comparison.append(
                {
                    "prediction_type": r["prediction_type"],
                    "fold": r["fold"],
                    "reference": reference,
                    "full_training_rows": original["measurements"]["training_rows"],
                    "stress_training_rows": r["measurements"]["training_rows"],
                    "full_metrics": original["metrics"]["all"],
                    "stress_metrics": r["metrics"]["all"],
                    "mae_delta": r["metrics"]["all"]["mae"]
                    - original["metrics"]["all"]["mae"],
                    "identical_evaluation": r["evaluation_rows_hash"]
                    == original["evaluation_rows_hash"],
                }
            )
    cohort_city = []
    if plan["f2_eda"].get("path"):
        f2 = read_json(Path(plan["f2_eda"]["path"]))["research"]["histograms"]
        for r in full:
            if r.get("model_name") != "seasonal_naive" or r["status"] != "success":
                continue
            with duckdb.connect() as db:
                month_bins = db.execute(
                    """SELECT
                    strftime(timezone('America/New_York',target_hour_utc),'%Y-%m'),
                    CASE WHEN actual=0 THEN '0' WHEN actual=1 THEN '1'
                        WHEN actual<5 THEN '2-4' WHEN actual<10 THEN '5-9'
                        WHEN actual<20 THEN '10-19' ELSE '20+' END,count(*)
                    FROM read_parquet(?) GROUP BY 1,2""",
                    [str(Path(r["path"]) / r["predictions"])],
                ).fetchall()
            for month in sorted({v[0] for v in month_bins}):
                a = {v[1]: v[2] for v in month_bins if v[0] == month}
                b = {
                    v["bin"]: v["n"]
                    for v in f2[month]
                    if v["prediction_type"] == r["prediction_type"]
                }
                cohort_city.append(
                    {
                        "fold": r["fold"],
                        "prediction_type": r["prediction_type"],
                        "target_month": month,
                        "cohort_n": sum(a.values()),
                        "city_n": sum(b.values()),
                        "cohort_histogram": a,
                        "tv": 1
                        - sum(
                            min(
                                a.get(k, 0) / sum(a.values()),
                                b.get(k, 0) / sum(b.values()),
                            )
                            for k in a.keys() | b.keys()
                        ),
                        "interpretation": (
                            "Different cohort/eligible/day-window populations; "
                            "TV is descriptive composition, not proof of drift cause"
                        ),
                    }
                )
    return {
        "version": "f4-research-1",
        "verifier_code_sha256": checksum(Path(__file__)),
        "verified_at": datetime.now(UTC).isoformat(),
        "data_mode": plan["data_mode"],
        "availability_mode": MODE,
        "protocol": protocol,
        "run_id": summary["id"],
        "run_path": str(summary_path),
        "status": summary["status"],
        "outcome_counts": summary["outcome_counts"],
        "measurements": summary["measurements"],
        "inputs_id": inputs["id"],
        "splits": inputs["splits"],
        "decisions": decisions,
        "comparisons": comparisons,
        "pooled_error_slices": pooled_slices,
        "worst_station_hour_groups": worst_groups,
        "ablation": ablation,
        "stress": summary["stress_decisions"],
        "stress_comparison": stress_comparison,
        "cohort_city_monthly_composition": cohort_city,
        "count_distributions": distributions,
        "fold_distribution_shifts": shifts,
        "repeatability": repeats,
        "independent_audit": {
            "prediction_rows": oracle_rows,
            "diagnostic_groups": diagnostic_groups,
            "pooled_slice_groups": len(pooled_slices),
            "metric_mismatches": 0,
            "row_membership_mismatches": 0,
            "PIT_mismatches": 0,
            "saved_model_parity_sample_rows_per_trial": 32,
            "frozen_files_unchanged": True,
        },
        "trials": [
            {
                k: r[k]
                for k in (
                    "id",
                    "path",
                    "status",
                    "fold",
                    "prediction_type",
                    "model_name",
                    "parameters",
                    "measurements",
                    "error",
                    "evaluation_rows_hash",
                    "prediction_values_hash",
                    "feature_names",
                    "parity_rows",
                    "checksums",
                    "created_at",
                    "reason",
                )
                if k in r
            }
            | {
                "metrics": {
                    k: v
                    for k, v in r.get("metrics", {}).items()
                    if k == "all" or k.startswith(("actual:", "activity:"))
                },
                "diagnostics": r.get("diagnostics", {}),
                "input_provenance": {
                    k: r.get("request", {}).get(k)
                    for k in (
                        "protocol_id",
                        "dataset_id",
                        "inputs_id",
                        "seed",
                        "fit_asof",
                        "purpose",
                        "train_file_sha256",
                        "evaluation_file_sha256",
                    )
                },
            }
            for r in reports
        ],
        "limitations": [
            "Cohort64, about 2.56% known-ID events; not citywide",
            "New validation diagnostics do not open/reselect by final test",
            "F2 city station-hour and F4 daily-origin cohort populations differ",
            "Source availability +45d is a retrospective assumption, "
            "not historical fact",
            "Weather is outcome-only after prediction; "
            "no GBFS/capacity/short lag added",
        ],
    }


def label_oracle(dataset: Path, data: dict[str, Any]) -> dict[str, Any]:
    gold = read_json(Path(data["gold_path"]) / "manifest.json")
    snapshot = read_json(Path(data["source_snapshot"]) / "manifest.json")
    if (
        checksum(Path(data["source_snapshot"]) / "manifest.json")
        != data["source_manifest_checksum"]
    ):
        raise ValueError("Independent F2 manifest checksum mismatch")
    with (
        tempfile.TemporaryDirectory(dir=dataset.parent) as directory,
        configured(Path(directory) / "oracle.duckdb") as db,
    ):
        try:
            db.read_parquet(
                [
                    str(Path(p["path"]) / "silver.parquet")
                    for p in snapshot["partitions"]
                ]
            ).create_view("silver")
            db.read_parquet(str(dataset / "stations.parquet")).create_view("stations")
            db.read_parquet(str(dataset / "dense.parquet")).create_view("dense")
            first, last = utc(gold["period_start"]), utc(gold["period_end"])
            db.execute(
                """CREATE OR REPLACE TABLE reference AS SELECT station_id,
                date_trunc('hour',event_at) hour_utc,prediction_type,count(*) actual
                FROM (SELECT start_station_id station_id,started_at_utc event_at,
                    'departures' prediction_type FROM silver
                    WHERE disposition='accepted'
                    AND started_at_utc>=? AND started_at_utc<?
                    AND start_station_id IN(SELECT station_id FROM stations)
                    UNION ALL SELECT end_station_id,ended_at_utc,'arrivals' FROM silver
                    WHERE disposition='accepted' AND ended_at_utc>=? AND ended_at_utc<?
                    AND end_station_id IN(SELECT station_id FROM stations)) events
                GROUP BY ALL""",
                [first, last, first, last],
            )
            db.execute("""CREATE OR REPLACE VIEW labels AS SELECT station_id,hour_utc,
                'departures' prediction_type,departures actual FROM dense UNION ALL
                SELECT station_id,hour_utc,'arrivals',arrivals FROM dense""")
            mismatch = db.execute("""SELECT count(*) FROM labels l LEFT JOIN reference r
                USING(station_id,hour_utc,prediction_type)
                WHERE l.actual IS NOT NULL
                    AND l.actual<>coalesce(r.actual,0)""").fetchone()
            lost = db.execute("""SELECT count(*) FROM reference r LEFT JOIN labels l
                USING(station_id,hour_utc,prediction_type)
                WHERE l.actual IS NULL""").fetchone()
            if mismatch != (0,) or lost != (0,):
                raise ValueError("Independent Silver → ML label mismatch")
            rows = db.execute("SELECT count(*),sum(actual) FROM reference").fetchone()
        finally:
            db.execute("DROP TABLE IF EXISTS reference")
    # The owned oracle DB is a scratch artifact, outside immutable dataset.
    return {
        "nonzero_groups": rows[0] if rows else 0,
        "observed_events": rows[1] if rows else 0,
        "mismatches": 0,
        "lost_positive_groups": 0,
        "source": "direct accepted Silver, separate start/end predicates",
    }


def audit_run(path: Path, report: dict[str, Any]) -> dict[str, Any]:
    rows = 0
    files = []
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        for split in report["splits"]:
            train = path / split["training_file"]
            invalid = db.execute(
                """SELECT count(*) FROM read_parquet(?)
                WHERE feature_available_at>forecast_origin_utc
                OR max_event_at>=forecast_origin_utc OR label_available_at>?
                OR target_hour_utc+INTERVAL '1 hour'>=?""",
                [
                    str(train),
                    utc(split["fit_asof"]),
                    utc(split["evaluation_target_min"]),
                ],
            ).fetchone()
            if invalid != (0,):
                raise ValueError("Independent fold boundary/PIT violation")
        for result in report["results"]:
            file = path / result["predictions"]
            count = oracle_file(file, result["metrics"])
            if count != result["metrics"]["all"]["n"]:
                raise ValueError("Prediction row count mismatch")
            files.append(
                {
                    "file": file.name,
                    "checksum": checksum(file),
                    "rows": count,
                    "evaluation_rows_hash": result["evaluation_rows_hash"],
                }
            )
            rows += count
        for fold in range(len(report["splits"])):
            for kind in ("departures", "arrivals"):
                hashes = {
                    r["evaluation_rows_hash"]
                    for r in report["results"]
                    if r["fold"] == fold and r["prediction_type"] == kind
                }
                if len(hashes) != 1:
                    raise ValueError("Different candidate evaluation populations")
    return {
        "independently_recomputed_prediction_rows": rows,
        "metric_mismatches": 0,
        "fold_leakage_rows": 0,
        "files": files,
    }


def feature_oracle(dataset: Path) -> dict[str, Any]:
    """Recompute prefix features by independent range joins, not builder windows."""
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        db.execute(
            """CREATE TABLE samples AS SELECT row_number() OVER() sample_id,*
            FROM (SELECT * FROM read_parquet(?) WHERE eligibility='eligible'
            ORDER BY sha256(station_id||forecast_origin_utc::VARCHAR||
                horizon_h::VARCHAR||prediction_type) LIMIT 128)""",
            [str(dataset / "examples.parquet")],
        )
        db.execute(
            """CREATE TABLE historical AS WITH labels AS(
            SELECT station_id,hour_utc,label_available_at,'departures' prediction_type,
                departures actual FROM read_parquet(?) UNION ALL
            SELECT station_id,hour_utc,label_available_at,'arrivals',arrivals
                FROM read_parquet(?))
            SELECT *,isodow(timezone('America/New_York',hour_utc))-1 weekday,
                hour(timezone('America/New_York',hour_utc)) local_hour
            FROM labels WHERE actual IS NOT NULL""",
            [str(dataset / "dense.parquet")] * 2,
        )
        rows = db.execute("""SELECT s.sample_id,s.profile_mean,avg(h.actual),
            s.profile_count,count(*),s.seasonal_naive,arg_max(h.actual,h.hour_utc),
            s.max_event_at,max(h.hour_utc)
            FROM samples s JOIN historical h ON s.station_id=h.station_id
            AND s.prediction_type=h.prediction_type AND s.weekday=h.weekday
            AND s.local_hour=h.local_hour
            AND h.label_available_at<=s.forecast_origin_utc
            GROUP BY 1,2,4,6,8""").fetchall()
        count = db.execute("SELECT count(*) FROM samples").fetchone()
        if count is None or len(rows) != count[0]:
            raise ValueError("Independent profile coverage mismatch")
        for (
            _key,
            mean,
            expected_mean,
            n,
            expected_n,
            naive,
            expected_naive,
            last,
            expected_last,
        ) in rows:
            if not math.isclose(mean, expected_mean, abs_tol=1e-10) or (
                n != expected_n or naive != expected_naive or last != expected_last
            ):
                raise ValueError("Independent as-of profile mismatch")
        means = db.execute("""SELECT s.sample_id,s.station_mean,avg(h.actual)
            FROM samples s JOIN historical h ON s.station_id=h.station_id
            AND s.prediction_type=h.prediction_type
            AND h.label_available_at<=s.forecast_origin_utc GROUP BY 1,2""").fetchall()
        if len(means) != len(rows) or any(
            not math.isclose(a, b, abs_tol=1e-10) for _, a, b in means
        ):
            raise ValueError("Independent as-of station mean mismatch")
    return {
        "sampled_origins_station_horizons": len(rows),
        "mismatches": 0,
        "method": "independent available-at range joins over Silver-reconciled labels",
    }


def serving_oracle(forecast: Path, report: dict[str, Any]) -> dict[str, Any]:
    from citibike.serving.db import connect

    with duckdb.connect() as db:
        expected = db.execute(
            "SELECT station_id,prediction_type,horizon_h,predicted_value "
            "FROM read_parquet(?) ORDER BY 1,2,3",
            [str(forecast / "forecasts.parquet")],
        ).fetchall()
    with connect(readonly=True) as db:
        actual = db.execute(
            """SELECT station_id,prediction_type,horizon_h,predicted_value
            FROM ml_forecasts WHERE publication_id=%s ORDER BY 1,2,3""",
            (report["publication_id"],),
        ).fetchall()
    observed = [tuple(r.values()) for r in actual]
    if observed != expected:
        raise ValueError("Independent artifact → PostgreSQL mismatch")
    with httpx.Client(timeout=30) as client:
        records: list[tuple[Any, ...]] = []
        for offset in range(0, len(expected), 200):
            response = client.get(
                "http://127.0.0.1:8000/v3/forecasts",
                params={
                    "publication_id": report["publication_id"],
                    "limit": 200,
                    "offset": offset,
                },
            )
            response.raise_for_status()
            body = response.json()
            if body["publication"]["publication_id"] != report[
                "publication_id"
            ] or body["total_rows"] != len(expected):
                raise ValueError("Mixed HTTP forecast version")
            records.extend(
                (
                    r["station_id"],
                    r["prediction_type"],
                    r["horizon_h"],
                    r["predicted_value"],
                )
                for r in body["rows"]
            )
        if records != expected:
            raise ValueError("Independent PostgreSQL → HTTP mismatch")
    return {
        "publication_id": report["publication_id"],
        "postgres_rows": len(actual),
        "http_rows": len(records),
        "mismatches": 0,
    }


def provenance(reports: list[dict[str, Any]]) -> None:
    data, validation, selection, final, bundle, forecast = reports
    if any(r["dataset_id"] != data["id"] for r in reports[1:]):
        raise ValueError("Mixed F4 dataset versions")
    if selection["experiment_id"] != validation["id"] or (
        final["selection_id"] != selection["id"]
        or bundle["selection_id"] != selection["id"]
        or forecast["model_bundle_id"] != bundle["id"]
    ):
        raise ValueError("Mixed F4 experiment/selection/model versions")


def weather_metrics(path: Path, daily: dict[str, Any]) -> dict[str, Any]:
    """Independent posthoc diagnostics over fixed predictions, no model fitting."""
    accum: dict[str, list[float]] = {}
    with duckdb.connect() as db:
        rows = db.execute(
            "SELECT target_hour_utc,actual,predicted FROM read_parquet(?)", [str(path)]
        )
        while batch := rows.fetchmany(4096):
            for hour, actual, predicted in batch:
                day = hour.astimezone(ZoneInfo("America/New_York")).date().isoformat()
                if day not in daily:
                    continue
                error = predicted - actual
                for name in ("observed", "cold", "heat", "heavy_rain"):
                    if name != "observed" and not daily[day][name]:
                        continue
                    a = accum.setdefault("weather:" + name, [0, 0, 0, 0, 0])
                    for i, value in enumerate(
                        (1, actual, abs(error), error * error, error)
                    ):
                        a[i] += value
    return {
        label: {
            "n": int(n),
            "actual_sum": actual,
            "mae": absolute / n,
            "wape": absolute / actual if actual else None,
            "rmse": math.sqrt(squares / n),
            "bias": bias / n,
            "availability_mode": MODE,
        }
        for label, (n, actual, absolute, squares, bias) in accum.items()
    }


def verify(root: Path, output: Path, *, serving: bool = False) -> Path:
    dataset, experiment, selection, final, model, forecast = (
        active(root, k)
        for k in (
            "datasets",
            "experiments",
            "selections",
            "final_tests",
            "models",
            "forecasts",
        )
    )
    data, validation, chosen, heldout, bundle, prediction = map(
        checked, (dataset, experiment, selection, final, model, forecast)
    )
    provenance([data, validation, chosen, heldout, bundle, prediction])
    if (
        data["feature_schema"] != SCHEMA
        or chosen["feature_schema"] != SCHEMA
        or bundle["feature_schema"] != SCHEMA
    ):
        raise ValueError("Feature schema mismatch")
    if heldout["selection_id"] != chosen["id"] or utc(chosen["created_at"]) > utc(
        heldout["created_at"]
    ):
        raise ValueError("Test selection not locked before final access")
    if heldout["selection_changed_by_test"] or validation["final_test_accessed"]:
        raise ValueError("Final test used for selection")
    if (
        checksum(Path(data["gold_path"]) / "manifest.json")
        != data["gold_manifest_checksum"]
    ):
        raise ValueError("Gold provenance checksum mismatch")
    with duckdb.connect() as db:
        leak = db.execute(
            """SELECT count(*) FROM read_parquet(?) WHERE eligibility='eligible'
            AND (feature_available_at>forecast_origin_utc
                OR max_event_at>=forecast_origin_utc)""",
            [str(dataset / "examples.parquet")],
        ).fetchone()
        if leak != (0,):
            raise ValueError("Dataset future feature leakage")
        historical = db.execute(
            """SELECT count(*) FROM read_parquet(?)
            WHERE eligibility='eligible' AND forecast_origin_utc<?""",
            [str(dataset / "examples.parquet"), utc(data["created_at"])],
        ).fetchone()
    label_audit = label_oracle(dataset, data)
    feature_audit = feature_oracle(dataset)
    validation_audit = audit_run(experiment, validation)
    final_audit = audit_run(final, heldout)
    from citibike.serving.forecast_publish import validate

    validate(forecast)

    weather = validation["evaluation_weather"]
    daily: dict[str, Any] = {}
    correction: dict[str, Any] = {"status": "not applicable to controlled fixture"}
    if weather.get("id"):
        weather_path = root / "f4" / "evaluation_weather" / weather["id"]
        original = checked(weather_path)
        daily = summarize(read_json(weather_path / "source.json"))
        old_daily = read_json(weather_path / "daily.json")
        changes = {
            day: {
                "original_mm": old_daily[day]["precipitation_mm"],
                "corrected_mm": values["precipitation_mm"],
            }
            for day, values in daily.items()
            if day in old_daily and old_daily[day]["heavy_rain"] != values["heavy_rain"]
        }
        correction = {
            "original_policy": original["policy"],
            "effective_diagnostic_policy": POLICY,
            "source_checksum": checksum(weather_path / "source.json"),
            "heavy_rain_label_changes_all_source_days": changes,
            "reason": (
                "ERA5 precipitation is the preceding hour sum; "
                "assign to NY day of interval, Decimal threshold"
            ),
            "scope": (
                "posthoc auxiliary metrics only; original immutable runs, "
                "predictions, actuals, model parameters and frozen selection unchanged"
            ),
        }

    def compact(path: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
        results = [
            {
                k: r[k]
                for k in (
                    "fold",
                    "prediction_type",
                    "model_name",
                    "model_id",
                    "parameters",
                    "evaluation_rows_hash",
                    "metrics",
                    "measurements",
                )
            }
            for r in run["results"]
        ]
        if daily:
            for source, result in zip(run["results"], results, strict=True):
                result["metrics"] = {
                    k: v
                    for k, v in result["metrics"].items()
                    if not k.startswith("weather:")
                } | weather_metrics(path / source["predictions"], daily)
        return results

    validation_results = compact(experiment, validation)
    final_results = compact(final, heldout)
    if daily:
        from citibike.forecasting.experiment import promotion

        gates = {}
        for kind in ("departures", "arrivals"):
            rows = [r for r in validation_results if r["prediction_type"] == kind]
            baseline = [r for r in rows if r["model_name"] == "seasonal_naive"]
            gates[kind] = {
                name: promotion(
                    [r for r in rows if r["model_name"] == name],
                    baseline,
                    strict_validation=False,
                )
                for name in sorted({r["model_name"] for r in rows} - {"seasonal_naive"})
            }
            if any(g["promote"] for g in gates[kind].values()):
                raise ValueError("Corrected diagnostics require champion review")
        correction["corrected_validation_gates"] = gates
        correction["champion_decision_unchanged"] = True

    evidence = {
        "version": "f4.0.0",
        "verified_at": datetime.now(UTC).isoformat(),
        "data_mode": data["data_mode"],
        "availability_mode": MODE,
        "dataset_id": data["id"],
        "dataset_checksums": data["checksums"],
        "source_snapshot_id": data["source_snapshot_id"],
        "gold_publication_id": data["gold_publication_id"],
        "feature_schema": SCHEMA,
        "feature_catalog": data["catalog"],
        "coverage": data["coverage"],
        "eligibility": data["eligibility"],
        "availability_audit": data["availability_audit"],
        "strict_historical_unavailable_rows": historical[0] if historical else 0,
        "label_oracle": label_audit,
        "feature_oracle": feature_audit,
        "validation_splits": validation["splits"],
        "validation_results": validation_results,
        "validation_independent_audit": validation_audit,
        "selection": chosen,
        "final_splits": heldout["splits"],
        "final_results": final_results,
        "final_independent_audit": final_audit,
        "model_bundle": bundle,
        "inference": prediction,
        "environment": validation["environment"],
        "measurements": {
            "dataset": data["measurements"],
            "validation": validation["measurements"],
            "final_test": heldout["measurements"],
            "inference": prediction["measurements"],
        },
        "evaluation_weather": validation["evaluation_weather"],
        "weather_diagnostic_correction": correction,
        "weather_use": "ERA5 outcomes only after prediction; never feature input",
        "registry": (
            "Immutable local manifests; MLflow Registry automation deferred to F7"
        ),
        "serving": serving_oracle(forecast, prediction)
        if serving
        else (
            "NOT VERIFIED in this command: run verify --serving "
            "with published forecast/API"
        ),
    }
    if (root / "f4/active_research.json").exists():
        corrective = research_evidence(root)
        if output.exists():
            previous = read_json(output)
            if previous.get("dataset_id") != data["id"]:
                raise ValueError("Cannot append research to another dataset evidence")
            # Preserve the old frozen evaluation verbatim as JSON values.
            previous["corrective_validation"] = corrective
            previous["corrective_frozen_regression"] = {
                k: evidence[k]
                for k in (
                    "verified_at",
                    "label_oracle",
                    "feature_oracle",
                    "validation_independent_audit",
                    "final_independent_audit",
                    "serving",
                )
            }
            evidence = previous
        else:
            evidence["corrective_validation"] = corrective
        if output.name == "f4-forecasting.json":
            research_markdown(corrective, output.with_suffix(".md"))
    write_json(output, evidence)
    return output


def research_markdown(report: dict[str, Any], output: Path) -> None:
    """Append to the existing report, preserving its historical frozen section."""
    marker = "\n## Корректирующий validation-only protocol"
    old = (
        output.read_text(encoding="utf-8")
        if output.exists()
        else "# F4 — Forecasting\n"
    )
    old = old.split(marker)[0]
    protocol = report["protocol"]
    rows = [
        marker,
        "",
        f"Protocol `{protocol['id']}`, saved {protocol['created_at']} before "
        f"new trials. Dataset `{protocol['plan']['dataset_id']}`. "
        f"Run `{report['run_id']}`; mode **{report['availability_mode']}**.",
        "",
        "Предыдущие разделы — прежний frozen evaluation. Этот раздел содержит "
        "только новые validation trials; Sep2026 final scores не участвовали в новом "
        "сравнении. Existing selection/model/forecast IDs и serving сохранены.",
        "",
        f"Outcomes: `{report['outcome_counts']}`. "
        f"Resources: `{report['measurements']}`. Per-trial native process RSS/wall/"
        "parameters/status/error/seed/input versions сохранены в immutable trials; "
        "компактные ссылки/checksums и результаты находятся в F4 JSON.",
        "",
        "### Bounded search и ablation",
        "",
        "Macro metrics — невзвешенное среднее по validation folds. N по каждому "
        "fold явен в JSON; пропущенный/нулевой actual denominator WAPE остаётся N/A. "
        "Counts0/1–4/5–19/20+ заданы до fits по принятому F2 EDA; weekday peak-hour "
        "и count>=20 — разные диагностические определения.",
        "",
        "| Target | Configuration | Macro MAE | WAPE | RMSE | Bias | "
        "Wins | >20% degraded key slices |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in report["comparisons"]:
        if r["status"] != "success":
            rows.append(
                f"| {r['prediction_type']} | {r['name']} | "
                "incomplete | — | — | — | — | — |"
            )
            continue
        m, g = r["macro_metrics"], r["gate"]
        wape = f"{m['wape']:.4f}" if m["wape"] is not None else "N/A"
        rows.append(
            f"| {r['prediction_type']} | {r['name']} | {m['mae']:.4f} | "
            f"{wape} | {m['rmse']:.4f} | {m['bias']:.4f} | "
            f"{g['wins']}/{g['windows']} | {len(g['degraded_segments'])} |"
            if g
            else "| incomplete baseline |"
        )
    rows += [
        "",
        "Ablation сравнивает каждый bounded algorithm с тем же algorithm без "
        "одной группы на идентичных evaluation rows. horizon_h сохранён. Seasonal "
        "Naive независим: удаление определяющих profile/station/calendar связей "
        "меняет его семантику, поэтому baseline ablation неприменима. Profiles "
        "косвенно кодируют station/calendar; это сравнение колонок, не причинный "
        "вклад информации.",
        "",
        "| Target | Removed group / model | ΔMAE по folds (ablated − full) |",
        "|---|---|---|",
    ]
    for r in report["ablation"]:
        values = ", ".join(f"{v:+.4f}" for v in r["mae_delta_by_fold"])
        rows.append(f"| {r['prediction_type']} | {r['name']} | {values} |")
    rows += [
        "",
        "### Stress, ошибки и стабильность",
        "",
        "Stress candidate выбран только по validation majority/resource gates и "
        "macro MAE среди прежних bounded configurations; parameter search не "
        "расширялся. Train уменьшен до половины фактических полных station/origin "
        "blocks. Evaluation population неизменна.",
        "",
        "| Target | Candidate | Fold | Train full/half | MAE full/half | ΔMAE |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for r in report["stress_comparison"]:
        rows.append(
            f"| {r['prediction_type']} | {r['reference']} | {r['fold']} | "
            f"{r['full_training_rows']}/{r['stress_training_rows']} | "
            f"{r['full_metrics']['mae']:.4f}/{r['stress_metrics']['mae']:.4f} | "
            f"{r['mae_delta']:+.4f} |"
        )
    rows += [
        "",
        "### Zero/nonzero, rare и peak errors",
        "",
        "Pooled metrics ниже пересчитаны из сумм ошибок, квадратов ошибок и "
        "actual по четырём folds; это другой знаменатель, чем macro scores выше. "
        "Zero WAPE не определена. Показаны baseline и validation score leader.",
        "",
        "| Target | Model | Slice | N | MAE | WAPE | RMSE | Bias |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in report["pooled_error_slices"]:
        metric_text = [
            f"{r[k]:.4f}" if r[k] is not None else "N/A"
            for k in ("mae", "wape", "rmse", "bias")
        ]
        rows.append(
            f"| {r['prediction_type']} | {r['name']} | {r['slice']} | "
            f"{r['n']} | " + " | ".join(metric_text) + " |"
        )
    rows += [
        "",
        "### Крупные station/hour ошибки",
        "",
        "Top groups относятся к одному указанному validation fold, "
        "не ко всей городской сети и не к pooled station ranking.",
        "",
        "| Target | Model | Field/key | Fold | N | MAE | Bias |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for r in report["worst_station_hour_groups"]:
        rows.append(
            f"| {r['prediction_type']} | {r['name']} | {r['field']}:{r['key']} | "
            f"{r['fold']} | {r['n']} | {r['mae']:.4f} | {r['bias']:.4f} |"
        )
    rows += [
        "",
        "Validation JSON содержит zero/nonzero/rare/peak, low/high/new-known "
        "slices, top station/hour MAE/bias и largest residuals для каждого trial; "
        "fold MAE/bias/min/max/std и coverage, count-distribution TV между "
        "validation окнами. Cohort vs F2 city monthly TV — разные populations "
        "и длины окон; это composition diagnostic, не causal drift и не citywide "
        "точность. Отсутствующие slices не заполнены нулями.",
        "",
        "| Target | Lowest-score configuration | Passed research gates | Operational |",
        "|---|---|---|---|",
    ]
    for kind, d in report["decisions"].items():
        rows.append(
            f"| {kind} | {d['score_leader']} | {d['research_gate_candidate']} | "
            "Seasonal Naive fallback; promotion NOT VERIFIED |"
        )
    rows += ["", "### Проверяемые выводы", ""]
    for kind, d in report["decisions"].items():
        leader = next(
            (
                r
                for r in report["comparisons"]
                if r.get("name") == d["score_leader"] and r["prediction_type"] == kind
            ),
            None,
        )
        baseline = next(
            (
                r
                for r in report["comparisons"]
                if r.get("name") == "seasonal_naive" and r["prediction_type"] == kind
            ),
            None,
        )
        if leader and baseline:
            a, b = leader["macro_metrics"]["mae"], baseline["macro_metrics"]["mae"]
            rows.append(
                f"- {kind}: lowest validation macro MAE {a:.4f} "
                f"({d['score_leader']}) против baseline {b:.4f}; "
                f"fold range [{leader['mae_min']:.4f},{leader['mae_max']:.4f}], "
                f"std {leader['mae_std']:.4f}. "
                f"Majority {leader['gate']['wins']}/{leader['gate']['windows']}; "
                f"degraded key slices {len(leader['gate']['degraded_segments'])}. "
                f"Research gate candidate: {d['research_gate_candidate']}; "
                "это не смена опубликованного champion."
            )
        for label in ("actual:zero", "actual:peak_ge20"):
            slices_by_name = {
                r["name"]: r
                for r in report["pooled_error_slices"]
                if r["prediction_type"] == kind and r["slice"] == label
            }
            leader_slice = slices_by_name.get(d["score_leader"])
            baseline_slice = slices_by_name.get("seasonal_naive")
            if leader_slice and baseline_slice and leader_slice["n"]:
                rows.append(
                    f"- {kind} {label}: leader MAE {leader_slice['mae']:.4f}, "
                    f"baseline {baseline_slice['mae']:.4f}, "
                    f"N={leader_slice['n']}; leader bias "
                    f"{leader_slice['bias']:+.4f}. "
                    "Положительный bias означает overprediction, "
                    "редкий peak slice не доказывает переносимость на всю сеть."
                )
    rows += [
        "",
        "Research gate требует majority wins, отсутствие >20% деградации "
        "значимых key segments, ресурсы и fit warnings. Низкий средний score сам "
        "по себе не проходит эти gates. Strict publication availability и "
        "forward/shadow evidence отсутствуют: operational uplift не доказан. "
        "Baseline/unavailable fallback сохранён; новые final fits/reselection "
        "и переключение опубликованного champion не выполнялись.",
        "",
        "### Независимая сверка / воспроизводимость",
        "",
        f"Independent audit: `{report['independent_audit']}`; "
        f"repeat fits: `{report['repeatability']}`.",
        "Метрики пересчитаны stdlib oracle по всем prediction rows; station/hour "
        "diagnostics — отдельным SQL; row membership — двусторонним EXCEPT ALL; "
        "PIT/embargo — SQL; сохранённые модели заново загружены и сверены с "
        "prediction samples. Все failures сохраняют error/status и input hashes. "
        "Controlled failed/timeout/changed-population tests — отдельные fixture "
        "доказательства механизма, не вымышленные failed real fits.",
        "",
        "```sh",
        "uv run --locked --group ml python -m citibike.forecasting protocol",
        "uv run --locked --group ml python -m citibike.forecasting research --protocol "
        + "data/f4/research_protocols/"
        + report["protocol"]["id"],
        "uv run --locked --group ml python -m citibike.forecasting verify --serving",
        "```",
        "`--protocol` принимает directory "
        f"`data/f4/research_protocols/{protocol['id']}`. "
        "Повтор research переиспользует "
        "immutable outcomes, включая failures. Ресурсы — shared-host OS process "
        "high-water; RSS gate не OS memory quota и не production SLA.",
        "",
        "Ограничения: " + "; ".join(report["limitations"]) + ".",
        "",
    ]
    output.write_text(old + "\n".join(rows), encoding="utf-8")
