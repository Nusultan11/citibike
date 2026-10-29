"""Pinned, bounded aggregate-only queries. User values are always parameters."""

from collections import Counter
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import psycopg

from citibike.analytics.contracts import MARTS, MEASURES
from citibike.serving.analytics_models import (
    AnalyticsFilters,
    AnalyticsMetadata,
    AnalyticsMetrics,
    AnalyticsResponse,
    AnalyticsRow,
    AvailabilityFilters,
    AvailabilityMetrics,
    ComparisonResult,
)
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.serving.read import InvalidFilter

NY = ZoneInfo("America/New_York")


def metadata(
    db: psycopg.Connection[dict[str, Any]], version: str | None
) -> tuple[dict[str, Any], AnalyticsMetadata] | None:
    try:
        row = db.execute(
            """SELECT manifest,published_at FROM analytics_publications
            WHERE publication_id=coalesce(%s,(SELECT publication_id
            FROM analytics_active WHERE singleton))""",
            (version,),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    if row is None:
        if version:
            raise InvalidFilter("Analytics publication not found; F1 IDs are separate")
        return None
    manifest = row["manifest"]
    fields = {k: manifest[k] for k in AnalyticsMetadata.model_fields if k in manifest}
    fields["published_at"] = row["published_at"]
    first_month = (
        datetime.fromisoformat(manifest["period_start"])
        .astimezone(NY)
        .strftime("%Y-%m")
    )
    end_month = (
        datetime.fromisoformat(manifest["period_end"]).astimezone(NY).strftime("%Y-%m")
    )
    default_starts = sum(
        p["metrics"]["n"]
        for p in manifest["months"]
        if first_month <= p["month"] < end_month
    )
    fields["coverage"] = {
        "source_months": [p["month"] for p in manifest["months"]],
        "data_as_of_kind": manifest["data_as_of_kind"],
        "excluded_from_silver": (
            "invalid, conflicting IDs and exact duplicates were excluded by"
            " F2; canonical Silver includes starts outside default interval"
        ),
        "geo_mapping": (
            "event WGS84 0.01 degree grid, DECIMAL(12,8) then floor; districts unknown"
        ),
        "historical_gbfs": False,
        "completed_rides_in_default_window": default_starts,
        "starts_outside_default_window": manifest["accepted_silver_rows"]
        - default_starts,
        "input_rows": manifest.get("input_rows"),
        "excluded_silver_rows": (
            manifest["input_rows"] - manifest["accepted_silver_rows"]
            if manifest.get("input_rows") is not None
            else None
        ),
    }
    return manifest, AnalyticsMetadata.model_validate(fields)


def bounds(
    filters: AnalyticsFilters, manifest: dict[str, Any]
) -> tuple[date, date, datetime, datetime]:
    lower = datetime.fromisoformat(manifest["period_start"]).astimezone(NY).date()
    upper = datetime.fromisoformat(manifest["period_end"]).astimezone(NY).date()
    start, end = filters.start_date or lower, filters.end_date or upper
    if start < lower or end > upper or start >= end:
        raise InvalidFilter("Date range outside published NYC source window")
    return (
        start,
        end,
        datetime.combine(start, time(), NY).astimezone(UTC),
        datetime.combine(end, time(), NY).astimezone(UTC),
    )


def metrics(row: dict[str, Any]) -> AnalyticsMetrics:
    n = int(row["n"])
    duration_n = int(row["duration_n"])
    return AnalyticsMetrics(
        completed_published_rides=n,
        duration_sum_us=int(row["duration_us"]),
        duration_count=duration_n,
        average_duration_seconds=float(row["duration_us"]) / duration_n / 1_000_000
        if duration_n
        else None,
        departures=int(row.get("departures", 0)),
        arrivals=int(row.get("arrivals", 0)),
        net_flow=int(row.get("arrivals", 0)) - int(row.get("departures", 0)),
        unknown_start_station=n - int(row.get("departures", n)),
        unknown_end_station=int(row.get("arrival_rows", 0))
        - int(row.get("arrivals", 0)),
        route_eligible_rides=int(row["route_n"]) if "route_n" in row else None,
        route_coverage=float(row["route_n"]) / n if n and "route_n" in row else None,
        geo_eligible_starts=int(row["geo_n"]) if "geo_n" in row else None,
        geo_coverage=float(row["geo_n"]) / n if n and "geo_n" in row else None,
    )


def route_metrics(row: dict[str, Any]) -> AnalyticsMetrics:
    count = int(row["duration_n"])
    return AnalyticsMetrics(
        completed_published_rides=int(row["n"]),
        duration_sum_us=int(row["duration_us"]),
        duration_count=count,
        average_duration_seconds=float(row["duration_us"]) / count / 1e6
        if count
        else None,
    )


def query_parts(manifest: dict[str, Any], start: date, end: date) -> list[str]:
    return [
        p["id"]
        for p in manifest["months"]
        if start.strftime("%Y-%m")
        <= p["month"]
        <= (end - timedelta(days=1)).strftime("%Y-%m")
    ]


def where(
    filters: AnalyticsFilters,
    manifest: dict[str, Any],
    name: str,
    *,
    station: bool = True,
    destination: bool = False,
) -> tuple[str, list[Any]]:
    mart = MARTS[name]
    start, end, utc_start, utc_end = bounds(filters, manifest)
    time_bounds = (utc_start, utc_end) if mart.time_key == "hour_utc" else (start, end)
    clauses = ["partition_id=ANY(%s)", f"{mart.time_key}>=%s", f"{mart.time_key}<%s"]
    params: list[Any] = [query_parts(manifest, start, end), *time_bounds]
    for field in ("vehicle", "membership"):
        value = getattr(filters, field)
        if value is not None and field in mart.keys:
            clauses.append(f"{field}=%s")
            params.append(value)
    if station and filters.station_id:
        key = (
            "station_id"
            if "station_id" in mart.keys
            else "from_station"
            if "from_station" in mart.keys
            else "station_key"
        )
        if key in mart.keys:
            clauses.append(f"{key}=%s")
            params.append(filters.station_id)
    if destination and filters.to_station:
        clauses.append("to_station=%s")
        params.append(filters.to_station)
    return " AND ".join(clauses), params


def full_months(filters: AnalyticsFilters, manifest: dict[str, Any]) -> bool:
    start, end, _, _ = bounds(filters, manifest)
    return start.day == end.day == 1


def fact_name(
    filters: AnalyticsFilters, manifest: dict[str, Any], *, hourly: bool = False
) -> str:
    grain = (
        "hourly" if hourly else "monthly" if full_months(filters, manifest) else "daily"
    )
    return ("station_category_" if filters.station_id else "trip_") + grain


def totals(
    db: psycopg.Connection[dict[str, Any]],
    filters: AnalyticsFilters,
    manifest: dict[str, Any],
) -> dict[str, Any] | None:
    name = fact_name(filters, manifest)
    clause, params = where(filters, manifest, name)
    row = db.execute(
        "SELECT count(*) groups,"
        + ",".join(f"coalesce(sum({c}),0) {c}" for c in MEASURES)
        + f" FROM analytics_{name} WHERE {clause}",
        params,
    ).fetchone()
    return row if row and row["groups"] else None


def observed_hours(
    db: psycopg.Connection[dict[str, Any]],
    filters: AnalyticsFilters,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    clause, params = where(
        filters.model_copy(update={"vehicle": None, "membership": None}),
        manifest,
        "trip_hourly",
        station=False,
    )
    row = db.execute(
        "SELECT count(DISTINCT hour_utc) n FROM analytics_trip_hourly "
        f"WHERE {clause} AND n>0",
        params,
    ).fetchone()
    _, _, start, end = bounds(filters, manifest)
    expected = int((end - start).total_seconds() / 3600)
    observed = int(row["n"]) if row else 0
    return {
        "observed_city_start_hours": observed,
        "expected_utc_hours": expected,
        "unobserved_city_start_hours": expected - observed,
        "missing_policy": (
            "unobserved hours are absent, not zero filled; public completed"
            " trips do not measure attempted demand"
        ),
    }


def read(
    kind: str, filters: AnalyticsFilters, settings: DatabaseSettings | None = None
) -> AnalyticsResponse:
    if filters.to_station and kind != "routes":
        raise InvalidFilter("to_station is only applicable to routes")
    if (filters.compare_start or filters.compare_end) and kind != "compare":
        raise InvalidFilter("Comparison dates only apply to compare")
    try:
        with connect(settings, readonly=True) as db:
            found = metadata(db, filters.publication_id)
            if found is None:
                return AnalyticsResponse(
                    status="empty", filters=filters, message="No F3 publication"
                )
            manifest, publication = found
            start, end, utc_start, utc_end = bounds(filters, manifest)
            filters = filters.model_copy(
                update={
                    "publication_id": publication.publication_id,
                    "start_date": start,
                    "end_date": end,
                }
            )
            response = AnalyticsResponse(
                status="ok", publication=publication, filters=filters
            )
            if kind == "publication":
                return response
            base = totals(db, filters, manifest)
            response.metrics = metrics(base) if base else None
            response.status = "ok" if base else "empty"
            response.coverage = observed_hours(db, filters, manifest)
            if kind == "kpi":
                return response
            if kind in ("timeseries", "peaks"):
                hourly = filters.grain == "hour"
                if hourly and (end - start).days > 31 and kind != "peaks":
                    raise InvalidFilter("Hourly series is bounded to 31 local days")
                name = fact_name(filters, manifest, hourly=hourly)
                if not hourly:
                    name = (
                        "station_category_" if filters.station_id else "trip_"
                    ) + "daily"
                clause, params = where(filters, manifest, name)
                key = (
                    "hour_utc"
                    if hourly
                    else "local_date"
                    if filters.grain == "day"
                    else "date_trunc('month',local_date)::date"
                )
                sums = ",".join(f"sum({c}) {c}" for c in MEASURES)
                rows = db.execute(
                    f"SELECT {key} key,{sums} FROM analytics_{name} "
                    f"WHERE {clause} GROUP BY 1 ORDER BY 1 LIMIT 18000",
                    params,
                ).fetchall()
                if kind == "peaks":
                    for measure in ("departures", "arrivals"):
                        if rows:
                            peak = max(rows, key=lambda r: int(r[measure]))
                            response.rows.append(
                                AnalyticsRow(
                                    key=str(peak["key"]),
                                    dimension=measure,
                                    n=int(peak[measure]),
                                    metrics=metrics(peak),
                                )
                            )
                    response.coverage["tie_policy"] = (
                        "earliest interval among equal maxima; "
                        "UTC hour or NYC date/month"
                    )
                    response.coverage["peak_grain"] = filters.grain
                else:
                    values = {str(r["key"]): metrics(r) for r in rows}
                    cursor: date | datetime = utc_start if hourly else start
                    stop: date | datetime = utc_end if hourly else end
                    if not hourly and filters.grain == "month":
                        cursor = start.replace(day=1)
                    while cursor < stop:
                        key = str(cursor)
                        response.rows.append(
                            AnalyticsRow(key=key, metrics=values.get(key))
                        )
                        if hourly:
                            cursor += timedelta(hours=1)
                        elif filters.grain == "month":
                            cursor = (
                                cursor.replace(day=28) + timedelta(days=4)
                            ).replace(day=1)
                        else:
                            cursor += timedelta(days=1)
                    response.coverage["bucket_policy"] = (
                        "UTC hour; NYC day/month. Counts clipped to requested "
                        "half-open interval; null metrics for unobserved buckets."
                    )
                return response
            if kind == "categories":
                name = fact_name(filters, manifest)
                clause, params = where(filters, manifest, name)
                for dimension in ("vehicle", "membership"):
                    rows = db.execute(
                        f"SELECT {dimension} category,sum(n) n "
                        f"FROM analytics_{name} WHERE {clause} "
                        "GROUP BY 1 HAVING sum(n)>0 ORDER BY 1",
                        params,
                    ).fetchall()
                    known = sum(int(r["n"]) for r in rows if r["category"] != "unknown")
                    unknown = sum(
                        int(r["n"]) for r in rows if r["category"] == "unknown"
                    )
                    response.rows.extend(
                        AnalyticsRow(
                            key=f"{dimension}:{r['category']}",
                            dimension=dimension,
                            category=r["category"],
                            n=int(r["n"]),
                            known_denominator=known,
                            unknown_count=unknown,
                            known_share=float(r["n"]) / known
                            if known and r["category"] != "unknown"
                            else None,
                        )
                        for r in rows
                    )
                return response
            if kind in ("stations", "routes", "geography"):
                monthly = full_months(filters, manifest)
                if (
                    kind in ("routes", "geography")
                    and (end - start).days > 31
                    and not filters.station_id
                    and not (kind == "routes" and monthly)
                ):
                    raise InvalidFilter(
                        "Geography and non-calendar route scans require "
                        "<=31 days or "
                        "station_id"
                    )
                if kind == "stations":
                    name = "station_category_" + ("monthly" if monthly else "daily")
                    keys = ["station_id"]
                elif kind == "routes":
                    category = (
                        filters.vehicle is not None or filters.membership is not None
                    )
                    name = ("route_category_" if category else "route_") + (
                        "monthly" if monthly else "daily"
                    )
                    if name == "route_daily":
                        name = "fact_route_daily"
                    keys = ["from_station", "to_station"]
                else:
                    name = "geo_category_daily"
                    keys = ["cell_lat", "cell_lon"]
                clause, params = where(
                    filters, manifest, name, destination=kind == "routes"
                )
                if kind == "geography":
                    clause += " AND event_type=%s"
                    params.append(filters.event_type)
                sums = ",".join(f"sum({c}) {c}" for c in MARTS[name].measures)
                rows = db.execute(
                    f"SELECT {','.join(keys)},{sums},count(*) OVER() total "
                    f"FROM analytics_{name} WHERE {clause} "
                    f"GROUP BY {','.join(keys)} "
                    f"ORDER BY sum(n) DESC,{','.join(keys)} LIMIT %s OFFSET %s",
                    [*params, filters.limit, filters.offset],
                ).fetchall()
                # Empty pages retain the actual filtered group count.
                total = (
                    db.execute(
                        f"SELECT count(*) n FROM (SELECT 1 FROM analytics_{name} "
                        f"WHERE {clause} GROUP BY {','.join(keys)}) groups",
                        params,
                    ).fetchone()
                    if not rows
                    else None
                )
                response.total_groups = (
                    int(rows[0]["total"]) if rows else int(total["n"]) if total else 0
                )
                denominator = int(base["n"]) if base else 0
                if kind == "geography" and filters.event_type == "end":
                    denominator = int(base["arrival_rows"]) if base else 0
                selected = db.execute(
                    "SELECT "
                    + ",".join(
                        f"coalesce(sum({c}),0) {c}" for c in MARTS[name].measures
                    )
                    + f" FROM analytics_{name} WHERE {clause}",
                    params,
                ).fetchone()
                response.coverage.update(
                    {
                        "eligible_count_before_pagination": int(selected["n"])
                        if selected
                        else 0,
                        "population_denominator": denominator,
                        "population": (
                            "time/category/origin filters before destination "
                            "and pagination"
                        )
                        if kind == "routes"
                        else "selected event side before pagination",
                        "eligible_coverage": float(selected["n"]) / denominator
                        if selected and denominator
                        else None,
                    }
                )
                if kind == "routes" and selected and base is not None:
                    response.metrics = route_metrics(selected)
                    response.metrics.route_eligible_rides = int(selected["n"])
                    response.metrics.route_coverage = (
                        int(selected["n"]) / denominator if denominator else None
                    )
                    response.coverage["metrics_population"] = (
                        "both-known routes after all filters, before pagination"
                    )
                elif kind == "geography":
                    response.metrics = None
                for r in rows:
                    row_metrics = (
                        route_metrics(r)
                        if kind == "routes"
                        else metrics(r)
                        if kind == "stations"
                        else None
                    )
                    response.rows.append(
                        AnalyticsRow(
                            key="/".join(str(r[k]) for k in keys),
                            metrics=row_metrics,
                            n=int(r["n"]),
                            **{k: r[k] for k in keys},
                        )
                    )
                response.status = "ok" if rows else "empty"
                return response
            if kind == "compare":
                if filters.compare_start is None or filters.compare_end is None:
                    raise InvalidFilter("Provide compare_start and compare_end")
                other = filters.model_copy(
                    update={
                        "start_date": filters.compare_start,
                        "end_date": filters.compare_end,
                    }
                )
                a, b, c, d = bounds(other, manifest)

                def weekdays(lo: date, hi: date) -> Counter[int]:
                    return Counter(
                        (lo + timedelta(days=i)).weekday()
                        for i in range((hi - lo).days)
                    )

                same = (
                    (end - start) == (b - a)
                    and (utc_end - utc_start) == (d - c)
                    and weekdays(start, end) == weekdays(a, b)
                )
                previous = totals(db, other, manifest)
                previous_coverage = observed_hours(db, other, manifest)
                same = (
                    same
                    and not response.coverage["unobserved_city_start_hours"]
                    and not previous_coverage["unobserved_city_start_hours"]
                )
                prev_n = int(previous["n"]) if previous else None
                response.comparison = ComparisonResult.model_validate(
                    {
                        "comparable": same,
                        "previous_start": str(a),
                        "previous_end": str(b),
                        "previous_rides": prev_n,
                        "previous_coverage": previous_coverage,
                        "relative_change": (int(base["n"]) - prev_n) / prev_n
                        if same and base and prev_n
                        else None,
                        "reason": None
                        if same and base and prev_n
                        else (
                            "N/A: requires equal local days, UTC hours, "
                            "weekday mix, full "
                            "observed-hour coverage and nonzero baseline"
                        ),
                    }
                )
                return response
            raise InvalidFilter("Unsupported analytics query")
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None


def availability(
    filters: AvailabilityFilters, settings: DatabaseSettings | None = None
) -> AnalyticsResponse:
    try:
        with connect(settings, readonly=True) as db:
            found = metadata(db, filters.publication_id)
            applied = AnalyticsFilters(
                publication_id=filters.publication_id, station_id=filters.station_id
            )
            if found is None:
                return AnalyticsResponse(
                    status="empty", filters=applied, message="No F3 publication"
                )
            manifest, publication = found
            info = manifest["availability"]
            response = AnalyticsResponse(
                status="unavailable",
                filters=applied.model_copy(
                    update={"publication_id": publication.publication_id}
                ),
                publication=publication,
            )
            if not info["period_start"] or not info["period_end"]:
                response.message = "No captured GBFS observations"
                return response
            start = filters.start or datetime.fromisoformat(info["period_start"])
            end = filters.end or datetime.fromisoformat(info["period_end"])
            if end <= start:
                response.message = "Insufficient observation window"
                return response
            target = (end - start).total_seconds() * (
                1 if filters.station_id else info["expected_stations"]
            )
            clause = "publication_id=%s AND valid_from<%s AND valid_to>%s"
            params: list[Any] = [publication.publication_id, end, start]
            if filters.station_id:
                clause += " AND station_id=%s"
                params.append(filters.station_id)
            row = db.execute(
                f"""WITH clipped AS (SELECT *,extract(epoch FROM
                least(valid_to,%s)-greatest(valid_from,%s)) seconds
                FROM analytics_availability WHERE {clause})
                SELECT coalesce(sum(seconds),0) observed,
                sum(seconds*rentable_bikes) bikes,sum(seconds*returnable_docks) docks,
                coalesce(sum(CASE WHEN rentable_bikes IS NOT NULL THEN seconds
                    ELSE 0 END),0) rent_seconds,
                coalesce(sum(CASE WHEN returnable_docks IS NOT NULL THEN seconds
                    ELSE 0 END),0) return_seconds,
                sum(CASE WHEN rentable_bikes=0 THEN seconds ELSE 0 END) empty_seconds,
                sum(CASE WHEN returnable_docks=0 THEN seconds ELSE 0 END) full_seconds,
                count(DISTINCT source_updated) updates,max(source_updated) updated,
                min(source_updated) first_update,max(valid_to) last_valid
                FROM clipped""",
                [end, start, *params],
            ).fetchone()
            assert row is not None
            observed = float(row["observed"])
            coverage = observed / target if target else None
            sufficient = bool(
                coverage is not None
                and coverage >= 0.95
                and row["updates"] >= 2
                and row["updated"] - row["first_update"] >= timedelta(seconds=60)
            )
            captured_updated = max(
                (
                    datetime.fromisoformat(c["feeds"]["station_status"]["last_updated"])
                    for c in info["collections"]
                ),
                default=row["updated"],
            )
            freshness = (
                (datetime.now(UTC) - captured_updated).total_seconds()
                if captured_updated
                else None
            )
            rent_seconds = float(row["rent_seconds"])
            return_seconds = float(row["return_seconds"])
            # Remove confirmed non-operating time from each operation's target;
            # unknown gaps remain in the target and are never assumed stopped.
            rent_target = target - observed + rent_seconds
            return_target = target - observed + return_seconds
            rent_coverage = rent_seconds / rent_target if rent_target else None
            return_coverage = return_seconds / return_target if return_target else None
            instant = end - timedelta(microseconds=1)
            instant_clause = "publication_id=%s AND valid_from<=%s AND valid_to>%s"
            instant_params: list[Any] = [publication.publication_id, instant, instant]
            if filters.station_id:
                instant_clause += " AND station_id=%s"
                instant_params.append(filters.station_id)
            active = db.execute(
                "SELECT count(*) known,count(*) FILTER(WHERE "
                "rentable_bikes IS NOT NULL OR returnable_docks IS NOT NULL) n "
                "FROM analytics_availability WHERE " + instant_clause,
                instant_params,
            ).fetchone()
            active_n = int(active["n"]) if active else 0
            known_n = int(active["known"]) if active else 0
            expected = 1 if filters.station_id else info["expected_stations"]
            response.availability = AvailabilityMetrics.model_validate(
                {
                    "source": "captured F2 GBFS; separate from historical trips",
                    "station_id_namespace": (
                        "GBFS station UUID; trip aliases are not substituted"
                    ),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "observed_station_seconds": observed,
                    "target_station_seconds": target,
                    "observed_coverage": coverage,
                    "rent_observed_station_seconds": rent_seconds,
                    "return_observed_station_seconds": return_seconds,
                    "rent_target_station_seconds": rent_target,
                    "return_target_station_seconds": return_target,
                    "rent_observed_coverage": rent_coverage,
                    "return_observed_coverage": return_coverage,
                    "rentable_bikes_time_weighted": float(row["bikes"]) / rent_seconds
                    if rent_seconds
                    else None,
                    "returnable_docks_time_weighted": float(row["docks"])
                    / return_seconds
                    if return_seconds
                    else None,
                    "empty_station_time_share": float(row["empty_seconds"])
                    / rent_seconds
                    if rent_seconds
                    and sufficient
                    and rent_coverage is not None
                    and rent_coverage >= 0.95
                    else None,
                    "full_station_time_share": float(row["full_seconds"])
                    / return_seconds
                    if return_seconds
                    and sufficient
                    and return_coverage is not None
                    and return_coverage >= 0.95
                    else None,
                    "active_stations": active_n
                    if expected and known_n == expected
                    else None,
                    "active_stations_at": instant.isoformat(),
                    "active_stations_policy": (
                        "operating stations at selected instant; N/A without "
                        "complete network coverage"
                    ),
                    "time_weighted_population": (
                        "mean per observed operating station; separate rent/return "
                        "valid station-seconds; not a network total"
                    ),
                    "scope": (
                        "captured operator GBFS feed; jurisdiction not mapped; "
                        "denominator is all observed IDs, separate from NYC trips"
                    ),
                    "status_freshness_sec": freshness,
                    "last_captured_status_updated": captured_updated,
                    "last_valid_source_updated": row["updated"],
                    "valid_interval_source_age_sec": (
                        (datetime.now(UTC) - row["updated"]).total_seconds()
                        if row["updated"]
                        else None
                    ),
                    "freshness_status": "stale"
                    if freshness is None or freshness > 60
                    else "fresh",
                    "distinct_updates": row["updates"],
                    "availability_version": info["availability_version"],
                    "policy": info["policy"],
                    "sufficient_coverage": sufficient,
                }
            )
            response.status = (
                "stale"
                if sufficient and (freshness is None or freshness > 60)
                else "ok"
                if sufficient
                else "unavailable"
            )
            response.message = (
                "N/A for empty/full ratios without sufficient operating-time "
                "coverage; confirmed non-operating statuses remain observed."
                if sufficient
                and (
                    rent_coverage is None
                    or return_coverage is None
                    or min(rent_coverage, return_coverage) < 0.95
                )
                else None
                if sufficient
                else (
                    "N/A: insufficient valid temporal/network coverage; gaps and "
                    "stale observations excluded"
                )
            )
            return response
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None
