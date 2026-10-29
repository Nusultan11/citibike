"""Verified Gold → conservative station-hour labels and delayed PIT examples."""

import time
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.build import configured
from citibike.features.contracts import (
    CATALOG,
    MODE,
    SCHEMA,
    TYPES,
    VERSION,
    code_hash,
    holiday,
    month_available,
)
from citibike.forecasting.artifacts import create
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.serving.analytics_publish import validate_gold
from citibike.storage import identity


def settings(smoke: bool = False) -> dict[str, Any]:
    return {
        "train_start": "2025-01-01T00:00:00+00:00",
        "validation": [
            ["2025-04-01", "2025-04-03"],
            ["2025-05-01", "2025-05-03"],
            ["2025-06-01", "2025-06-03"],
        ]
        if smoke
        else [
            ["2025-06-01", "2025-06-15"],
            ["2025-09-01", "2025-09-15"],
            ["2025-12-01", "2025-12-15"],
            ["2026-03-01", "2026-03-15"],
        ],
        "final": ["2025-07-01", "2025-07-03"]
        if smoke
        else ["2026-09-01", "2026-10-01"],
        "max_train_rows": 1024 if smoke else 80000,
        "smoke": smoke,
    }


def availability_rows(report: dict[str, Any]) -> list[tuple[str, datetime]]:
    return [
        (
            p["month"],
            max(
                month_available(p["month"]),
                *(month_available(s["source_period"]) for s in p["silver"]),
            ),
        )
        for p in report["months"]
    ]


