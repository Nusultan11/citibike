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
    write_json(output, evidence)
    return output
