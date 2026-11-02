"""ML readiness audit; reads labels and availability, never fits or scores a model."""

import time
from pathlib import Path
from typing import Any

import duckdb

from citibike.decision_research import folds, inputs, populations_of, research_root
from citibike.decision_research.features import (
    CLOSURE_EXPECTED_EVENTS,
    CLOSURE_MIN_HOURS,
    RULES,
    VERSION,
    anomalous,
    quality_flags,
)
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity


def rows(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cursor = db.execute(sql)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, r, strict=True)) for r in cursor.fetchall()]


def labels_sql(field: str) -> str:
    return f"""SELECT '{field}' label_type,count(*) station_hours,
        count({field}) observed,count(*)-count({field}) missing,
        count(*) FILTER(WHERE {field}=0) zero,
        count(*) FILTER(WHERE {field} BETWEEN 1 AND 4) rare_1_4,
        count(*) FILTER(WHERE {field} BETWEEN 5 AND 19) middle_5_19,
        count(*) FILTER(WHERE {field}>=20) peak_ge20,
        max({field}) maximum,sum({field}) events,
        quantile_cont({field},0.99) p99,quantile_cont({field},0.999) p999
        FROM dense"""


def cohort_profile(path: Path) -> dict[str, Any]:
    data = checked(path)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
        for name in ("dense", "stations", "examples"):
            db.read_parquet(str(path / f"{name}.parquet")).create_view(name)
        quality_flags(db)
        stations = [
            r["station_id"] for r in rows(db, "SELECT station_id FROM stations")
        ]
        gaps = rows(
            db,
            """SELECT 'departures' label_type,count(*) hour_count FROM (
                SELECT hour_utc FROM dense GROUP BY 1 HAVING count(departures)=0)
            UNION ALL SELECT 'arrivals',count(*) FROM (SELECT hour_utc FROM dense
                GROUP BY 1 HAVING count(arrivals)=0)""",
        )
        station_share = rows(
            db,
            """WITH s AS (SELECT station_id,
                count(departures)/count(*)::DOUBLE dep,
                count(arrivals)/count(*)::DOUBLE arr FROM dense GROUP BY 1)
            SELECT min(dep) dep_min,median(dep) dep_median,
                count(*) FILTER(WHERE dep<0.5) dep_below_half,
                min(arr) arr_min,median(arr) arr_median,
                count(*) FILTER(WHERE arr<0.5) arr_below_half FROM s""",
        )
        tail = rows(
            db,
            """WITH t AS (SELECT departures v,
                row_number() OVER(ORDER BY departures DESC) r,count(*) OVER() n
                FROM dense WHERE departures IS NOT NULL)
            SELECT sum(v) FILTER(WHERE r<=n*0.01)/sum(v) top1pct_hours_event_share,
                sum(v) FILTER(WHERE r<=n*0.1)/sum(v) top10pct_hours_event_share
                FROM t""",
        )
        return {
            "dataset_id": data["id"],
            "stations": len(stations),
            "activity": rows(
                db,
                "SELECT activity,count(*) stations,sum(enrolled_new::INT) enrolled_new "
                "FROM stations GROUP BY 1 ORDER BY 1",
            ),
            "anomalous_ids": sorted(s for s in stations if anomalous(s)),
            "coverage": data["coverage"],
            "eligibility": data["eligibility"],
            "build_measurements": data["measurements"],
            "dataset_bytes": sum(
                p.stat().st_size for p in path.glob("*") if p.is_file()
            ),
            "labels": [
                rows(db, labels_sql("departures"))[0],
                rows(db, labels_sql("arrivals"))[0],
            ],
            "hours_missing_for_every_station": gaps,
            "station_observed_share": station_share[0],
            "tail_concentration_departures": tail[0],
            "monthly": rows(
                db,
                """SELECT month_key,count(*) station_hours,count(departures) observed,
                    count(*) FILTER(WHERE departures=0) zero,
                    count(*)-count(departures) missing,
                    sum(departures)/count(departures) mean_departures,
                    count(DISTINCT station_id) FILTER(WHERE departures>0) active_ids
                    FROM dense GROUP BY 1 ORDER BY 1""",
            ),
            "examples_by_month": rows(
                db,
                """SELECT strftime(timezone('America/New_York',target_hour_utc),
                    '%Y-%m') month_key,eligibility,count(*) n FROM examples
                    GROUP BY 1,2 ORDER BY 1,2""",
            ),
            "composition": rows(
                db,
                """SELECT strftime(timezone('America/New_York',first_event),'%Y-%m')
                    first_month,count(*) stations FROM stations
                    GROUP BY 1 ORDER BY 1""",
            ),
            "exits_before_2026_06": rows(
                db,
                """SELECT station_id,last_event FROM stations
                    WHERE last_event<TIMESTAMPTZ '2026-06-01 00:00:00+00'
                    ORDER BY last_event""",
            ),
            "suspect_closure": rows(
                db,
                f"""SELECT count(*) runs,count(DISTINCT station_id) stations,
                    coalesce(sum(run_hours),0) flagged_hours,
                    max(run_hours) longest_hours,
                    {CLOSURE_MIN_HOURS} min_hours,{CLOSURE_EXPECTED_EVENTS}
                    expected_events FROM flags""",
            )[0],
            "leakage": rows(
                db,
                """SELECT count(*) FILTER(WHERE feature_available_at>
                        forecast_origin_utc) future_feature_rows,
                    count(*) FILTER(WHERE max_event_at>=forecast_origin_utc)
                        future_event_rows,
                    count(*) FILTER(WHERE forecast_origin_utc<known_at)
                        unknown_station_rows
                    FROM examples WHERE eligibility='eligible'""",
            )[0],
            "grain_duplicates": rows(
                db,
                """SELECT count(*) n FROM (SELECT station_id,forecast_origin_utc,
                    horizon_h,prediction_type FROM examples GROUP BY ALL
                    HAVING count(*)<>1)""",
            )[0]["n"],
        }