def prepare_tables(
    db: duckdb.DuckDBPyConnection, report: dict[str, Any], count: int
) -> dict[str, Any]:
    db.read_parquet(
        [str(Path(p["path"]) / "fact_station_hourly.parquet") for p in report["months"]]
    ).create_view("facts")
    db.read_parquet(
        [str(Path(p["path"]) / "trip_hourly.parquet") for p in report["months"]]
    ).create_view("city_input")
    db.execute(
        "CREATE TABLE month_availability(month_key VARCHAR, available_at TIMESTAMPTZ)"
    )
    db.executemany(
        "INSERT INTO month_availability VALUES (?,?)", availability_rows(report)
    )
    first = datetime.fromisoformat(report["period_start"])
    last = datetime.fromisoformat(report["period_end"])
    db.execute(
        "CREATE TABLE city AS SELECT hour_utc,sum(n) n,sum(arrival_rows) ends,"
        "sum(departures) departures,sum(arrivals) arrivals "
        "FROM city_input WHERE hour_utc>=? AND hour_utc<? GROUP BY 1",
        [first, last],
    )
    db.execute("""CREATE TABLE station_history AS SELECT station_id,
        min(hour_utc) first_event,max(hour_utc) last_event
        FROM facts WHERE departures+arrivals>0 GROUP BY 1""")
    seed_month = first.astimezone(
        __import__("zoneinfo").ZoneInfo("America/New_York")
    ).strftime("%Y-%m")
    legacy = 3 * max(1, count // 4)
    db.execute(
        """CREATE TABLE seed_activity AS WITH counts AS (
        SELECT station_id,sum(departures+arrivals) volume FROM facts
        WHERE strftime(timezone('America/New_York',hour_utc),'%Y-%m')=?
        GROUP BY 1 HAVING volume>0 AND count(DISTINCT
            timezone('America/New_York',hour_utc)::DATE)>=14), ranked AS (
        SELECT *,ntile(3) OVER(ORDER BY volume,station_id) tier FROM counts)
        SELECT *,row_number() OVER(PARTITION BY tier
            ORDER BY sha256(station_id||'42')) pick FROM ranked""",
        [seed_month],
    )
    db.execute(
        """CREATE TABLE cohort AS SELECT station_id,
        CASE tier WHEN 1 THEN 'low' WHEN 2 THEN 'middle' ELSE 'high' END activity,
        false enrolled_new FROM seed_activity WHERE pick<=?""",
        [legacy // 3],
    )
    db.execute(
        """INSERT INTO cohort SELECT station_id,'new',true
        FROM station_history h WHERE station_id NOT IN(SELECT station_id FROM cohort)
        AND strftime(timezone('America/New_York',first_event),'%Y-%m')>?
        AND strftime(timezone('America/New_York',first_event),'%Y-%m')>='2025-01'
        AND (SELECT count(DISTINCT timezone('America/New_York',f.hour_utc)::DATE)
            FROM facts f WHERE f.station_id=h.station_id
            AND strftime(timezone('America/New_York',f.hour_utc),'%Y-%m')=
                strftime(timezone('America/New_York',h.first_event),'%Y-%m'))>=7
        ORDER BY first_event::DATE,sha256(station_id||'42') LIMIT ?""",
        [seed_month, count - legacy],
    )
    # Enrollment must use the source month that first proves this ID, including
    # next-file dependencies for cross-month trips. Never use today's GBFS.
    db.execute(
        """CREATE TABLE stations AS SELECT c.*,h.first_event,h.last_event,
        CASE WHEN c.enrolled_new THEN a.available_at ELSE greatest(a.available_at,
            (SELECT available_at FROM month_availability
                WHERE month_key=?)) END known_at
        FROM cohort c JOIN station_history h USING(station_id)
        JOIN month_availability a ON a.month_key=
        strftime(timezone('America/New_York',h.first_event),'%Y-%m')""",
        [seed_month],
    )
    db.execute(
        """CREATE TABLE selected_facts AS SELECT f.station_id,hour_utc,
        departures,arrivals FROM facts f JOIN cohort USING(station_id)
        WHERE hour_utc>=? AND hour_utc<?""",
        [first, last],
    )
    db.execute(
        """CREATE TABLE dense AS WITH base AS (
        SELECT s.station_id,t.hour_utc,
        strftime(timezone('America/New_York',t.hour_utc),'%Y-%m') month_key,
        coalesce(f.departures,0) dep,coalesce(f.arrivals,0) arr,
        coalesce(c.n,0)>0 city_dep,coalesce(c.ends,0)>0 city_arr
        FROM stations s CROSS JOIN generate_series(?::TIMESTAMPTZ,
            ?::TIMESTAMPTZ-INTERVAL '1 hour',INTERVAL '1 hour') t(hour_utc)
        LEFT JOIN selected_facts f ON f.station_id=s.station_id
            AND f.hour_utc=t.hour_utc
        LEFT JOIN city c ON c.hour_utc=t.hour_utc), bounded AS (
        SELECT *,max(CASE WHEN dep+arr>0 THEN hour_utc END) OVER(
            PARTITION BY station_id,month_key ORDER BY hour_utc
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) prior_event,
        min(CASE WHEN dep+arr>0 THEN hour_utc END) OVER(
            PARTITION BY station_id,month_key ORDER BY hour_utc
            ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING) next_event
        FROM base)
        SELECT b.station_id,b.hour_utc,b.month_key,
        CASE WHEN city_dep AND hour_utc-prior_event<=INTERVAL '7 days'
            AND next_event-hour_utc<=INTERVAL '7 days' THEN dep END departures,
        CASE WHEN city_arr AND hour_utc-prior_event<=INTERVAL '7 days'
            AND next_event-hour_utc<=INTERVAL '7 days' THEN arr END arrivals,
        a.available_at label_available_at
        FROM bounded b JOIN month_availability a USING(month_key)""",
        [first, last],
    )
    cohorts = db.execute("SELECT * FROM stations ORDER BY station_id").fetchall()
    if not cohorts:
        raise ValueError("No observed station cohort")
    coverage = db.execute(
        """SELECT sum(f.departures),sum(f.arrivals)
        FROM facts f JOIN cohort USING(station_id)
        WHERE hour_utc>=? AND hour_utc<?""",
        [first, last],
    ).fetchone()
    city = db.execute(
        "SELECT sum(n),sum(ends),sum(departures),sum(arrivals) FROM city"
    ).fetchone()
    assert city is not None and coverage is not None
    return {
        "stations": len(cohorts),
        "city_starts": city[0],
        "city_ends": city[1],
        "unknown_start_ids": city[0] - city[2],
        "unknown_end_ids": city[1] - city[3],
        "cohort_departures": coverage[0],
        "cohort_arrivals": coverage[1],
        "departure_city_coverage": coverage[0] / city[2] if city[2] else None,
        "arrival_city_coverage": coverage[1] / city[3] if city[3] else None,
    }


def profiles(db: duckdb.DuckDBPyConnection) -> None:
    db.execute("""CREATE TABLE labels AS SELECT station_id,hour_utc,
        label_available_at,'departures' prediction_type,departures actual FROM dense
        UNION ALL SELECT station_id,hour_utc,label_available_at,'arrivals',arrivals
        FROM dense""")
    db.execute("""CREATE TABLE profile_states AS WITH monthly AS (
        SELECT station_id,prediction_type,label_available_at available_at,
        isodow(timezone('America/New_York',hour_utc))-1 weekday,
        hour(timezone('America/New_York',hour_utc)) local_hour,
        count(*) amount,sum(actual) total,max(hour_utc) max_event_at,
        arg_max(actual,hour_utc) last_count FROM labels WHERE actual IS NOT NULL
        GROUP BY ALL)
        SELECT station_id,prediction_type,available_at,weekday,local_hour,
        sum(total) OVER w/sum(amount) OVER w profile_mean,
        sum(amount) OVER w profile_count,max(max_event_at) OVER w max_event_at,
        arg_max(last_count,max_event_at) OVER w seasonal_naive
        FROM monthly WINDOW w AS(PARTITION BY station_id,prediction_type,
            weekday,local_hour ORDER BY available_at
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)""")
    db.execute("""CREATE TABLE station_states AS WITH monthly AS(
        SELECT station_id,prediction_type,label_available_at available_at,
        sum(actual) total,count(*) amount FROM labels WHERE actual IS NOT NULL
        GROUP BY ALL)
        SELECT station_id,prediction_type,available_at,
        sum(total) OVER w/sum(amount) OVER w station_mean FROM monthly
        WINDOW w AS(PARTITION BY station_id,prediction_type ORDER BY available_at
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)""")


def feature_query() -> str:
    return """WITH calendar AS (SELECT r.*,
        hour(target_hour_utc) utc_hour,
        hour(timezone('America/New_York',target_hour_utc)) local_hour,
        isodow(timezone('America/New_York',target_hour_utc))-1 weekday,
        month(timezone('America/New_York',target_hour_utc)) AS "month",
        (isodow(timezone('America/New_York',target_hour_utc))>=6)::INT weekend,
        is_holiday(timezone('America/New_York',target_hour_utc)::DATE)::INT holiday,
        (date_diff('minute',timezone('UTC',target_hour_utc),
            timezone('America/New_York',target_hour_utc))=-240)::INT dst
        FROM requests r)
        SELECT c.*,p.seasonal_naive,p.profile_mean,p.profile_count,p.max_event_at,
        greatest(p.available_at,s.known_at) feature_available_at,
        m.station_mean,s.activity,s.enrolled_new,s.known_at,s.last_event,
        (c.forecast_origin_utc-s.known_at<=INTERVAL '90 days') new_known
        FROM calendar c JOIN stations s USING(station_id)
        ASOF LEFT JOIN profile_states p ON c.station_id=p.station_id
        AND c.prediction_type=p.prediction_type AND c.weekday=p.weekday
        AND c.local_hour=p.local_hour AND c.forecast_origin_utc>=p.available_at
        ASOF LEFT JOIN station_states m ON c.station_id=m.station_id
        AND c.prediction_type=m.prediction_type
        AND c.forecast_origin_utc>=m.available_at"""


def build(
    root: Path, gold: Path, *, station_count: int = 64, smoke: bool = False
) -> Path:
    if not 4 <= station_count <= 256:
        raise ValueError("Bounded cohort must contain 4..256 stations")
    report = validate_gold(gold)
    config = settings(smoke)
    recipe = code_hash(Path(__file__).parent)
    identifier = identity(
        [VERSION, SCHEMA, recipe, report["publication_id"], station_count, config]
    )

    def calculate(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        with configured(stage.parent / "features.duckdb") as db:
            db.create_function("is_holiday", holiday)
            coverage = prepare_tables(db, report, station_count)
            profiles(db)
            db.execute(
                """CREATE TABLE requests AS SELECT s.station_id,
                o.origin forecast_origin_utc,
                o.origin+h*INTERVAL '1 hour' target_hour_utc,
                h::INT horizon_h,kind prediction_type FROM stations s
                CROSS JOIN generate_series(?::TIMESTAMPTZ,
                    ?::TIMESTAMPTZ-INTERVAL '1 day',INTERVAL '1 day') o(origin)
                CROSS JOIN range(1,25) horizons(h)
                CROSS JOIN (VALUES ('departures'),('arrivals')) k(kind)""",
                [config["train_start"], config["final"][1]],
            )
            db.execute("CREATE TABLE features AS " + feature_query())
            db.execute("""CREATE TABLE examples AS SELECT f.*,l.actual,
                l.label_available_at,
                CASE WHEN f.forecast_origin_utc<f.known_at THEN 'station_not_known'
                    WHEN l.actual IS NULL THEN 'missing_target'
                    WHEN seasonal_naive IS NULL OR station_mean IS NULL
                        THEN 'missing_available_profile' ELSE 'eligible' END eligibility
                FROM features f LEFT JOIN labels l ON f.station_id=l.station_id
                AND f.prediction_type=l.prediction_type
                AND f.target_hour_utc=l.hour_utc""")
            if db.execute("""SELECT count(*) FROM examples WHERE eligibility='eligible'
                AND (feature_available_at>forecast_origin_utc
                    OR max_event_at>=forecast_origin_utc)""").fetchone() != (0,):
                raise ValueError("PIT feature leakage detected")
            counts = dict(
                db.execute(
                    "SELECT eligibility,count(*) FROM examples GROUP BY 1"
                ).fetchall()
            )
            for name in (
                "dense",
                "stations",
                "profile_states",
                "station_states",
                "examples",
            ):
                db.execute(
                    f"COPY (SELECT * FROM {name} ORDER BY ALL) TO ? "
                    "(FORMAT PARQUET,COMPRESSION ZSTD)",
                    [str(stage / f"{name}.parquet")],
                )
            zeros = db.execute("""SELECT count(*) FILTER(WHERE actual=0),
                count(*) FILTER(WHERE actual IS NULL) FROM examples""").fetchone()
            duplicate = db.execute("""SELECT count(*) FROM (SELECT station_id,
                forecast_origin_utc,target_hour_utc,horizon_h,prediction_type
                FROM examples GROUP BY ALL HAVING count(*)<>1)""").fetchone()
            if duplicate != (0,):
                raise ValueError("ML example grain violated")
        return {
            "kind": "dataset",
            "data_mode": report["data_mode"],
            "availability_mode": MODE,
            "feature_schema": SCHEMA,
            "catalog": CATALOG,
            "recipe": recipe,
            "config": config,
            "gold_path": str(gold),
            "gold_publication_id": report["publication_id"],
            "gold_manifest_checksum": checksum(gold / "manifest.json"),
            "source_snapshot_id": report["source_snapshot_id"],
            "source_manifest_checksum": report["source_manifest_checksum"],
            "source_snapshot": report["source_snapshot"],
            "strict_source_available_at": max(
                report["created_at"], report["data_as_of"]
            ),
            "source_event_end": report["period_end"],
            "coverage": coverage,
            "eligibility": counts,
            "zero_labels": zeros[0] if zeros else 0,
            "missing_labels": zeros[1] if zeros else 0,
            "availability_audit": {
                "future_feature_rows": 0,
                "historical_first_publication": "unknown; not inferred from retrieval",
                "assumed_delay_days": 45,
                "dependency_months": availability_rows(report),
                "strict_historical_replay": (
                    "unavailable before immutable source creation"
                ),
            },
            "measurements": {
                "elapsed_seconds": time.monotonic() - before,
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }

    return create(root, "datasets", identifier, calculate)


def inference_features(
    path: Path, origin: datetime, stations: list[str]
) -> list[dict[str, Any]]:
    from citibike.forecasting.artifacts import checked

    checked(path)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        db.create_function("is_holiday", holiday)
        for name in ("stations", "profile_states", "station_states"):
            db.read_parquet(str(path / f"{name}.parquet")).create_view(name)
        db.execute("""CREATE TABLE requests(station_id VARCHAR,
            forecast_origin_utc TIMESTAMPTZ,target_hour_utc TIMESTAMPTZ,
            horizon_h INT,prediction_type VARCHAR)""")
        from citibike.features.contracts import target_hour

        db.executemany(
            "INSERT INTO requests VALUES (?,?,?,?,?)",
            [
                (station, origin, target_hour(origin, h), h, kind)
                for station in stations
                for kind in TYPES
                for h in range(1, 25)
            ],
        )
        # Unknown requested IDs are retained as unavailable by inference, not
        # manufactured by a zero-filling join.
        cur = db.execute(feature_query())
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
