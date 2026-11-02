"""F6 Station Explorer: published coordinates + the same F3 station aggregates.

Station metrics reuse the F3 bounds/where/observed-hour rules, so a station row
equals `/v2/analytics/stations` for identical filters. GBFS status comes from the
published captured intervals mapped by same-collection short_name; never inferred.
"""

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from citibike.serving.analytics_models import AnalyticsFilters, AnalyticsMetadata
from citibike.serving.analytics_read import (
    bounds,
    full_months,
    metadata,
    observed_hours,
    where,
)
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect

router = APIRouter(prefix="/v4/stations", tags=["Station explorer"])
STATION = r"^[^\x00-\x1f]{1,80}$"
SPLIT = re.compile(r"^\d+\.\d$")


def suspected_split(station_id: str, universe: set[str]) -> str | None:
    """Return the dddd.d0 partner of a dddd.d ID (or reverse); never merged."""
    if SPLIT.match(station_id) and station_id + "0" in universe:
        return station_id + "0"
    if station_id.endswith("0") and SPLIT.match(station_id[:-1]):
        partner = station_id[:-1]
        return partner if partner in universe else None
    return None


class StationFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start_date: date | None = None
    end_date: date | None = None
    vehicle: Literal["classic_bike", "electric_bike", "unknown"] | None = None
    membership: Literal["member", "casual", "unknown"] | None = None
    q: str | None = Field(default=None, min_length=1, max_length=80)
    limit: int = Field(default=3000, ge=1, le=3000)
    offset: int = Field(default=0, ge=0, le=10000)

    @model_validator(mode="after")
    def dates(self) -> "StationFilters":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both start_date and end_date")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("Date interval must be nonempty and half-open")
        return self


class GeoMetadata(BaseModel):
    geo_id: str
    version: str
    policy: str
    created_at: str
    source_snapshot_id: str
    stations: int


class StationRow(BaseModel):
    station_id: str
    station_name: str | None = None
    lat: float | None = None
    lon: float | None = None
    coordinate_month: str | None = None
    coordinate_status: Literal["ok", "missing", "unavailable"]
    coordinate_changes: int = 0
    suspected_id_split: bool = False
    split_partner: str | None = None
    completed_published_rides: int | None = None
    departures: int | None = None
    arrivals: int | None = None
    net_flow: int | None = None


class GbfsStatus(BaseModel):
    status: Literal["fresh", "stale", "unavailable"]
    reason: str | None = None
    gbfs_station_id: str | None = None
    rentable_bikes: int | None = None
    returnable_docks: int | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_updated: datetime | None = None
    collection_id: str | None = None
    status_freshness_sec: float | None = None


class CoordinateVersion(BaseModel):
    local_month: str
    event_type: Literal["start", "end"]
    lat: float | None
    lon: float | None
    station_name: str | None
    events: int
    coordinate_rows: int
    modal_rows: int
    distinct_coordinates: int


class StationsResponse(BaseModel):
    status: Literal["ok", "empty", "unavailable"]
    message: str | None = None
    publication: AnalyticsMetadata | None = None
    geo: GeoMetadata | None = None
    filters: StationFilters
    total_stations: int = 0
    rows: list[StationRow] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    gbfs: GbfsStatus | None = None
    history: list[CoordinateVersion] = Field(default_factory=list)


def geo_for(
    db: psycopg.Connection[dict[str, Any]], publication_id: str
) -> GeoMetadata | None:
    relation = db.execute(
        "SELECT to_regclass('station_geo_publications') object"
    ).fetchone()
    if relation is None or relation["object"] is None:
        return None
    row = db.execute(
        """SELECT geo_id, manifest FROM station_geo_publications
        WHERE analytics_publication_id=%s ORDER BY published_at DESC LIMIT 1""",
        (publication_id,),
    ).fetchone()
    if row is None:
        return None
    m = row["manifest"]
    return GeoMetadata(
        geo_id=row["geo_id"],
        version=m["version"],
        policy=m["policy"],
        created_at=m["created_at"],
        source_snapshot_id=m["source_snapshot_id"],
        stations=m["stations"],
    )


