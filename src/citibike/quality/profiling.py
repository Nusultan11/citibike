"""Reproducible SQL profiles of observed archive/Silver contents, without cleanup."""

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.storage import read_json, write_json

PROFILE_VERSION = "f2-profile-1"
RESEARCH_VERSION = "f2-research-1"


def label_grid(db: duckdb.DuckDBPyConnection, start: str, end: str) -> None:
    """Retrospective label population, matching F4's within-month envelope.

    Input events: station_id (nullable), hour_utc, n, prediction_type. IDs are
    identities, not installations. A city-hour without events is unobserved.
    Future bracketing is label eligibility only, never feature availability.
    """
    if db.execute("""SELECT count(*) FROM(SELECT station_id,hour_utc,
        prediction_type FROM events GROUP BY ALL HAVING count(*)<>1)""").fetchone() != (
        0,
    ):
        raise ValueError("Research event join explosion: duplicate grain")
    db.execute("""CREATE OR REPLACE TEMP TABLE city AS SELECT prediction_type,
        hour_utc,sum(n) n FROM events GROUP BY 1,2""")
    db.execute(
        """CREATE OR REPLACE TEMP TABLE grid AS WITH hours AS (
        SELECT hour_utc FROM generate_series(?::TIMESTAMPTZ,
            ?::TIMESTAMPTZ-INTERVAL '1 hour',INTERVAL '1 hour') t(hour_utc)),
        base AS (SELECT s.station_id,h.hour_utc,k.prediction_type,
            coalesce(e.n,0) n,coalesce(c.n,0)>0 source_observed,
            coalesce(b.n,0)>0 station_observed
        FROM stations s CROSS JOIN hours h
        CROSS JOIN (VALUES ('departures'),('arrivals')) k(prediction_type)
        LEFT JOIN events e ON e.station_id=s.station_id
            AND e.hour_utc=h.hour_utc AND e.prediction_type=k.prediction_type
        LEFT JOIN city c ON c.hour_utc=h.hour_utc
            AND c.prediction_type=k.prediction_type
        LEFT JOIN (SELECT station_id,hour_utc,sum(n) n FROM events
            WHERE station_id IS NOT NULL GROUP BY 1,2) b
            ON b.station_id=s.station_id AND b.hour_utc=h.hour_utc),
        bounded AS (SELECT *,max(CASE WHEN station_observed THEN hour_utc END)
            OVER w1 prior_event,min(CASE WHEN station_observed THEN hour_utc END)
            OVER w2 next_event FROM base
        WINDOW w1 AS(PARTITION BY station_id,prediction_type ORDER BY hour_utc
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
        w2 AS(PARTITION BY station_id,prediction_type ORDER BY hour_utc
            ROWS BETWEEN CURRENT ROW AND UNBOUNDED FOLLOWING))
        SELECT *,CASE WHEN NOT source_observed THEN 'source_gap'
            WHEN prior_event IS NULL OR next_event IS NULL
                OR hour_utc-prior_event>INTERVAL '7 days'
                OR next_event-hour_utc>INTERVAL '7 days' THEN 'station_gap'
            ELSE 'eligible' END eligibility,
        CASE WHEN source_observed AND hour_utc-prior_event<=INTERVAL '7 days'
            AND next_event-hour_utc<=INTERVAL '7 days' THEN n END actual
        FROM bounded""",
        [start, end],
    )


