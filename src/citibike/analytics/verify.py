"""Real Silver → Gold → serving → HTTP → HTML evidence and measured workload."""

import platform
import re
import statistics
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import httpx
from psycopg import sql

from citibike.analytics.build import configured
from citibike.serving.analytics_models import AnalyticsFilters
from citibike.serving.analytics_publish import validate_gold
from citibike.serving.analytics_read import fact_name, where
from citibike.serving.db import connect
from citibike.serving.route_window_contract import month_table, window_id, window_table
from citibike.storage import read_json, write_json


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
        "p99_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))],
    }


def verify(path: Path, output: Path, *, benchmark: bool = False) -> Path:
    report = validate_gold(path)
    version = report["publication_id"]
    silver = read_json(Path(report["source_snapshot"]) / "manifest.json")
    # Independent direct Silver calculation: completed starts and arrivals have
    # different time predicates; exact UTC microseconds, no Gold helper query.
    import tempfile

    with (
        tempfile.TemporaryDirectory() as temporary,
        configured(Path(temporary) / "oracle.duckdb") as db,
    ):
        paths = [str(Path(p["path"]) / "silver.parquet") for p in silver["partitions"]]
        db.read_parquet(paths).create_view("raw")
        start = datetime.fromisoformat(report["period_start"])
        end = datetime.fromisoformat(report["period_end"])
        expected = db.execute(
            """SELECT
            count(*) FILTER(WHERE started_at_utc>=? AND started_at_utc<?) n,
            sum(epoch_us(ended_at_utc)-epoch_us(started_at_utc))
                FILTER(WHERE started_at_utc>=? AND started_at_utc<?) duration_us,
            count(*) FILTER(WHERE started_at_utc>=? AND started_at_utc<?
                AND start_station_id IS NOT NULL) departures,
            count(*) FILTER(WHERE ended_at_utc>=? AND ended_at_utc<?
                AND end_station_id IS NOT NULL) arrivals,
            count(*) FILTER(WHERE ended_at_utc>=? AND ended_at_utc<?) arrival_rows,
            count(*) FILTER(WHERE started_at_utc>=? AND started_at_utc<?
                AND start_station_id IS NOT NULL
                AND end_station_id IS NOT NULL) route_n,
            count(*) FILTER(WHERE started_at_utc>=? AND started_at_utc<?
                AND start_lat BETWEEN -90 AND 90 AND start_lng BETWEEN -180 AND 180
                AND isfinite(start_lat) AND isfinite(start_lng)) geo_n
            FROM raw WHERE disposition='accepted'""",
            [start, end] * 7,
        ).fetchone()
        assert expected is not None
        baseline = dict(
            zip(
                [
                    "n",
                    "duration_us",
                    "departures",
                    "arrivals",
                    "arrival_rows",
                    "route_n",
                    "geo_n",
                ],
                expected,
                strict=True,
            )
        )
        monthly = db.execute(
            """SELECT
            strftime(timezone('America/New_York',started_at_utc),'%Y-%m-01') bucket,
            count(*) n FROM raw WHERE started_at_utc>=? AND started_at_utc<?
            AND disposition='accepted' GROUP BY 1 ORDER BY 1""",
            [start, end],
        ).fetchall()
        station = db.execute(
            """SELECT start_station_id FROM raw
            WHERE started_at_utc>=? AND started_at_utc<?
            AND start_station_id IS NOT NULL GROUP BY 1
            ORDER BY count(*) DESC,start_station_id LIMIT 1""",
            [start, end],
        ).fetchone()
        assert station is not None
        top = str(station[0])
        sample_start = datetime(
            2026, 4, 1, tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        sample_end = datetime(
            2026, 5, 1, tzinfo=ZoneInfo("America/New_York")
        ).astimezone(UTC)
        sample_station = db.execute(
            """SELECT
            count(*) FILTER(WHERE started_at_utc>=? AND started_at_utc<?
                AND start_station_id=?) n,
            count(*) FILTER(WHERE ended_at_utc>=? AND ended_at_utc<?
                AND end_station_id=?) arrivals,
            sum(epoch_us(ended_at_utc)-epoch_us(started_at_utc)) FILTER(
                WHERE started_at_utc>=? AND started_at_utc<?
                AND start_station_id=?) duration_us FROM raw""",
            [sample_start, sample_end, top] * 3,
        ).fetchone()
        sample_routes = db.execute(
            """SELECT start_station_id,end_station_id,
            count(*) n,sum(epoch_us(ended_at_utc)-epoch_us(started_at_utc)) du
            FROM raw WHERE started_at_utc>=? AND started_at_utc<?
            AND start_station_id IS NOT NULL AND end_station_id IS NOT NULL
            GROUP BY 1,2 ORDER BY n DESC,1,2 LIMIT 3""",
            [sample_start, sample_end],
        ).fetchall()
        sample_categories = {}
        for dimension, column, known in [
            ("vehicle", "rideable_type", ("classic_bike", "electric_bike")),
            ("membership", "member_casual", ("member", "casual")),
        ]:
            rows = db.execute(
                f"""SELECT CASE WHEN {column} IN (?,?)
                THEN {column} ELSE 'unknown' END category,count(*) n FROM raw
                WHERE started_at_utc>=? AND started_at_utc<? GROUP BY 1""",
                [*known, start, end],
            ).fetchall()
            sample_categories[dimension] = {str(k): int(n) for k, n in rows}
        sample_peaks = {}
        for dimension, timestamp, station_column in [
            ("departures", "started_at_utc", "start_station_id"),
            ("arrivals", "ended_at_utc", "end_station_id"),
        ]:
            peak = db.execute(
                f"""SELECT
                strftime({timestamp},'%Y-%m-%d %H:00:00+00:00') bucket,count(*) n
                FROM raw WHERE {timestamp}>=? AND {timestamp}<?
                AND {station_column} IS NOT NULL GROUP BY 1
                ORDER BY n DESC,bucket LIMIT 1""",
                [start, end],
            ).fetchone()
            sample_peaks[dimension] = peak
    root_params = {"publication_id": version}
    evidence: dict[str, Any] = {
        "publication_id": version,
        "source_snapshot_id": report["source_snapshot_id"],
        "pipeline_version": report["pipeline_version"],
        "data_mode": report["data_mode"],
        "verified_at": datetime.now(UTC).isoformat(),
        "independent_silver": baseline,
        "independent_station_sample": {
            "station_id": top,
            "month": "2026-04",
            "values": sample_station,
        },
        "independent_route_sample": sample_routes,
        "independent_categories": sample_categories,
        "independent_peaks": sample_peaks,
        "gold_months": [
            {
                k: p[k]
                for k in (
                    "id",
                    "month",
                    "rows",
                    "checksums",
                    "metrics",
                    "independent_group_mismatches",
                    "measurements",
                )
            }
            for p in report["months"]
        ],
        "build_measurements": report["measurements"],
        "availability_manifest": report["availability"],
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "duckdb": duckdb.__version__,
            "workload": (
                "sequential HTTP on localhost, persistent client; no concurrent "
                "load; first request new session != cold OS cache"
            ),
        },
    }
    with httpx.Client(timeout=120) as client:
        kpi_response = client.get(
            "http://127.0.0.1:8000/v2/analytics/kpi", params=root_params
        )
        kpi_response.raise_for_status()
        kpi = kpi_response.json()
        actual = kpi["metrics"]
        f1_response = client.get("http://127.0.0.1:8000/v1/kpi")
        f1_response.raise_for_status()
        f1 = f1_response.json()
        if f1.get("metadata"):
            f1_id = f1["metadata"]["publication_id"]
            assert f1_id != version
            assert (
                client.get(
                    "http://127.0.0.1:8000/v2/analytics/kpi",
                    params={"publication_id": f1_id},
                ).status_code
                == 422
            )
            assert (
                client.get(
                    "http://127.0.0.1:8000/v1/kpi", params=root_params
                ).status_code
                == 422
            )
            evidence["f1_version_isolation"] = {
                "f1_id": f1_id,
                "f1_count": f1["completed_published_rides"],
                "cross_version_pins": "both rejected 422",
            }
        mapped = {
            "n": actual["completed_published_rides"],
            "duration_us": actual["duration_sum_us"],
            "departures": actual["departures"],
            "arrivals": actual["arrivals"],
            "arrival_rows": actual["arrivals"] + actual["unknown_end_station"],
            "route_n": actual["route_eligible_rides"],
            "geo_n": actual["geo_eligible_starts"],
        }
        if mapped != baseline:
            raise ValueError("Independent Silver → HTTP KPI mismatch")
        series = client.get(
            "http://127.0.0.1:8000/v2/analytics/timeseries",
            params={**root_params, "grain": "month"},
        )
        series.raise_for_status()
        observed = [
            (r["key"], r["metrics"]["completed_published_rides"])
            for r in series.json()["rows"]
        ]
        if observed != monthly:
            raise ValueError("Independent Silver → HTTP monthly mismatch")
        html = client.get("http://127.0.0.1:3000/analytics", params=root_params)
        html.raise_for_status()
        match = re.search(r'data-analytics-completed-rides="(\d+)"', html.text)
        if match is None or int(match[1]) != baseline["n"] or version not in html.text:
            raise ValueError("Real HTTP → rendered HTML reconciliation failed")
        gbfs = client.get(
            "http://127.0.0.1:8000/v2/analytics/availability", params=root_params
        )
        gbfs.raise_for_status()
        evidence.update(
            {
                "api_kpi": kpi,
                "monthly_reconciliation": observed,
                "html_count": int(match[1]),
                "gbfs": gbfs.json(),
                "reconciliation_mismatches": 0,
            }
        )
        sample_params = {
            **root_params,
            "start_date": "2026-04-01",
            "end_date": "2026-05-01",
        }
        station_response = client.get(
            "http://127.0.0.1:8000/v2/analytics/kpi",
            params={**sample_params, "station_id": top},
        )
        station_response.raise_for_status()
        station_metrics = station_response.json()["metrics"]
        if sample_station != (
            station_metrics["completed_published_rides"],
            station_metrics["arrivals"],
            station_metrics["duration_sum_us"],
        ):
            raise ValueError("Independent real station filter mismatch")
        route_response = client.get(
            "http://127.0.0.1:8000/v2/analytics/routes",
            params={**sample_params, "limit": 3},
        )
        route_response.raise_for_status()
        actual_routes = [
            (
                r["from_station"],
                r["to_station"],
                r["n"],
                r["metrics"]["duration_sum_us"],
            )
            for r in route_response.json()["rows"]
        ]
        if actual_routes != sample_routes:
            raise ValueError("Independent real route ranking mismatch")
        whole_routes = client.get(
            "http://127.0.0.1:8000/v2/analytics/routes",
            params={**root_params, "limit": 3},
        )
        whole_routes.raise_for_status()
        # Recount returned whole-window pairs directly in Silver. Full ranking
        # is additionally checked by 32 disjoint shards × 16 independent slices.
        with (
            tempfile.TemporaryDirectory() as temporary,
            configured(Path(temporary) / "routes.duckdb") as oracle,
        ):
            oracle.read_parquet(paths).create_view("silver")
            pair_checks = []
            for row in whole_routes.json()["rows"]:
                expected_pair = oracle.execute(
                    """SELECT count(*),
                    sum(epoch_us(ended_at_utc)-epoch_us(started_at_utc))
                    FROM silver WHERE disposition='accepted'
                    AND started_at_utc>=? AND started_at_utc<?
                    AND start_station_id=? AND end_station_id=?""",
                    [start, end, row["from_station"], row["to_station"]],
                ).fetchone()
                if expected_pair != (row["n"], row["metrics"]["duration_sum_us"]):
                    raise ValueError("Whole-window route Silver recount mismatch")
                pair_checks.append(
                    {
                        "from": row["from_station"],
                        "to": row["to_station"],
                        "silver": expected_pair,
                    }
                )
        evidence["whole_window_route_recounts"] = pair_checks
        category_response = client.get(
            "http://127.0.0.1:8000/v2/analytics/categories", params=root_params
        )
        category_response.raise_for_status()
        actual_categories = {
            dimension: {
                r["category"]: r["n"]
                for r in category_response.json()["rows"]
                if r["dimension"] == dimension
            }
            for dimension in sample_categories
        }
        if actual_categories != sample_categories:
            raise ValueError("Independent real category population mismatch")
        peak_response = client.get(
            "http://127.0.0.1:8000/v2/analytics/peaks",
            params={**root_params, "grain": "hour"},
        )
        peak_response.raise_for_status()
        if {
            r["dimension"]: (r["key"], r["n"]) for r in peak_response.json()["rows"]
        } != sample_peaks:
            raise ValueError("Independent real departure/arrival peak mismatch")
        evidence["filtered_http_reconciliation"] = (
            "station, route rankings/durations, categories and separate "
            "departure/arrival peaks matched independent Silver"
        )
        workloads = {
            "kpi_24_months": ("kpi", {}),
            "stations_24_months": ("stations", {}),
            "routes_24_months": ("routes", {}),
            "monthly_series_24_months": ("timeseries", {"grain": "month"}),
            "station_day": (
                "timeseries",
                {
                    "station_id": top,
                    "start_date": "2026-04-01",
                    "end_date": "2026-05-01",
                },
            ),
            "routes_month": (
                "routes",
                {"start_date": "2026-04-01", "end_date": "2026-05-01"},
            ),
            "routes_month_vehicle": (
                "routes",
                {
                    "start_date": "2026-04-01",
                    "end_date": "2026-05-01",
                    "vehicle": "electric_bike",
                },
            ),
            "routes_24_months_vehicle": ("routes", {"vehicle": "electric_bike"}),
            "station_routes": (
                "routes",
                {
                    "station_id": top,
                    "start_date": "2026-04-01",
                    "end_date": "2026-05-01",
                },
            ),
            "categories": ("categories", {}),
            "geo_day": (
                "geography",
                {"start_date": "2026-04-01", "end_date": "2026-04-02"},
            ),
        }
        results = {}
        for name, (endpoint, extra) in workloads.items():
            samples = []
            for _ in range(30 if benchmark else 2):
                before = time.perf_counter()
                response = client.get(
                    f"http://127.0.0.1:8000/v2/analytics/{endpoint}",
                    params={**root_params, **extra},
                )
                response.raise_for_status()
                if response.json()["publication"]["publication_id"] != version:
                    raise ValueError("Mixed HTTP publication")
                samples.append((time.perf_counter() - before) * 1000)
            results[name] = {
                "samples": len(samples),
                "first_request_ms": samples[0],
                "warm": percentiles(samples[1:]),
                "all_requests": percentiles(samples),
            }
        evidence["http_latency"] = results
    with connect(readonly=True) as db:
        evidence["postgres_version"] = db.execute("SELECT version() version").fetchone()
        evidence["postgres_bytes"] = db.execute(
            "SELECT pg_database_size(current_database()) bytes"
        ).fetchone()
        evidence["f3_relation_bytes"] = db.execute("""SELECT
            sum(pg_total_relation_size(relid)) bytes FROM pg_stat_user_tables
            WHERE left(relname,2)='a_' OR left(relname,10)='analytics_'""").fetchone()
        plans = {}
        for label, filters in {
            "all_months_kpi": AnalyticsFilters(publication_id=version),
            "station_april": AnalyticsFilters(
                publication_id=version,
                station_id=top,
                start_date=date(2026, 4, 1),
                end_date=date(2026, 5, 1),
            ),
        }.items():
            name = fact_name(filters, report)
            clause, params = where(filters, report, name)
            plans[label] = db.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT sum(n) "
                f"FROM analytics_{name} WHERE {clause}",
                params,
            ).fetchone()
        evidence["explain_analyze"] = plans
        april = AnalyticsFilters(
            publication_id=version,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 5, 1),
        )
        queries = {
            "daily_series": (
                "trip_daily",
                april,
                "local_date,sum(n) n",
                " GROUP BY 1 ORDER BY 1",
            ),
            "categories": (
                "trip_monthly",
                AnalyticsFilters(publication_id=version),
                "vehicle,membership,sum(n) n",
                " GROUP BY 1,2 ORDER BY 1,2",
            ),
            "geography_day": (
                "geo_category_daily",
                april.model_copy(update={"end_date": date(2026, 4, 2)}),
                "cell_lat,cell_lon,sum(n) n",
                " AND event_type='start' GROUP BY 1,2 "
                "ORDER BY sum(n) DESC,1,2 LIMIT 20",
            ),
        }
        sql_times = {}
        for label, (name, filters, select, tail) in queries.items():
            clause, params = where(filters, report, name)
            query = f"SELECT {select} FROM analytics_{name} WHERE {clause}{tail}"
            samples = []
            for _ in range(3):
                before = time.perf_counter()
                db.execute(query, params).fetchall()
                samples.append((time.perf_counter() - before) * 1000)
            sql_times[label] = {
                "same_connection_ms": samples,
                "cache": "no cache reset; first and subsequent executions",
            }
            plans[label] = db.execute(
                "EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query, params
            ).fetchone()
        evidence["sql_latency"] = sql_times
        route_window = db.execute(
            "SELECT manifest FROM analytics_route_windows WHERE window_id=%s",
            (window_id(report),),
        ).fetchone()
        assert route_window is not None
        evidence["route_serving_rollup"] = route_window["manifest"]
        april_partition = next(p for p in report["months"] if p["month"] == "2026-04")
        for label, table, clause, params in [
            (
                "routes_24_months",
                window_table(window_id(report)),
                "vehicle=%s AND membership=%s",
                ["*", "*"],
            ),
            (
                "routes_month",
                month_table("route_monthly", april_partition["id"]),
                "true",
                [],
            ),
            (
                "station_routes",
                month_table("route_monthly", april_partition["id"]),
                "from_station=%s",
                [top],
            ),
        ]:
            route_query = sql.SQL(
                "SELECT from_station,to_station,n,duration_us,duration_n "
                "FROM {} WHERE "
                + clause
                + " ORDER BY n DESC,from_station,to_station LIMIT 20"
            ).format(sql.Identifier(table))
            plans[label] = db.execute(
                sql.SQL("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) ") + route_query, params
            ).fetchone()
            samples = []
            for _ in range(3):
                before = time.perf_counter()
                db.execute(route_query, params).fetchall()
                samples.append((time.perf_counter() - before) * 1000)
            sql_times[label] = {
                "same_connection_ms": samples,
                "cache": "first and subsequent; no cache reset",
            }
    evidence["serving_runs"] = [
        read_json(p)
        for p in sorted((path.parent.parent / "serving-runs").glob("*.json"))
    ]
    evidence["parquet_bytes"] = sum(
        p.stat().st_size
        for month in report["months"]
        for p in Path(month["path"]).glob("*.parquet")
    )
    evidence["cold_os_cache"] = (
        "NOT VERIFIED: shared host/DB; no reproducible isolated cache reset. "
        "First-connection and warm measurements recorded separately."
    )
    write_json(output, evidence)
    return output