def gbfs_status(
    db: psycopg.Connection[dict[str, Any]], publication_id: str, station_id: str
) -> GbfsStatus:
    row = db.execute(
        """SELECT station_id, rentable_bikes, returnable_docks, valid_from, valid_to,
        source_updated, collection_id FROM analytics_availability
        WHERE publication_id=%s AND trip_station_id=%s
        ORDER BY valid_to DESC LIMIT 1""",
        (publication_id, station_id),
    ).fetchone()
    if row is None:
        return GbfsStatus(
            status="unavailable",
            reason=(
                "No valid captured GBFS interval mapped to this trip station_id;"
                " not shown as zero bikes or docks"
            ),
        )
    age = (datetime.now(UTC) - row["source_updated"]).total_seconds()
    fresh = datetime.now(UTC) < row["valid_to"]
    return GbfsStatus(
        status="fresh" if fresh else "stale",
        reason=None
        if fresh
        else "Captured interval expired (TTL/next receipt); historical observation",
        gbfs_station_id=row["station_id"],
        rentable_bikes=row["rentable_bikes"],
        returnable_docks=row["returnable_docks"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        source_updated=row["source_updated"],
        collection_id=row["collection_id"],
        status_freshness_sec=age,
    )


def read(
    filters: StationFilters,
    station_id: str | None = None,
    settings: DatabaseSettings | None = None,
) -> StationsResponse:
    try:
        with connect(settings, readonly=True) as db:
            db.execute("SET LOCAL TimeZone='UTC'")
            # Small aggregate queries: JIT compilation costs more than execution.
            db.execute("SET LOCAL jit=off")
            # Bounded per-request sort memory keeps the ~1e5-row geo sort in RAM.
            db.execute("SET LOCAL work_mem='32MB'")
            found = metadata(db, filters.publication_id)
            if found is None:
                return StationsResponse(
                    status="empty", filters=filters, message="No F3 publication"
                )
            manifest, publication = found
            start, end, _, _ = bounds(
                AnalyticsFilters(
                    start_date=filters.start_date, end_date=filters.end_date
                ),
                manifest,
            )
            filters = filters.model_copy(
                update={
                    "publication_id": publication.publication_id,
                    "start_date": start,
                    "end_date": end,
                }
            )
            analytic = AnalyticsFilters(
                publication_id=publication.publication_id,
                start_date=start,
                end_date=end,
                vehicle=filters.vehicle,
                membership=filters.membership,
                station_id=station_id,
            )
            response = StationsResponse(
                status="ok", publication=publication, filters=filters
            )
            response.coverage = observed_hours(db, analytic, manifest)
            starts = response.coverage["observed_city_start_hours"] > 0
            ends = response.coverage["observed_city_end_hours"] > 0
            name = "station_category_" + (
                "monthly" if full_months(analytic, manifest) else "daily"
            )
            clause, params = where(analytic, manifest, name)
            metrics = {
                r["station_id"]: r
                for r in db.execute(
                    "SELECT station_id, sum(n) n, sum(departures) departures,"
                    " sum(arrivals) arrivals FROM analytics_" + name + " WHERE "
                    f"{clause} GROUP BY 1",
                    params,
                ).fetchall()
            }
            geo = geo_for(db, publication.publication_id)
            response.geo = geo
            coordinates: dict[str, dict[str, Any]] = {}
            names: dict[str, str] = {}
            history: list[dict[str, Any]] = []
            if geo is not None:
                geo_params: list[Any] = [geo.geo_id, start.replace(day=1), end]
                station_clause = ""
                if station_id:
                    station_clause = " AND station_id=%s"
                    geo_params.append(station_id)
                scoped = (
                    "FROM station_geo_monthly WHERE geo_id=%s AND local_month>=%s"
                    " AND local_month<%s" + station_clause
                )
                # Latest month wins; within it the start side is preferred.
                for r in db.execute(
                    f"""WITH g AS MATERIALIZED (SELECT station_id, local_month,
                    event_type, lat, lon, station_name {scoped}),
                    latest AS (SELECT DISTINCT ON (station_id) station_id, lat, lon,
                      local_month FROM g WHERE lat IS NOT NULL
                      ORDER BY station_id, local_month DESC, event_type DESC),
                    named AS (SELECT DISTINCT ON (station_id) station_id,
                      station_name FROM g WHERE station_name IS NOT NULL
                      ORDER BY station_id, local_month DESC, event_type DESC),
                    pairs AS (SELECT station_id, count(*) n FROM (SELECT DISTINCT
                      station_id, lat, lon FROM g WHERE lat IS NOT NULL) d
                      GROUP BY 1)
                    SELECT i.station_id, l.lat, l.lon, l.local_month, p.n pairs,
                      m.station_name FROM (SELECT DISTINCT station_id FROM g) i
                    LEFT JOIN latest l USING (station_id)
                    LEFT JOIN named m USING (station_id)
                    LEFT JOIN pairs p USING (station_id)""",
                    geo_params,
                ).fetchall():
                    coordinates[r["station_id"]] = {
                        "latest": r if r["lat"] is not None else None,
                        "changes": max(int(r["pairs"] or 0) - 1, 0),
                    }
                    if r["station_name"]:
                        names[r["station_id"]] = r["station_name"]
                if station_id:
                    history = db.execute(
                        "SELECT station_id, local_month, event_type, lat, lon,"
                        " station_name, events, coordinate_rows, modal_rows,"
                        f" distinct_coordinates {scoped}"
                        " ORDER BY local_month, event_type",
                        geo_params,
                    ).fetchall()
            universe = set(metrics) | set(coordinates)
            needle = (filters.q or "").casefold()
            selected = sorted(
                s
                for s in universe
                if not needle
                or s.casefold().startswith(needle)
                or needle in names.get(s, "").casefold()
            )
            if station_id:
                selected = [s for s in selected if s == station_id]
            response.total_stations = len(selected)
            stations: list[StationRow] = []
            for s in selected[filters.offset : filters.offset + filters.limit]:
                m = metrics.get(s)
                place = coordinates.get(s, {}).get("latest")
                partner = suspected_split(s, universe)
                stations.append(
                    StationRow(
                        station_id=s,
                        station_name=names.get(s),
                        lat=place["lat"] if place else None,
                        lon=place["lon"] if place else None,
                        coordinate_month=str(place["local_month"])[:7]
                        if place
                        else None,
                        coordinate_status="unavailable"
                        if geo is None
                        else "ok"
                        if place
                        else "missing",
                        coordinate_changes=coordinates.get(s, {}).get("changes", 0),
                        suspected_id_split=partner is not None,
                        split_partner=partner,
                        completed_published_rides=int(m["n"]) if m and starts else None,
                        departures=int(m["departures"]) if m and starts else None,
                        arrivals=int(m["arrivals"]) if m and ends else None,
                        net_flow=int(m["arrivals"]) - int(m["departures"])
                        if m and starts and ends
                        else None,
                    )
                )
            response.rows = stations
            response.coverage.update(
                {
                    "stations_with_coordinates": sum(
                        1 for s in selected if coordinates.get(s, {}).get("latest")
                    ),
                    "stations_without_coordinates": sum(
                        1 for s in selected if not coordinates.get(s, {}).get("latest")
                    ),
                    "suspected_id_split_stations": sum(
                        1 for s in selected if suspected_split(s, universe)
                    ),
                    "metric_source": f"analytics_{name}; same F3 filters as stations",
                    "coordinate_policy": (
                        "latest NYC event month inside the period; modal reported"
                        " trip endpoint pair; IDs never merged"
                    ),
                    "id_split_policy": (
                        "dddd.d with an existing dddd.d0 partner is flagged only;"
                        " raw-source lineage, not operator-confirmed identity"
                    ),
                }
            )
            if geo is None:
                response.message = (
                    "Station coordinates are not published for this F3 version"
                )
            if station_id:
                response.history = [
                    CoordinateVersion(
                        local_month=str(r["local_month"])[:7],
                        **{
                            k: r[k]
                            for k in (
                                "event_type",
                                "lat",
                                "lon",
                                "station_name",
                                "events",
                                "coordinate_rows",
                                "modal_rows",
                                "distinct_coordinates",
                            )
                        },
                    )
                    for r in history
                ]
                response.gbfs = gbfs_status(db, publication.publication_id, station_id)
            if not stations:
                response.status = "empty"
                response.message = response.message or (
                    "No station observations for these filters; not zero demand"
                )
            return response
    except psycopg.Error:
        raise DatabaseUnavailable("Serving database unavailable") from None


@router.get("", response_model=StationsResponse)
def stations(filters: Annotated[StationFilters, Query()]) -> StationsResponse:
    return read(filters)


@router.get("/{station_id}", response_model=StationsResponse)
def station(
    station_id: Annotated[str, Path(pattern=STATION)],
    filters: Annotated[StationFilters, Query()],
) -> StationsResponse:
    return read(filters.model_copy(update={"q": None}), station_id)
