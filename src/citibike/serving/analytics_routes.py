"""Fast exact whole-window/month routes; values always parameterized."""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from psycopg import sql

from citibike.serving.analytics_models import (
    AnalyticsFilters,
    AnalyticsResponse,
    AnalyticsRow,
)
from citibike.serving.route_window_contract import (
    VERSION,
    month_table,
    window_id,
    window_table,
)


def read_routes(
    db: psycopg.Connection[dict[str, Any]],
    filters: AnalyticsFilters,
    manifest: dict[str, Any],
    response: AnalyticsResponse,
    base: dict[str, Any] | None,
) -> AnalyticsResponse | None:
    from citibike.serving.analytics_read import route_metrics

    zone = ZoneInfo("America/New_York")
    first = datetime.fromisoformat(manifest["period_start"]).astimezone(zone).date()
    end = datetime.fromisoformat(manifest["period_end"]).astimezone(zone).date()
    whole = filters.start_date == first and filters.end_date == end
    single = (
        filters.start_date is not None
        and filters.end_date is not None
        and filters.start_date.day == filters.end_date.day == 1
        and (filters.start_date.replace(day=28) + timedelta(days=4)).replace(day=1)
        == filters.end_date
    )
    exact = (filters.vehicle is None) == (filters.membership is None)
    if not whole and not (single and exact):
        return None
    relation = db.execute(
        "SELECT to_regclass('analytics_route_windows') object"
    ).fetchone()
    assert relation is not None
    if relation["object"] is None:
        return None
    identifier = window_id(manifest)
    found = db.execute(
        "SELECT manifest FROM analytics_route_windows WHERE window_id=%s", (identifier,)
    ).fetchone()
    if found is None:
        return None
    rollup = found["manifest"]
    vehicle, membership = filters.vehicle or "*", filters.membership or "*"
    stats = next(
        (
            s
            for s in rollup["slices"]
            if s["vehicle"] == vehicle and s["membership"] == membership
        ),
        None,
    )
    params: list[Any] = []
    clauses = []
    if whole:
        table = window_table(identifier)
        clauses = ["vehicle=%s", "membership=%s"]
        params = [vehicle, membership]
    else:
        assert filters.start_date is not None
        period = filters.start_date.strftime("%Y-%m")
        part = next((p for p in manifest["months"] if p["month"] == period), None)
        stats = next(
            (
                s
                for s in rollup["monthly_slices"]
                if s["month"] == period
                and s["vehicle"] == vehicle
                and s["membership"] == membership
            ),
            None,
        )
        if part is None:
            return None
        table = month_table(
            "route_monthly"
            if vehicle == membership == "*"
            else "route_category_monthly",
            part["id"],
        )
        if vehicle != "*":
            clauses = ["vehicle=%s", "membership=%s"]
            params = [vehicle, membership]
    for column, value in [
        ("from_station", filters.station_id),
        ("to_station", filters.to_station),
    ]:
        if value is not None:
            clauses.append(column + "=%s")
            params.append(value)
    clause = " AND ".join(clauses) or "true"
    table_sql = sql.Identifier(table)
    query = sql.SQL(
        "SELECT from_station,to_station,n,duration_us,duration_n FROM {} WHERE "
        + clause
        + " ORDER BY n DESC,from_station,to_station LIMIT %s OFFSET %s"
    ).format(table_sql)
    rows = db.execute(query, [*params, filters.limit, filters.offset]).fetchall()
    if filters.station_id or filters.to_station:
        stats = db.execute(
            sql.SQL(
                (
                    "SELECT count(*) groups,coalesce(sum(n),0) "
                    "n,coalesce(sum(duration_us),0) "
                    "duration_us,coalesce(sum(duration_n),0) duration_n FROM {} "
                    "WHERE "
                )
                + clause
            ).format(table_sql),
            params,
        ).fetchone()
    stats = stats or {"groups": 0, "n": 0, "duration_us": 0, "duration_n": 0}
    denominator = int(base["n"]) if base else 0
    response.total_groups = int(stats["groups"])
    response.metrics = (
        route_metrics(stats)
        if base and response.coverage["observed_city_start_hours"] > 0
        else None
    )
    if response.metrics:
        response.metrics.route_eligible_rides = int(stats["n"])
        response.metrics.route_coverage = (
            int(stats["n"]) / denominator if denominator else None
        )
    response.rows = [
        AnalyticsRow(
            key=r["from_station"] + "/" + r["to_station"],
            from_station=r["from_station"],
            to_station=r["to_station"],
            n=int(r["n"]),
            metrics=route_metrics(r),
        )
        for r in rows
    ]
    response.coverage.update(
        {
            "eligible_count_before_pagination": int(stats["n"]),
            "population_denominator": denominator,
            "eligible_coverage": int(stats["n"]) / denominator if denominator else None,
            "population": (
                "time/category/origin filters before destination and pagination"
            ),
            "metrics_population": (
                "both-known routes after all filters before pagination"
            ),
            "serving_rollup_version": VERSION,
            "serving_window_id": identifier,
            "plan": "exact immutable window slice"
            if whole
            else "unique monthly grain, no redundant GROUP BY",
        }
    )
    response.status = "ok" if rows else "empty"
    return response