def fold_audit(path: Path, dataset: Path) -> list[dict[str, Any]]:
    """Row counts and availability only; holdout actuals are not summarised here."""
    report = checked(path)
    result = []
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
        for split in report["splits"]:
            train = str(path / split["train_file"])
            evaluation = str(path / split["evaluation_file"])
            known = db.execute(
                "SELECT count(DISTINCT station_id) FROM read_parquet(?)", [train]
            ).fetchone()
            unseen = db.execute(
                """SELECT count(*),count(DISTINCT station_id) FROM read_parquet(?)
                WHERE station_id NOT IN (SELECT station_id FROM read_parquet(?))""",
                [evaluation, train],
            ).fetchone()
            months = db.execute(
                """WITH m AS (SELECT DISTINCT month(timezone('America/New_York',
                    target_hour_utc)) x,? side FROM read_parquet(?)
                UNION ALL SELECT DISTINCT month(timezone('America/New_York',
                    target_hour_utc)),? FROM read_parquet(?))
                SELECT count(*) FROM (SELECT x FROM m GROUP BY 1
                    HAVING count(DISTINCT side)=2)""",
                ["t", train, "e", evaluation],
            ).fetchone()
            bad = db.execute(
                """SELECT count(*) FROM read_parquet(?) WHERE
                label_available_at>?::TIMESTAMPTZ
                OR target_hour_utc>=?::TIMESTAMPTZ-INTERVAL '24 hours'""",
                [train, split["fit_asof"], split["fit_asof"]],
            ).fetchone()
            overlap = db.execute(
                """SELECT count(*) FROM (SELECT station_id,target_hour_utc,
                    prediction_type FROM read_parquet(?) INTERSECT
                    SELECT station_id,target_hour_utc,prediction_type
                    FROM read_parquet(?))""",
                [train, evaluation],
            ).fetchone()
            assert known and unseen and months and bad and overlap
            result.append(
                {
                    "fold": split["fold"],
                    "fit_asof": split["fit_asof"],
                    "train_rows": split["train_rows"],
                    "evaluation_rows": split["evaluation_rows"],
                    "train_stations": known[0],
                    "train_rows_per_station": split["train_rows"] / known[0],
                    "evaluation_rows_unseen_station": unseen[0],
                    "evaluation_stations_unseen": unseen[1],
                    "evaluation_calendar_months_in_train": months[0],
                    "train_rows_after_availability_or_inside_embargo": bad[0],
                    "train_evaluation_target_overlap": overlap[0],
                    "train_flagged_rows": split["train_flagged_rows"],
                    "evaluation_flagged_rows": split["evaluation_flagged_rows"],
                    "train_missing_fraction": split["train_missing_fraction"],
                    "evaluation_missing_fraction": split["evaluation_missing_fraction"],
                    "eligibility": split["eligibility"],
                }
            )
    return result


