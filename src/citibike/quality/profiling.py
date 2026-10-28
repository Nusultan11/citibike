"""Reproducible SQL profiles of observed archive/Silver contents, without cleanup."""

from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.storage import read_json, write_json

PROFILE_VERSION = "f2-profile-1"


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
