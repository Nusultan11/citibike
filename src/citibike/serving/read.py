"""Read-only, pinned-version SQL; no source-file access in the serving path."""

from datetime import UTC, datetime, time
from typing import Any

import psycopg

from citibike.contracts import NY
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.serving.models import (
    AppliedFilters,
    HourPoint,
    KpiResponse,
    Publication,
    PublicationResponse,
    QueryFilters,
    SeriesResponse,
)


class InvalidFilter(ValueError):
    pass


def publication_row(
    connection: psycopg.Connection[dict[str, Any]],
    version: str | None,
) -> Publication | None:
    try:
        row = connection.execute(
            """
            SELECT p.metadata,p.published_at FROM publications p
            WHERE p.id=coalesce(%s,(SELECT publication_id FROM active_publication
                                   WHERE singleton=true))
        """,
            (version,),
        ).fetchone()
    except psycopg.errors.UndefinedTable:
        return None
    if not row:
        if version:
            raise InvalidFilter("Publication not found")
        return None
    return Publication.model_validate(
        {**row["metadata"], "published_at": row["published_at"]}
    )


def get_publication(settings: DatabaseSettings | None = None) -> PublicationResponse:
    try:
        with connect(settings, readonly=True) as connection:
            publication = publication_row(connection, None)
            return PublicationResponse(
                status="ok" if publication else "empty", publication=publication
            )
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None


def filters_for(query: QueryFilters, publication: Publication) -> AppliedFilters:
    if query.start_date and query.end_date:
        start = datetime.combine(query.start_date, time.min, NY).astimezone(UTC)
        end = datetime.combine(query.end_date, time.min, NY).astimezone(UTC)
    else:
        start, end = publication.period_start, publication.period_end
    if start < publication.period_start or end > publication.period_end:
        raise InvalidFilter("Requested dates are outside published coverage")
    return AppliedFilters(start=start, end=end, station_id=query.station_id)


def get_series(
    query: QueryFilters, settings: DatabaseSettings | None = None
) -> SeriesResponse:
    try:
        with connect(settings, readonly=True) as connection:
            publication = publication_row(connection, query.publication_id)
            if not publication:
                return SeriesResponse(
                    status="empty", metadata=None, filters=None, points=[]
                )
            filters = filters_for(query, publication)
            if query.station_id:
                rows = connection.execute(
                    """
                    SELECT hour_utc, departures_s completed_published_rides,
                           departures_s,arrivals_s,
                           arrivals_s-departures_s net_trip_flow_s
                    FROM station_hours WHERE publication_id=%s AND station_id=%s
                      AND hour_utc>=%s AND hour_utc<%s ORDER BY hour_utc
                """,
                    (
                        publication.publication_id,
                        query.station_id,
                        filters.start,
                        filters.end,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT hour_utc,completed_published_rides FROM ride_hours
                    WHERE publication_id=%s AND hour_utc>=%s AND hour_utc<%s
                    ORDER BY hour_utc
                """,
                    (publication.publication_id, filters.start, filters.end),
                ).fetchall()
            return SeriesResponse(
                status="ok" if rows else "empty",
                metadata=publication,
                filters=filters,
                points=[HourPoint.model_validate(row) for row in rows],
            )
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None


def get_kpi(
    query: QueryFilters, settings: DatabaseSettings | None = None
) -> KpiResponse:
    # One published month of hourly facts; no trip scanning in HTTP requests.
    series = get_series(query, settings)
    return KpiResponse(
        status=series.status,
        metadata=series.metadata,
        filters=series.filters,
        completed_published_rides=sum(
            p.completed_published_rides for p in series.points
        )
        if series.points
        else None,
    )