def composition_bias(gold: Path, cohorts: dict[str, Path]) -> dict[str, Any]:
    """City station deciles against each cohort; different stations, not a sample."""
    months = [
        str(Path(m["path"]) / "fact_station_daily.parquet")
        for m in checked_gold(gold)["months"]
    ]
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
        db.read_parquet(months).create_view("facts")
        db.execute("""CREATE TABLE city AS SELECT station_id,
            sum(departures+arrivals) events,
            ntile(10) OVER(ORDER BY sum(departures+arrivals),station_id) decile
            FROM facts GROUP BY 1""")
        total = db.execute("SELECT count(*),sum(events) FROM city").fetchone()
        assert total is not None
        result: dict[str, Any] = {
            "city_station_ids": total[0],
            "city_known_id_events": total[1],
        }
        for name, path in cohorts.items():
            db.read_parquet(str(path / "stations.parquet")).create_view(
                "cohort", replace=True
            )
            shares = dict(
                db.execute("""SELECT decile,count(*)/(SELECT count(*)::DOUBLE
                    FROM cohort) FROM cohort JOIN city USING(station_id)
                    GROUP BY 1""").fetchall()
            )
            mean = db.execute("""SELECT avg(events),sum(events) FROM cohort
                JOIN city USING(station_id)""").fetchone()
            assert mean is not None
            result[name] = {
                "stations": len(db.execute("SELECT 1 FROM cohort").fetchall()),
                "decile_share": {str(k): shares.get(k, 0.0) for k in range(1, 11)},
                "total_variation_from_city_deciles": sum(
                    abs(shares.get(k, 0.0) - 0.1) for k in range(1, 11)
                )
                / 2,
                "mean_events_ratio_to_city": float(mean[0])
                / (float(total[1]) / total[0]),
                "share_of_known_id_events": float(mean[1]) / float(total[1]),
            }
        return result


def checked_gold(gold: Path) -> dict[str, Any]:
    from citibike.storage import read_json

    report: dict[str, Any] = read_json(gold / "manifest.json")
    return report


def availability_table(path: Path) -> list[dict[str, Any]]:
    from datetime import date, datetime, timedelta

    from citibike.features.contracts import NY

    rows_out = []
    for month, when in checked(path)["availability_audit"]["dependency_months"]:
        year, number = map(int, month.split("-"))
        end = date(year + (number == 12), number % 12 + 1, 1)
        close = datetime.combine(end, datetime.min.time(), NY)
        available = datetime.fromisoformat(when)
        rows_out.append(
            {
                "month": month,
                "assumed_available_at": when,
                "days_after_month_end": (available - close) / timedelta(days=1),
            }
        )
    return rows_out


def readiness(root: Path, dataset: Path) -> Path:
    before = time.monotonic()
    data = checked(dataset)
    cohorts = populations_of(root, dataset, smoke=data["config"]["smoke"])
    work = research_root(root)
    prepared = {
        name: inputs(work, path, tuning=name == "cohort64")
        for name, path in cohorts.items()
    }
    identifier = identity(
        [VERSION, "readiness", {n: checked(p)["id"] for n, p in prepared.items()}]
    )

    def build(stage: Path) -> dict[str, Any]:
        profiles = {name: cohort_profile(path) for name, path in cohorts.items()}
        ids = {name: set(rows_of_stations(path)) for name, path in cohorts.items()}
        names = list(ids)
        nested = all(ids[a] <= ids[b] for a, b in zip(names, names[1:], strict=False))
        return {
            "kind": "decision_readiness",
            "version": VERSION,
            "cohorts": profiles,
            "nested_cohorts": nested,
            "folds": {
                name: fold_audit(path, cohorts[name]) for name, path in prepared.items()
            },
            "fold_plan": folds(data, tuning=True),
            "composition_bias": composition_bias(Path(data["gold_path"]), cohorts),
            "availability": availability_table(dataset),
            "preprocessing_rules": RULES,
            "gold_manifest_sha256": checksum(Path(data["gold_path"]) / "manifest.json"),
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
            "limits": "Holdout actuals are not summarised before the protocol; "
            "observed trip labels are not demand; a suspect closure is a statistical "
            "flag, not a confirmed station closure",
        }

    return create(work, "readiness", identifier, build, activate=False)


def rows_of_stations(path: Path) -> list[str]:
    with duckdb.connect() as db:
        return [
            r[0]
            for r in db.execute(
                "SELECT station_id FROM read_parquet(?)",
                [str(path / "stations.parquet")],
            ).fetchall()
        ]