def research(snapshot: Path, work: Path) -> dict[str, Any]:
    """Bounded SQL EDA from immutable effective Silver, without rebuilding it."""
    import time
    from bisect import bisect_left, bisect_right
    from collections import defaultdict
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from citibike.ingestion.archive import checksum
    from citibike.ingestion.inventory import shift_month
    from citibike.processing.resources import peak_rss_bytes

    before = time.monotonic()
    manifest = read_json(snapshot / "manifest.json")
    periods = manifest["window"]
    zone = ZoneInfo("America/New_York")

    def boundary(month: str) -> str:
        return datetime.fromisoformat(month + "-01").replace(tzinfo=zone).isoformat()

    first, last = boundary(periods[0]), boundary(shift_month(periods[-1], 1))
    work.mkdir(parents=True, exist_ok=True)
    sql: list[str] = []
    with duckdb.connect() as db:
        db.execute("SET memory_limit='2GB'; SET threads=2; SET TimeZone='UTC'")
        db.execute("SET temp_directory=?", [str(work)])
        paths = [
            str(Path(p["path"]) / "silver.parquet") for p in manifest["partitions"]
        ]
        db.read_parquet(paths).create_view("trips")

        def rows(query: str) -> list[dict[str, Any]]:
            sql.append(query)
            return query_rows(db, query)

        def execute(query: str) -> None:
            sql.append(query)
            db.execute(query)

        lineage = rows("""SELECT count(*) rides,count(DISTINCT ride_id) unique_ids,
            count(DISTINCT source_object_id) sources,
            count(*) FILTER(WHERE source_object_id IS NULL OR schema_version IS NULL
                OR processed_at_utc IS NULL) missing_lineage FROM trips""")[0]
        if lineage["rides"] != manifest["canonical_rows"] or (
            lineage["rides"] != lineage["unique_ids"] or lineage["missing_lineage"]
        ):
            raise ValueError("Research Silver lineage/grain mismatch")
        # Aggregate each target separately; all source partitions contribute to
        # event months (arrivals may cross source-month boundaries).
        execute("""CREATE TEMP TABLE events AS SELECT 'departures' prediction_type,
            start_station_id station_id,date_trunc('hour',started_at_utc) hour_utc,
            count(*)::BIGINT n FROM trips GROUP BY 1,2,3
            UNION ALL SELECT 'arrivals',end_station_id,
            date_trunc('hour',ended_at_utc),count(*)::BIGINT
            FROM trips GROUP BY 1,2,3""")
        execute(f"""CREATE TEMP TABLE scoped AS SELECT * FROM events
            WHERE hour_utc>='{first}'::TIMESTAMPTZ
            AND hour_utc<'{last}'::TIMESTAMPTZ""")
        execute("""CREATE TEMP TABLE stations AS SELECT DISTINCT station_id
            FROM scoped WHERE station_id IS NOT NULL""")
        execute("""CREATE TEMP TABLE station_month AS SELECT station_id,
            strftime(timezone('America/New_York',hour_utc),'%Y-%m') period,
            sum(n)::BIGINT n FROM scoped WHERE station_id IS NOT NULL GROUP BY 1,2""")
        execute(f"""CREATE TEMP TABLE stable AS SELECT station_id FROM station_month
            GROUP BY 1 HAVING count(*)={len(periods)}""")
        universe = rows("SELECT count(*) ids FROM stations")[0]["ids"]
        stable = rows("SELECT count(*) ids FROM stable")[0]["ids"]
        provenance = rows("""SELECT prediction_type,sum(n)::BIGINT all_events,
            sum(n) FILTER(WHERE station_id IS NULL)::BIGINT unknown_id_events,
            sum(n) FILTER(WHERE station_id IS NOT NULL)::BIGINT known_id_events
            FROM scoped GROUP BY 1 ORDER BY 1""")
        outside = rows(f"""SELECT prediction_type,sum(n)::BIGINT events
            FROM events WHERE hour_utc<'{first}'::TIMESTAMPTZ
            OR hour_utc>='{last}'::TIMESTAMPTZ GROUP BY 1 ORDER BY 1""")
        geography = []
        for kind, prefix, time_field in [
            ("departures", "start", "started"),
            ("arrivals", "end", "ended"),
        ]:
            geography += rows(f"""SELECT '{kind}' prediction_type,
                CASE WHEN {prefix}_lat BETWEEN 40 AND 42
                    AND {prefix}_lng BETWEEN -75 AND -72
                    THEN floor({prefix}_lat*10)::VARCHAR||','||
                        floor({prefix}_lng*10)::VARCHAR
                    ELSE 'unknown_or_outside' END cell,
                count(*) n,count(DISTINCT {prefix}_station_id) ids
                FROM trips WHERE {time_field}_at_utc>='{first}'::TIMESTAMPTZ
                AND {time_field}_at_utc<'{last}'::TIMESTAMPTZ
                GROUP BY 1,2 ORDER BY n DESC,cell""")
        # One ID can have different historical endpoint coordinates. Report the
        # ambiguity rather than assigning an ID to a present-day borough.
        coordinate_churn = rows("""WITH cells AS(SELECT start_station_id id,
            floor(start_lat*10) lat,floor(start_lng*10) lng FROM trips
            WHERE start_station_id IS NOT NULL AND start_lat BETWEEN 40 AND 42
                AND start_lng BETWEEN -75 AND -72 GROUP BY 1,2,3)
            SELECT count(*) ids,count(*) FILTER(WHERE cells>1) multi_cell_ids
            FROM(SELECT id,count(*) cells FROM cells GROUP BY 1)""")[0]
        heterogeneity = rows("""SELECT prediction_type,count(*) ids,
            quantile_cont(n,[0,0.25,0.5,0.75,0.95,0.99,1]) event_quantiles,
            sum(n)::BIGINT events FROM(SELECT prediction_type,station_id,sum(n) n
            FROM scoped WHERE station_id IS NOT NULL GROUP BY 1,2) GROUP BY 1""")
        ranked = rows("""SELECT station_id,sum(n) n FROM scoped
            WHERE station_id IS NOT NULL GROUP BY 1 ORDER BY n,station_id""")
        sample_ids = (
            sorted(
                {
                    ranked[i]["station_id"]
                    for i in (0, len(ranked) // 2, len(ranked) - 1)
                }
            )
            if ranked
            else []
        )
        db.execute("CREATE TEMP TABLE oracle_ids(station_id VARCHAR)")
        if sample_ids:
            db.executemany(
                "INSERT INTO oracle_ids VALUES (?)", [(s,) for s in sample_ids]
            )
        oracle_counts: dict[tuple[str, str, datetime], int] = {}
        oracle_city: set[tuple[str, datetime]] = set()
        for kind, prefix, field in [
            ("departures", "start", "started"),
            ("arrivals", "end", "ended"),
        ]:
            direct = rows(f"""SELECT {prefix}_station_id station_id,
                date_trunc('hour',{field}_at_utc) hour_utc,count(*) n FROM trips
                WHERE {prefix}_station_id IN(SELECT * FROM oracle_ids)
                GROUP BY 1,2""")
            oracle_counts.update(
                {(kind, r["station_id"], r["hour_utc"]): r["n"] for r in direct}
            )
            city_direct = rows(f"""SELECT date_trunc('hour',{field}_at_utc) hour_utc
                FROM trips GROUP BY 1""")
            oracle_city.update((kind, r["hour_utc"]) for r in city_direct)
        oracle_mismatches, oracle_tested = 0, 0
        grid_duplicates = 0
        monthly, weekly, station_exclusions, peaks = [], [], [], []
        histograms: dict[str, list[dict[str, Any]]] = {}
        for period in periods:
            start, end = boundary(period), boundary(shift_month(period, 1))
            execute(f"""CREATE OR REPLACE TEMP VIEW events_month AS
                SELECT * FROM scoped WHERE hour_utc>='{start}'::TIMESTAMPTZ
                AND hour_utc<'{end}'::TIMESTAMPTZ""")
            # label_grid takes an events view; retain full sparse table separately.
            execute("ALTER TABLE events RENAME TO all_events")
            execute("CREATE TEMP VIEW events AS SELECT * FROM events_month")
            label_grid(db, start, end)
            execute("DROP VIEW events")
            execute("ALTER TABLE all_events RENAME TO events")
            cardinality = rows("""SELECT count(*) n,
                count(*)-count(DISTINCT(station_id,hour_utc,prediction_type)) duplicates
                FROM grid""")[0]
            expected = universe * int(
                (
                    datetime.fromisoformat(end) - datetime.fromisoformat(start)
                ).total_seconds()
                / 3600
            )
            # Offset-aware subtraction includes DST; UTC keys retain both fall hours.
            if cardinality["n"] != expected * 2 or cardinality["duplicates"]:
                raise ValueError("Research label join explosion or missing hour")
            grid_duplicates += cardinality["duplicates"]
            start_dt, end_dt = (
                datetime.fromisoformat(start),
                datetime.fromisoformat(end),
            )
            stamps = {
                s: sorted(
                    {
                        t
                        for (_, sid, t) in oracle_counts
                        if sid == s and start_dt <= t < end_dt
                    }
                )
                for s in sample_ids
            }
            oracle_rows = query_rows(
                db,
                """SELECT station_id,hour_utc,prediction_type,
                actual FROM grid WHERE station_id IN(SELECT * FROM oracle_ids)""",
            )
            for r in oracle_rows:
                station, stamp, kind = (
                    r["station_id"],
                    r["hour_utc"],
                    r["prediction_type"],
                )
                observed = stamps[station]
                lo, hi = bisect_right(observed, stamp) - 1, bisect_left(observed, stamp)
                valid = (
                    (kind, stamp) in oracle_city
                    and lo >= 0
                    and hi < len(observed)
                    and stamp - observed[lo] <= timedelta(days=7)
                    and observed[hi] - stamp <= timedelta(days=7)
                )
                actual = oracle_counts.get((kind, station, stamp), 0) if valid else None
                oracle_mismatches += int(actual != r["actual"])
                oracle_tested += 1
            monthly += rows(f"""SELECT '{period}' period,prediction_type,
                count(*) candidate_hours,count(actual) eligible_hours,
                count(*) FILTER(WHERE eligibility='source_gap') source_gap_hours,
                count(*) FILTER(WHERE eligibility='station_gap') station_gap_hours,
                count(*) FILTER(WHERE actual=0) zero_hours,
                count(*) FILTER(WHERE actual=1) single_event_hours,
                count(*) FILTER(WHERE actual BETWEEN 2 AND 4) rare_2_4_hours,
                count(*) FILTER(WHERE actual>=20) peak_ge20_hours,
                sum(actual)::BIGINT events,avg(actual) mean,
                quantile_cont(actual,[0.25,0.5,0.75,0.95,0.99,1]) quantiles,
                count(actual) FILTER(WHERE station_id IN(SELECT * FROM stable))
                    stable_eligible_hours,
                avg(actual) FILTER(WHERE station_id IN(SELECT * FROM stable))
                    stable_mean
                FROM grid GROUP BY 1,2 ORDER BY 2""")
            weekly += rows(f"""SELECT '{period}' period,prediction_type,
                isodow(timezone('America/New_York',hour_utc)) weekday,
                hour(timezone('America/New_York',hour_utc)) local_hour,
                count(actual) eligible_hours,sum(actual)::BIGINT events
                FROM grid GROUP BY 1,2,3,4 ORDER BY 2,3,4""")
            station_exclusions += rows(f"""SELECT '{period}' period,prediction_type,
                quantile_cont(missing_rate,[0,0.25,0.5,0.75,0.95,1])
                    missing_rate_quantiles,
                count(*) FILTER(WHERE eligible=0) ids_without_labels,
                count(*) FILTER(WHERE positive=0) ids_without_events
                FROM(SELECT station_id,prediction_type,
                    1-count(actual)::DOUBLE/count(*) missing_rate,
                    count(actual) eligible,count(*) FILTER(WHERE n>0) positive
                    FROM grid GROUP BY 1,2) GROUP BY 1,2 ORDER BY 2""")
            histograms[period] = rows("""SELECT prediction_type,
                CASE WHEN actual=0 THEN '0' WHEN actual=1 THEN '1'
                    WHEN actual<5 THEN '2-4' WHEN actual<10 THEN '5-9'
                    WHEN actual<20 THEN '10-19' ELSE '20+' END bin,count(*) n
                FROM grid WHERE actual IS NOT NULL GROUP BY 1,2 ORDER BY 1,2""")
            peaks += rows(f"""SELECT '{period}' period,station_id,prediction_type,
                hour_utc,actual FROM grid WHERE actual IS NOT NULL
                ORDER BY actual DESC,station_id,prediction_type,hour_utc LIMIT 4""")
        churn = rows("""SELECT period,count(*) observed_ids,
            count(*) FILTER(WHERE period=first_period) first_seen_ids,
            count(*) FILTER(WHERE period=last_period) last_seen_ids,
            count(*) FILTER(WHERE station_id IN(SELECT * FROM stable)) stable_ids
            FROM station_month JOIN(SELECT station_id,min(period) first_period,
                max(period) last_period FROM station_month GROUP BY 1) USING(station_id)
            GROUP BY 1 ORDER BY 1""")
        sets = {
            p: {
                r[0]
                for r in db.execute(
                    "SELECT station_id FROM station_month WHERE period=?", [p]
                ).fetchall()
            }
            for p in periods
        }
        for a, b in zip(periods, periods[1:], strict=False):
            row = next(r for r in churn if r["period"] == b)
            row.update(
                {
                    "entered_vs_previous": len(sets[b] - sets[a]),
                    "absent_vs_previous": len(sets[a] - sets[b]),
                    "jaccard_vs_previous": len(sets[a] & sets[b])
                    / len(sets[a] | sets[b])
                    if sets[a] | sets[b]
                    else None,
                }
            )
        # IQR fences describe high counts; no observations are discarded.
        tails = rows("""WITH q AS(SELECT prediction_type,quantile_cont(n,0.25) q1,
            quantile_cont(n,0.75) q3 FROM scoped WHERE station_id IS NOT NULL
            GROUP BY 1) SELECT prediction_type,first(q1) q1_positive,
            first(q3) q3_positive,count(*) FILTER(WHERE n>q3+1.5*(q3-q1))
                positive_hours_above_tukey_fence,max(n) max_positive
            FROM scoped JOIN q USING(prediction_type) WHERE station_id IS NOT NULL
            GROUP BY 1""")
        daily = rows("""SELECT prediction_type,
            timezone('America/New_York',hour_utc)::DATE local_date,sum(n)::BIGINT n
            FROM scoped GROUP BY 1,2 ORDER BY 1,2""")
        # Different path: independent GROUPING SETS directly on Silver, without
        # events/grid. Checks every known/unknown event-month total for both targets.
        independent = []
        for kind, prefix, field in [
            ("departures", "start", "started"),
            ("arrivals", "end", "ended"),
        ]:
            independent += rows(f"""SELECT '{kind}' prediction_type,
                strftime(timezone('America/New_York',{field}_at_utc),'%Y-%m') period,
                count(*) all_events,count({prefix}_station_id) known_events
                FROM trips WHERE {field}_at_utc>='{first}'::TIMESTAMPTZ
                AND {field}_at_utc<'{last}'::TIMESTAMPTZ GROUP BY 1,2 ORDER BY 2""")
        mismatches = 0
        for r in independent:
            m = next(
                x
                for x in monthly
                if (x["period"], x["prediction_type"])
                == (r["period"], r["prediction_type"])
            )
            mismatches += int(r["known_events"] != m["events"])
        if mismatches:
            raise ValueError("Independent Silver event-month reconciliation failed")
        if oracle_mismatches:
            raise ValueError("Independent Python label oracle failed")
    compact_weekly: dict[tuple[str, int, int], list[int]] = defaultdict(lambda: [0, 0])
    for r in weekly:
        key = (r["prediction_type"], r["weekday"], r["local_hour"])
        compact_weekly[key][0] += r["eligible_hours"]
        compact_weekly[key][1] += r["events"] or 0
    weekly = [
        {
            "prediction_type": k[0],
            "weekday": k[1],
            "local_hour": k[2],
            "eligible_hours": v[0],
            "events": v[1],
            "mean": v[1] / v[0] if v[0] else None,
        }
        for k, v in sorted(compact_weekly.items())
    ]
    shifts = []
    for i, current in enumerate(periods):
        for j in (i - 1, i - 12):
            if j < 0:
                continue
            previous = periods[j]
            for kind in ("departures", "arrivals"):
                a = {
                    r["bin"]: r["n"]
                    for r in histograms[previous]
                    if r["prediction_type"] == kind
                }
                b = {
                    r["bin"]: r["n"]
                    for r in histograms[current]
                    if r["prediction_type"] == kind
                }
                sa, sb = sum(a.values()), sum(b.values())
                shifts.append(
                    {
                        "from": previous,
                        "to": current,
                        "prediction_type": kind,
                        "comparison": "adjacent" if j == i - 1 else "year_on_year",
                        "total_variation": 0.5
                        * sum(
                            abs(a.get(k, 0) / sa - b.get(k, 0) / sb)
                            for k in a.keys() | b.keys()
                        )
                        if sa and sb
                        else None,
                    }
                )
    return {
        "version": RESEARCH_VERSION,
        "code_sha256": checksum(Path(__file__)),
        "snapshot_manifest_sha256": checksum(snapshot / "manifest.json"),
        "window_utc": [first, last],
        "lineage": lineage,
        "station_universe": universe,
        "stable_24_month_ids": stable,
        "target_provenance": provenance,
        "out_of_window_events": outside,
        "monthly": monthly,
        "weekly": weekly,
        "station_exclusions": station_exclusions,
        "churn": churn,
        "heterogeneity": heterogeneity,
        "geography": geography,
        "coordinate_churn": coordinate_churn,
        "histograms": histograms,
        "shifts": shifts,
        "peaks": sorted(peaks, key=lambda r: (-r["actual"], r["station_id"]))[:20],
        "outliers": tails,
        "daily": daily,
        "independent_event_month_counts": independent,
        "independent_mismatches": mismatches,
        "sql": sql,
        "label_oracle": {
            "method": "Direct Silver counts + stdlib bisect; no label_grid/F4 helpers",
            "sample_ids": sample_ids,
            "tested_station_hours": oracle_tested,
            "mismatches": oracle_mismatches,
            "scope": "low/median/high total activity IDs, every hour/type/month",
        },
        "join_duplicate_keys": grid_duplicates,
        "label_method": (
            "label_grid: source-hour >0, within-month both-target "
            "station bracketing <=7d"
        ),
        "measurements": {
            "elapsed_seconds": round(time.monotonic() - before, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "threads": 2,
            "duckdb_memory_limit": "2GB",
            "cold_cache": False,
        },
    }


def query_rows(db: duckdb.DuckDBPyConnection, query: str) -> list[dict[str, Any]]:
    cursor = db.execute(query)
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def profile(result: Path, target: Path) -> dict[str, Any]:
    quality = read_json(result / "quality.json")
    spill = target.parent / "profile-spill"
    spill.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as db:
        db.execute("SET memory_limit='2GB'; SET threads=2; SET TimeZone='UTC'")
        db.execute("SET temp_directory=?", [str(spill)])
        db.read_parquet(str(result / "silver.parquet")).create_view("trips")
        overview = query_rows(
            db,
            """SELECT count(*) canonical_rows,
            count(DISTINCT ride_id) unique_ride_ids,
            count(DISTINCT start_station_id) start_stations,
            count(DISTINCT end_station_id) end_stations,
            count(*) FILTER (WHERE start_station_id IS NOT NULL AND end_station_id
            IS NOT NULL) route_coverage_rows,
            count(DISTINCT (start_station_id,end_station_id)) FILTER (WHERE
            start_station_id IS NOT NULL AND end_station_id IS NOT NULL)
            unique_routes,
            min(duration_seconds) duration_min,max(duration_seconds) duration_max,
            quantile_cont(duration_seconds,[0.01,0.5,0.95,0.99]) duration_quantiles,
            min(start_lat) min_start_lat,max(start_lat) max_start_lat,
            min(start_lng) min_start_lng,max(start_lng) max_start_lng,
            count(*) FILTER (WHERE start_lat BETWEEN 40 AND 42 AND start_lng BETWEEN
            -75 AND -72) broad_nyc_box_rows,
            count(*) FILTER (WHERE
            date_trunc('month',started_at_local)<>date_trunc('month',ended_at_local))
            crosses_month_rows,
            count(*) FILTER (WHERE started_at_local::DATE<>ended_at_local::DATE)
            crosses_date_rows
            FROM trips""",
        )[0]
        hours = query_rows(
            db,
            """SELECT date_trunc('hour',started_at_utc) hour_utc,count(*) n
                                 FROM trips GROUP BY 1 ORDER BY 1""",
        )
        daily = query_rows(
            db,
            (
                "SELECT started_at_local::DATE local_date,count(*) n "
                "FROM trips GROUP BY 1 ORDER BY 1"
            ),
        )
        hour_of_week = query_rows(
            db,
            "SELECT extract(dow FROM started_at_local) dow,"
            "extract(hour FROM started_at_local) local_hour,count(*) n "
            "FROM trips GROUP BY 1,2 ORDER BY 1,2",
        )
        routes = query_rows(
            db,
            """SELECT start_station_id,end_station_id,count(*) n
            FROM trips WHERE start_station_id IS NOT NULL AND end_station_id IS NOT NULL
            GROUP BY 1,2 ORDER BY n DESC,start_station_id,end_station_id LIMIT 20""",
        )
        station_coverage = query_rows(
            db,
            """SELECT station_id,sum(n)::BIGINT event_rows FROM (
            SELECT start_station_id station_id,count(*) n FROM trips WHERE
            start_station_id IS NOT NULL GROUP BY 1
            UNION ALL SELECT end_station_id,count(*) n FROM trips WHERE
            end_station_id IS NOT NULL GROUP BY 1
            ) GROUP BY 1 ORDER BY 1""",
        )
        categories = {
            field: query_rows(
                db,
                f"SELECT {field} category,count(*) n FROM trips GROUP BY 1 ORDER BY 1",
            )
            for field in ["rideable_type", "member_casual"]
        }
        missing = {
            field: int(
                query_rows(db, f"SELECT count(*) n FROM trips WHERE {field} IS NULL")[
                    0
                ]["n"]
            )
            for field in [
                "start_station_id",
                "end_station_id",
                "start_lat",
                "start_lng",
                "end_lat",
                "end_lng",
                "rideable_type",
                "member_casual",
            ]
        }
    start = datetime.fromisoformat(quality["period_start"])
    end = datetime.fromisoformat(quality["period_end"])
    expected_hours = int((end - start).total_seconds() // 3600)
    covered_hours = sum(start <= h["hour_utc"] < end for h in hours)
    modified = quality["source"]["source_modified_at"]
    publication_time: datetime | None
    lag: float | None
    try:
        publication_time = datetime.fromisoformat(
            (modified or "").replace("Z", "+00:00")
        )
        lag = (publication_time - end).total_seconds()
    except ValueError:
        from email.utils import parsedate_to_datetime

        publication_time = parsedate_to_datetime(modified) if modified else None
        lag = (publication_time - end).total_seconds() if publication_time else None
    report = {
        "profile_version": PROFILE_VERSION,
        "period": quality["source"]["period"],
        "source_checksum": quality["source"]["checksum"],
        "pipeline_version": quality["pipeline_version"],
        "schema_version": quality["schema_version"],
        "files": quality["files"],
        "input_rows": quality["input_rows"],
        "dispositions": quality["dispositions"],
        "raw_missing_fields": quality["raw_missing_fields"],
        "raw_categories": quality["raw_categories"],
        "canonical_missing_fields": missing,
        "overview": overview,
        "categories": categories,
        "daily": daily,
        "hour_of_week": hour_of_week,
        "hourly": hours,
        "expected_hours": expected_hours,
        "covered_hours": covered_hours,
        "missing_hour_buckets": expected_hours - covered_hours,
        "top_routes": routes,
        "station_event_coverage": station_coverage,
        "reason_counts": quality["reason_counts"],
        "source_last_modified_minus_month_end_seconds": lag,
        "publication_delay_caveat": (
            "Last-Modified may describe a reissue, not original "
            "publication availability."
        ),
        "geography_rule": (
            "Broad box is descriptive only; out-of-box trips are not removed."
        ),
        "weather": {"status": "unavailable", "rows": 0, "join_performed": False},
        "train_serve_availability": (
            "Historical release times are not known from reissued "
            "objects; weather/forecasts absent; no ML training "
            "performed."
        ),
    }
    write_json(target, report)
    return report
