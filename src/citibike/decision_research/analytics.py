"""Decision analytics from accepted Gold and validation predictions; read-only."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import duckdb

from citibike.decision_research import checked_protocol, research_root
from citibike.decision_research.features import VERSION
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json

FIRST = "DATE '2024-10-01'"
LAST = "DATE '2026-10-01'"
Y1 = (FIRST, "DATE '2025-10-01'")
Y2 = ("DATE '2025-10-01'", LAST)
PEAKS = "(7,8,9,16,17,18,19)"
SEASON = """CASE WHEN month(m) IN (12,1,2) THEN 'winter'
    WHEN month(m) IN (3,4,5) THEN 'spring' WHEN month(m) IN (6,7,8) THEN 'summer'
    ELSE 'autumn' END"""


def rows(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cursor = db.execute(sql)
    names = [c[0] for c in cursor.description]
    return [
        {
            k: float(v) if type(v).__name__ == "Decimal" else v
            for k, v in zip(names, r, strict=True)
        }
        for r in cursor.fetchall()
    ]


def connect(gold: Path) -> duckdb.DuckDBPyConnection:
    report = read_json(gold / "manifest.json")
    db = duckdb.connect()
    db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='3GB'")
    for name in (
        "trip_monthly",
        "trip_hourly",
        "fact_station_hourly",
        "station_category_monthly",
        "route_monthly",
    ):
        db.read_parquet(
            [str(Path(m["path"]) / f"{name}.parquet") for m in report["months"]]
        ).create_view(name)
    db.execute(f"""CREATE VIEW hourly AS SELECT *,
        timezone('America/New_York',hour_utc) lt FROM trip_hourly
        WHERE timezone('America/New_York',hour_utc)>={FIRST}
        AND timezone('America/New_York',hour_utc)<{LAST}""")
    db.execute(f"""CREATE TABLE station_month AS SELECT station_id,local_month m,
        sum(departures)::BIGINT departures,sum(arrivals)::BIGINT arrivals
        FROM station_category_monthly
        WHERE local_month>={FIRST} AND local_month<{LAST} GROUP BY 1,2""")
    return db


def item(
    decision: str,
    definition: str,
    domain: str,
    grain: str,
    sql: dict[str, str],
    result: Any,
    limits: str,
    *,
    kind: str = "fact",
    coverage: Any = None,
) -> dict[str, Any]:
    return {
        "user_decision": decision,
        "definition": definition,
        "time_domain": domain,
        "grain": grain,
        "sql": sql,
        "coverage": coverage,
        "result": result,
        "limits": limits,
        "kind": kind,
    }


def trends(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    monthly = f"""SELECT strftime(local_month,'%Y-%m') AS "month",
        sum(n)::BIGINT completed_published_rides,
        sum(departures)::BIGINT departures,sum(arrivals)::BIGINT arrivals,
        sum(arrivals)::BIGINT-sum(departures)::BIGINT net_trip_flow,
        sum(n)::BIGINT-sum(departures)::BIGINT unknown_start_rides,
        sum(arrival_rows)::BIGINT-sum(arrivals)::BIGINT unknown_end_rides,
        sum(arrival_rows)::BIGINT ended_rides
        FROM trip_monthly WHERE local_month>={FIRST} AND local_month<{LAST}
        GROUP BY 1 ORDER BY 1"""
    exposure = """SELECT strftime(lt,'%Y-%m') AS "month",
        count(DISTINCT hour_utc) FILTER(WHERE n>0) observed_source_hours
        FROM hourly GROUP BY 1"""
    months = rows(db, monthly)
    hours = {r["month"]: r["observed_source_hours"] for r in rows(db, exposure)}
    for row in months:
        row["observed_source_hours"] = hours[row["month"]]
        row["rides_per_observed_source_hour"] = (
            row["completed_published_rides"] / row["observed_source_hours"]
        )
    by_month = {row["month"]: row for row in months}
    comparisons = []
    for row in months:
        year, number = row["month"].split("-")
        previous = by_month.get(f"{int(year) - 1}-{number}")
        if previous is None:
            continue
        rate, base = (
            row["rides_per_observed_source_hour"],
            previous["rides_per_observed_source_hour"],
        )
        comparisons.append(
            {
                "month": row["month"],
                "baseline": previous["month"],
                "rides": row["completed_published_rides"],
                "baseline_rides": previous["completed_published_rides"],
                "rides_change": (
                    row["completed_published_rides"]
                    / previous["completed_published_rides"]
                    - 1
                ),
                "rate_change_per_observed_hour": rate / base - 1,
                "comparable_hours": row["observed_source_hours"]
                == previous["observed_source_hours"],
            }
        )
    annual = rows(
        db,
        f"""SELECT CASE WHEN local_month<{Y2[0]} THEN '2024-10..2025-09'
            ELSE '2025-10..2026-09' END period,
            sum(n)::BIGINT rides,sum(duration_us)::HUGEINT duration_us,
            sum(duration_n)::BIGINT duration_n
            FROM trip_monthly WHERE local_month>={FIRST} AND local_month<{LAST}
            GROUP BY 1 ORDER BY 1""",
    )
    return item(
        "Руководитель: как меняется активность и где ломается сопоставимость",
        "METRICS §2 completed_published_rides, departures_s, arrivals_s, "
        "net_trip_flow_s; §4 изменение периода",
        "NYC [2024-10-01,2026-10-01)",
        "NY month; hour exposure from trip_hourly",
        {"monthly": monthly, "exposure": exposure},
        {"months": months, "year_over_year": comparisons, "annual": annual},
        "Net trip flow sums known station IDs only, so its city total reflects "
        "month-edge trips and unknown IDs; it is not inventory. A ride change is "
        "not revenue. Year-over-year is shown both raw and per observed source hour",
        coverage={
            "unknown_ids_shown_separately": True,
            "source_gap_hours_excluded_from_rate": True,
        },
    )


def patterns(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    hour_of_week = """SELECT isodow(lt)-1 weekday,hour(lt) local_hour,
        count(DISTINCT hour_utc) FILTER(WHERE n>0) source_hours,sum(n)::BIGINT rides,
        sum(departures)::BIGINT departures,sum(arrivals)::BIGINT arrivals,
        sum(n)/nullif(count(DISTINCT hour_utc) FILTER(WHERE n>0),0) rides_per_hour
        FROM hourly GROUP BY 1,2 ORDER BY 1,2"""
    seasonal = f"""WITH h AS (SELECT lt::DATE m,lt,hour_utc,n,departures,arrivals
        FROM hourly)
        SELECT {SEASON} season,CASE WHEN isodow(lt)>=6 THEN 'weekend'
            ELSE 'weekday' END day_type,hour(lt) local_hour,
        sum(n)/nullif(count(DISTINCT hour_utc) FILTER(WHERE n>0),0) rides_per_hour,
        sum(departures)/nullif(count(DISTINCT hour_utc) FILTER(WHERE n>0),0)
            departures_per_hour,
        sum(arrivals)/nullif(count(DISTINCT hour_utc) FILTER(WHERE n>0),0)
            arrivals_per_hour
        FROM h GROUP BY 1,2,3 ORDER BY 1,2,3"""
    period = f"""SELECT CASE WHEN lt<{Y2[0]} THEN '2024-10..2025-09'
            ELSE '2025-10..2026-09' END period,hour(lt) local_hour,
        CASE WHEN isodow(lt)>=6 THEN 'weekend' ELSE 'weekday' END day_type,
        sum(n)/nullif(count(DISTINCT hour_utc) FILTER(WHERE n>0),0) rides_per_hour
        FROM hourly GROUP BY 1,2,3 ORDER BY 1,2,3"""
    seasons = rows(db, seasonal)
    peaks: dict[str, dict[str, Any]] = {}
    for row in seasons:
        key = f"{row['season']}:{row['day_type']}"
        if key not in peaks or row["rides_per_hour"] > peaks[key]["rides_per_hour"]:
            peaks[key] = row
    return item(
        "Аналитик: какие часы и дни объясняют нагрузку, сравнимо между сезонами",
        "METRICS §4 пики, отдельно departures и arrivals; знаменатель — наблюдённые "
        "source hours",
        "NYC [2024-10-01,2026-10-01), NY local time",
        "hour of week; season x day type x hour; year x day type x hour",
        {"hour_of_week": hour_of_week, "seasonal": seasonal, "by_year": period},
        {
            "hour_of_week": rows(db, hour_of_week),
            "seasonal": seasons,
            "seasonal_peaks": peaks,
            "by_year": rows(db, period),
        },
        "Rates divide by observed source hours; 54 unknown source hours are not "
        "zero. Repeated and missing DST local hours keep their real UTC hours",
    )


def concentration(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    monthly = """WITH s AS (SELECT m,departures+arrivals e,
            row_number() OVER(PARTITION BY m
                ORDER BY departures+arrivals,station_id) r,
            count(*) OVER(PARTITION BY m) n,
            sum(departures+arrivals) OVER(PARTITION BY m) t
            FROM station_month WHERE departures+arrivals>0)
        SELECT strftime(m,'%Y-%m') AS "month",any_value(n) observed_ids,
        sum(e) FILTER(WHERE r>n-ceil(n*0.01))/any_value(t) top1pct_share,
        sum(e) FILTER(WHERE r>n-ceil(n*0.1))/any_value(t) top10pct_share,
        sum(e*e)/(any_value(t)*any_value(t)) hhi,
        (2*sum(r*e)/(any_value(n)*any_value(t)))-(any_value(n)+1.0)/any_value(n) gini
        FROM s GROUP BY m ORDER BY m"""
    churn = """WITH f AS (SELECT station_id,min(m) first_m,max(m) last_m,
            count(*) month_count FROM station_month
            WHERE departures+arrivals>0 GROUP BY 1)
        SELECT strftime(g,'%Y-%m') AS "month",
        (SELECT count(*) FROM f WHERE first_m=g) entered,
        (SELECT count(*) FROM f WHERE last_m=g) last_seen
        FROM (SELECT DISTINCT m g FROM station_month) ORDER BY g"""
    continuity = """WITH f AS (SELECT station_id,count(DISTINCT m) month_count
            FROM station_month WHERE departures+arrivals>0 GROUP BY 1)
        SELECT count(*) observed_ids,
        count(*) FILTER(WHERE month_count=24) all_24_months,
        count(*) FILTER(WHERE month_count<12) under_12_months FROM f"""
    return item(
        "Руководитель/аналитик: насколько спрос сосредоточен и как меняется состав",
        "Доля по METRICS §4: числитель и знаменатель после фильтра. Gini и HHI — "
        "описательные меры неравенства, не официальные KPI",
        "NY months 2024-10..2026-09",
        "NY month over known station IDs with at least one event",
        {"monthly": monthly, "churn": churn, "continuity": continuity},
        {
            "monthly": rows(db, monthly),
            "churn": rows(db, churn),
            "continuity": rows(db, continuity)[0],
        },
        "Observed IDs with a trip are not active_stations (METRICS §2). First "
        "observation of an ID does not prove a station opening; `last_seen` of the "
        "final months only marks the data end",
    )


def imbalance(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    station = f"""WITH s AS (SELECT station_id,sum(departures) d,sum(arrivals) a,
            count(*) month_count,
            count(*) FILTER(WHERE arrivals>departures) months_surplus,
            count(*) FILTER(WHERE arrivals<departures) months_deficit
            FROM station_month WHERE m>={Y2[0]} GROUP BY 1)
        SELECT station_id,d::BIGINT departures,a::BIGINT arrivals,
        (a-d)::BIGINT net_trip_flow,(a-d)/nullif(a+d,0) net_share,month_count,
        months_surplus,months_deficit FROM s WHERE month_count>=6
        ORDER BY abs(a-d) DESC,station_id LIMIT 40"""
    peak = """WITH h AS (SELECT station_id,timezone('America/New_York',hour_utc) lt,
            departures,arrivals FROM fact_station_hourly
            WHERE hour_utc>=TIMESTAMPTZ '2025-10-01 04:00:00+00'
            AND hour_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00'),
        w AS (SELECT station_id,
            sum(arrivals-departures) FILTER(WHERE hour(lt) IN (7,8,9)) am,
            sum(arrivals-departures) FILTER(WHERE hour(lt) IN (16,17,18,19)) pm,
            sum(departures+arrivals) e
            FROM h WHERE isodow(lt)<=5 GROUP BY 1)
        SELECT CASE WHEN am>0 AND pm<0 THEN 'morning_inflow_evening_outflow'
            WHEN am<0 AND pm>0 THEN 'morning_outflow_evening_inflow'
            WHEN am>=0 AND pm>=0 THEN 'inflow_both_peaks'
            ELSE 'outflow_both_peaks' END pattern,
        count(*) stations,sum(e)::BIGINT weekday_events,sum(am)::BIGINT am_net,
        sum(pm)::BIGINT pm_net FROM w WHERE am IS NOT NULL AND pm IS NOT NULL
        GROUP BY 1 ORDER BY 1"""
    persistent = """WITH h AS (SELECT station_id,
            date_trunc('month',timezone('America/New_York',hour_utc)) m,
            isodow(timezone('America/New_York',hour_utc))-1 w,
            hour(timezone('America/New_York',hour_utc)) lh,
            sum(departures+arrivals) e FROM fact_station_hourly
            WHERE hour_utc>=TIMESTAMPTZ '2025-10-01 04:00:00+00'
            AND hour_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00' GROUP BY 1,2,3,4),
        top AS (SELECT * FROM h QUALIFY row_number() OVER(
            PARTITION BY station_id,m ORDER BY e DESC,w,lh)=1),
        modal AS (SELECT station_id,w,lh,count(*) month_count,sum(e)::BIGINT events
            FROM top GROUP BY 1,2,3)
        SELECT station_id,w weekday,lh local_hour,month_count months_as_peak,events
        FROM modal WHERE month_count>=9 ORDER BY month_count DESC,events DESC,station_id
        LIMIT 30"""
    pairs = f"""WITH r AS (SELECT from_station a,to_station b,sum(n) n
            FROM route_monthly WHERE local_month>={Y2[0]} AND local_month<{LAST}
            GROUP BY 1,2)
        SELECT x.a from_station,x.b to_station,x.n::BIGINT route_volume,
        coalesce(y.n,0)::BIGINT reverse_volume,
        (x.n-coalesce(y.n,0))/(x.n+coalesce(y.n,0)) direction_asymmetry
        FROM r x LEFT JOIN r y ON x.a=y.b AND x.b=y.a WHERE x.a<>x.b
        ORDER BY x.n DESC,x.a,x.b LIMIT 40"""
    split = """WITH s AS (SELECT station_id,sum(departures) d,sum(arrivals) a
            FROM station_month GROUP BY 1)
        SELECT x.station_id short_id,t.station_id twin_id,x.d::BIGINT short_departures,
        x.a::BIGINT short_arrivals,t.d::BIGINT twin_departures,
        t.a::BIGINT twin_arrivals FROM s x JOIN s t ON t.station_id=x.station_id||'0'
        WHERE regexp_full_match(x.station_id,'\\d{4}\\.\\d') ORDER BY x.a DESC"""
    coverage = f"""SELECT sum(route_n)::BIGINT known_pairs,sum(n)::BIGINT rides,
        sum(route_n)/sum(n) route_coverage FROM trip_monthly
        WHERE local_month>={Y2[0]} AND local_month<{LAST}"""
    return item(
        "Операционная команда (симулируемая): где устойчивы дисбалансы и потоки",
        "METRICS §2 net_trip_flow_s, route_volume; net_share=(a-d)/(a+d) описательно",
        "NY months 2025-10..2026-09 (последние 12 полных)",
        "station; pattern; station x modal peak hour; directed station pair",
        {
            "station": station,
            "peak_patterns": peak,
            "persistent_peaks": persistent,
            "routes": pairs,
            "route_coverage": coverage,
            "id_split": split,
        },
        {
            "stations": flag_split(rows(db, station), rows(db, split)),
            "id_split_suspects": summarize_split(rows(db, split)),
            "peak_patterns": rows(db, peak),
            "persistent_peaks": rows(db, persistent),
            "routes": rows(db, pairs),
            "route_coverage": rows(db, coverage)[0],
        },
        "Net trip flow is not an inventory change: rebalancing and repairs are "
        "unobserved. Short IDs dddd.d with a dddd.d0 twin are mostly arrivals: "
        "probably one station split by a lost trailing zero in the source, so their "
        "net flow is an ID artifact, flagged, not corrected in accepted Silver. "
        "Hour-of-week peaks are unweighted by weekday counts. Route "
        "rows need both station IDs, so coverage is shown",
        coverage="See route_coverage",
    )


def flag_split(
    stations: list[dict[str, Any]], split: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    suspects = {r["short_id"] for r in split} | {r["twin_id"] for r in split}
    return [{**r, "suspected_id_split": r["station_id"] in suspects} for r in stations]


def summarize_split(split: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pairs": len(split),
        "short_departures": sum(r["short_departures"] for r in split),
        "short_arrivals": sum(r["short_arrivals"] for r in split),
        "arrival_dominated_pairs": sum(
            r["short_departures"] * 10 < r["short_arrivals"] for r in split
        ),
        "top": split[:10],
        "status": "suspected source ID artifact; raw CSV check NOT VERIFIED",
    }


def categories(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    monthly = f"""SELECT strftime(local_month,'%Y-%m') AS "month",vehicle,membership,
        sum(n)::BIGINT rides,sum(duration_us)::HUGEINT duration_us,
        sum(duration_n)::BIGINT duration_n FROM trip_monthly
        WHERE local_month>={FIRST} AND local_month<{LAST}
        GROUP BY 1,2,3 ORDER BY 1,2,3"""
    annual = f"""SELECT CASE WHEN local_month<{Y2[0]} THEN '2024-10..2025-09'
            ELSE '2025-10..2026-09' END period,vehicle,membership,
        sum(n)::BIGINT rides,
        sum(duration_us)/sum(duration_n)/1e6 average_duration_s,
        sum(duration_n)::BIGINT valid_durations FROM trip_monthly
        WHERE local_month>={FIRST} AND local_month<{LAST}
        GROUP BY 1,2,3 ORDER BY 1,2,3"""
    data = rows(db, annual)
    shares: dict[str, dict[str, Any]] = {}
    for dimension in ("vehicle", "membership"):
        for period in sorted({r["period"] for r in data}):
            part = [r for r in data if r["period"] == period]
            known = sum(r["rides"] for r in part if r[dimension] != "unknown")
            unknown = sum(r["rides"] for r in part if r[dimension] == "unknown")
            shares[f"{dimension}:{period}"] = {
                "denominator_known": known,
                "unknown": unknown,
                "shares": {
                    value: sum(r["rides"] for r in part if r[dimension] == value)
                    / known
                    for value in sorted({r[dimension] for r in part} - {"unknown"})
                },
            }
    return item(
        "Аналитик: как меняются категории и длительности при сопоставимых схемах",
        "METRICS §2 trip_category_share, average_duration; unknown вне знаменателя",
        "NY months 2024-10..2026-09, Oct-Sep years",
        "NY month x vehicle x membership; year x category",
        {"monthly": monthly, "annual": annual},
        {"annual": data, "shares": shares, "monthly": rows_compact(db, monthly)},
        "All 104 source CSV use schema citibike-modern-v1, so these 24 months are "
        "comparable; older `usertype` archives are not mixed in. Durations are not "
        "clipped: extreme durations stay in the mean",
        coverage={"schema": "citibike-modern-v1", "csv_files": 104},
    )


def rows_compact(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    result = rows(db, sql)
    for row in result:
        row["duration_us"] = int(row["duration_us"])
    return result


def coverage_gaps(db: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    gaps = """WITH g AS (SELECT range hour_utc FROM range(
            TIMESTAMPTZ '2024-10-01 04:00:00+00',
            TIMESTAMPTZ '2026-10-01 04:00:00+00',INTERVAL '1 hour'))
        SELECT g.hour_utc::VARCHAR hour_utc FROM g LEFT JOIN (SELECT hour_utc,sum(n) n
            FROM hourly GROUP BY 1) h USING(hour_utc)
        WHERE coalesce(h.n,0)=0 ORDER BY 1"""
    hours = [r["hour_utc"] for r in rows(db, gaps)]
    return {"source_hours_without_starts": len(hours), "hours": hours, "sql": gaps}


def trial_frames(
    db: duckdb.DuckDBPyConnection,
    outcome: dict[str, Any],
    population: str,
    names: tuple[str, ...],
) -> None:
    files = []
    for row in outcome["stages"]["S3_holdout"]:
        if (
            row["population"] == population
            and row["name"] in names
            and row["status"] == "success"
        ):
            files.append(
                (
                    row["name"],
                    row["fold"],
                    str(Path(row["path"]) / "predictions.parquet"),
                )
            )
    selects = " UNION ALL ".join(
        f"SELECT '{name}' model,'{fold}' fold,station_id,prediction_type,actual,"
        f"predicted,activity FROM read_parquet('{path}')"
        for name, fold, path in files
    )
    db.execute(f"CREATE OR REPLACE TABLE predictions AS {selects}")


def forecast_quality(
    db: duckdb.DuckDBPyConnection, outcome: dict[str, Any], gaps: dict[str, Any]
) -> dict[str, Any]:
    names = ("seasonal_naive", "catboost_selected", "lightgbm_selected")
    trial_frames(db, outcome, "cohort64", names)
    station = """WITH f AS (SELECT model,fold,station_id,prediction_type,
            avg(abs(predicted-actual)) mae FROM predictions GROUP BY 1,2,3,4)
        SELECT model,prediction_type,station_id,count(*) folds,avg(mae) mean_fold_mae,
        stddev_pop(mae) fold_std,stddev_pop(mae)/nullif(avg(mae),0) fold_cv
        FROM f GROUP BY 1,2,3 ORDER BY 1,2,6 DESC,3"""
    quality = rows(db, station)
    worst: dict[str, list[dict[str, Any]]] = {}
    for row in quality:
        worst.setdefault(f"{row['model']}:{row['prediction_type']}", []).append(row)
    return item(
        "ML-инженер: где данных мало, а прогноз нестабилен или плох",
        "ML_SPEC §5 MAE; fold_cv=std/mean MAE станции между holdout folds, "
        "описательная мера",
        "Holdout origins H0-H3 (validation), cohort64",
        "station x target x model",
        {"station": station, "source_gaps": gaps["sql"]},
        {
            "source_hours_without_starts": gaps["source_hours_without_starts"],
            "source_gap_hours": gaps["hours"],
            "least_stable_stations": {k: v[:10] for k, v in worst.items()},
            "stations_scored": len({r["station_id"] for r in quality}),
        },
        "Validation only, four 14-day windows; station CV over four folds is noisy "
        "and mixes season with model error. Cohort64 is not citywide",
        kind="prediction",
        coverage={"population": "cohort64", "folds": 4},
    )


def spearman_sql(table: str, left: str, right: str) -> str:
    return f"""WITH r AS (SELECT rank() OVER(ORDER BY {left}) a,
        rank() OVER(ORDER BY {right}) b FROM {table}
        WHERE {left} IS NOT NULL AND {right} IS NOT NULL)
        SELECT corr(a,b) rho,count(*) n FROM r"""


def errors_vs_flows(
    db: duckdb.DuckDBPyConnection, outcome: dict[str, Any], root: Path
) -> dict[str, Any]:
    trial_frames(db, outcome, "cohort64", ("seasonal_naive", "catboost_selected"))
    join = f"""CREATE OR REPLACE TABLE joined AS WITH f AS (
            SELECT model,station_id,avg(abs(predicted-actual)) mae,
            avg(predicted-actual) bias,sum(abs(predicted-actual))
                /nullif(sum(actual),0) wape FROM predictions GROUP BY 1,2),
        n AS (SELECT station_id,sum(departures) d,sum(arrivals) a FROM station_month
            WHERE m>={Y2[0]} GROUP BY 1)
        SELECT f.*,abs(a-d)/nullif(a+d,0) abs_net_share,a+d events
        FROM f JOIN n USING(station_id)"""
    db.execute(join)
    correlation = {}
    for model in ("seasonal_naive", "catboost_selected"):
        db.execute(
            f"CREATE OR REPLACE VIEW m AS SELECT * FROM joined WHERE model='{model}'"
        )
        for left, right in (
            ("wape", "abs_net_share"),
            ("abs(bias)", "abs_net_share"),
            ("mae", "events"),
        ):
            correlation[f"{model}:{left}~{right}"] = rows(
                db, spearman_sql("m", left, right)
            )[0]
    scenario = read_json(active_f5(root) / "manifest.json")
    stations = rows(db, "SELECT * FROM joined ORDER BY model,station_id")
    excluded = scenario["gate"].get("coverage", {}).get("exclusions", {})
    eligible = sorted({r["station_id"] for r in stations} - set(excluded))
    return item(
        "ML-инженер/операции: совпадают ли плохие прогнозы со станциями дисбаланса",
        "Спирмен между станционными метриками; ассоциация, не причинность "
        "(METRICS §5 запрещённые подмены)",
        "Holdout H0-H3 for errors; 2025-10..2026-09 for flows; F5 recorded as-of "
        + scenario["data_as_of"],
        "station, cohort64; F5 only 3/64 eligible stations",
        {"join": join, "spearman": spearman_sql("m", "x", "y")},
        {
            "spearman": correlation,
            "stations": stations,
            "f5_eligible_stations": eligible,
            "f5_eligible_rows": [r for r in stations if r["station_id"] in eligible],
            "f5_scope_note": "F5 eligible stations are too few for any correlation",
        },
        "Association only. WAPE and net share are both noisier at low volume. MAE "
        "against events is mostly scale. No claim that imbalance causes error, or "
        "that better forecasts change F5 results",
        kind="prediction",
    )


def active_f5(root: Path) -> Path:
    return Path(read_json(root / "f5/active.json")["path"])


def compact_cell(cell: dict[str, Any]) -> dict[str, Any]:
    return {
        "scale": cell["scale"],
        "order": cell["order"],
        "policies": {
            name: {
                "unserved_total_mean": policy["totals"]["unserved_total"]["mean"],
                "scenario_objective_mean": policy["totals"]["scenario_objective"][
                    "mean"
                ],
                "delivered_mean": policy["totals"]["delivered"]["mean"],
                "constraint_violations": policy["constraint_violations"],
                "repeats": policy["repeats"],
            }
            for name, policy in cell["policies"].items()
        },
    }


def f5_policies(root: Path) -> dict[str, Any]:
    path = active_f5(root)
    report = read_json(path / "manifest.json")
    return item(
        "Операционная команда (симулируемая): сравнить no-action, эвристику и "
        "оптимизатор в валидном сценарии",
        "METRICS §5 simulated_unserved_rides, simulated_rebalance_gain",
        f"F5 recorded as-of replay {report['data_as_of']}",
        "scale x event order x policy, 32 repeats per cell",
        {"source": "f5 result manifest comparisons, verified by outcomes.jsonl"},
        {
            "result_id": report["id"],
            "comparisons": [compact_cell(cell) for cell in report["comparisons"]],
            "assumptions": report["assumptions"],
        },
        "Simulation only. Scope 3/64 cohort stations at one recorded as-of, not "
        "citywide, not live risk, not a field effect or an economy claim",
        kind="simulation",
        coverage={"eligible_stations": 3, "cohort_stations": 64},
    )


def dashboard(sections: dict[str, dict[str, Any]], scores: list[Any]) -> dict[str, Any]:
    """Tables a later F6 page can read; every table states what kind of number it is."""
    trends_part = sections["1_trends"]["result"]
    flows = sections["4_imbalance"]["result"]
    return {
        "trend_monthly": {"kind": "fact", "rows": trends_part["months"]},
        "trend_year_over_year": {"kind": "fact", "rows": trends_part["year_over_year"]},
        "hour_of_week": {
            "kind": "fact",
            "rows": sections["2_patterns"]["result"]["hour_of_week"],
        },
        "station_imbalance": {"kind": "fact", "rows": flows["stations"]},
        "top_routes": {"kind": "fact", "rows": flows["routes"]},
        "concentration": {
            "kind": "fact",
            "rows": sections["3_concentration"]["result"]["monthly"],
        },
        "category_shares": {
            "kind": "fact",
            "rows": sections["5_categories"]["result"]["shares"],
        },
        "forecast_quality": {"kind": "prediction", "rows": scores},
        "scenario_policies": {
            "kind": "simulation",
            "rows": sections["8_f5_policies"]["result"]["comparisons"],
        },
        "note": "No new official KPI. Fact, prediction and simulation stay separate.",
    }


def analytics(root: Path, result: Path) -> Path:
    before = time.monotonic()
    outcome = checked(result)
    protocol = checked_protocol(Path(outcome["protocol"]), full=False)
    population = protocol["plan"]["populations"]["cohort64"]
    gold = Path(checked(Path(population["dataset"]))["gold_path"])
    identifier = identity([VERSION, "analytics", outcome["id"], script_hash()])

    def build(stage: Path) -> dict[str, Any]:
        db = connect(gold)
        try:
            gaps = coverage_gaps(db)
            builders: dict[str, Callable[[], dict[str, Any]]] = {
                "1_trends": lambda: trends(db),
                "2_patterns": lambda: patterns(db),
                "3_concentration": lambda: concentration(db),
                "4_imbalance": lambda: imbalance(db),
                "5_categories": lambda: categories(db),
                "6_coverage_quality": lambda: forecast_quality(db, outcome, gaps),
                "7_errors_vs_flows": lambda: errors_vs_flows(db, outcome, root),
                "8_f5_policies": lambda: f5_policies(root),
            }
            sections = {name: build_one() for name, build_one in builders.items()}
        finally:
            db.close()
        return {
            "kind": "decision_analytics",
            "outcome_id": outcome["id"],
            "gold_manifest": str(gold / "manifest.json"),
            "gold_manifest_sha256": checksum(gold / "manifest.json"),
            "gold_publication_id": read_json(gold / "manifest.json")["publication_id"],
            "f5_result": str(active_f5(root)),
            "sections": sections,
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(research_root(root), "analytics", identifier, build, activate=False)


def script_hash() -> str:
    return checksum(Path(__file__))
