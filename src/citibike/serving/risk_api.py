"""Read-only F5 scenario results; never simulates, solves or reads artifacts.

A result counts as current only while its as-of GBFS state is inside the TTL and
the horizon has not ended. Otherwise it is an archival replay, never an
operational recommendation.
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.serving.read import InvalidFilter

router = APIRouter(prefix="/v4/risk", tags=["Scenario risks (simulation)"])


class RiskFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    scale: float | None = Field(default=None, ge=0, le=4)
    event_order: Literal["uniform", "departures_first", "arrivals_first"] | None = None
    station_id: str | None = Field(default=None, min_length=1, max_length=80)


class Currency(BaseModel):
    state: Literal["current", "archival"]
    checked_at: datetime
    as_of_age_seconds: float
    ttl_seconds: float
    reason: str


class RiskResponse(BaseModel):
    status: Literal["current_scenario", "archival_replay", "empty", "unavailable"]
    message: str | None = None
    publication: dict[str, Any] | None = None
    currency: Currency | None = None
    filters: RiskFilters
    outcomes: list[dict[str, Any]] = Field(default_factory=list)
    stations: list[dict[str, Any]] = Field(default_factory=list)
    station_outcomes: list[dict[str, Any]] = Field(default_factory=list)
    transfers: list[dict[str, Any]] = Field(default_factory=list)


def currency(manifest: dict[str, Any], now: datetime) -> Currency:
    as_of = datetime.fromisoformat(manifest["data_as_of"])
    end = datetime.fromisoformat(manifest["horizon_end"])
    age = (now - as_of).total_seconds()
    ttl = float(manifest["ttl_seconds"])
    current = 0 <= age <= ttl and now < end
    return Currency(
        state="current" if current else "archival",
        checked_at=now,
        as_of_age_seconds=age,
        ttl_seconds=ttl,
        reason=(
            "Initial GBFS state is inside TTL and the horizon is open"
            if current
            else "As-of GBFS state is older than TTL or the horizon ended:"
            " archival scenario replay, not a current recommendation"
        ),
    )


def read(
    filters: RiskFilters,
    settings: DatabaseSettings | None = None,
    now: datetime | None = None,
) -> RiskResponse:
    try:
        with connect(settings, readonly=True) as db:
            db.execute("SET LOCAL TimeZone='UTC'")
            relation = db.execute(
                "SELECT to_regclass('risk_publications') object"
            ).fetchone()
            if relation is None or relation["object"] is None:
                return RiskResponse(
                    status="empty", filters=filters, message="No F5 publication"
                )
            row = db.execute(
                """SELECT manifest, published_at FROM risk_publications
                WHERE publication_id=coalesce(%s,(SELECT publication_id
                FROM risk_active WHERE singleton))""",
                (filters.publication_id,),
            ).fetchone()
            if row is None:
                if filters.publication_id:
                    raise InvalidFilter("Risk publication not found")
                return RiskResponse(
                    status="empty", filters=filters, message="No F5 publication"
                )
            manifest = row["manifest"]
            identifier = manifest["publication_id"]
            scales = sorted(manifest["config"]["scales"])
            scale = (
                filters.scale
                if filters.scale is not None
                else (1.0 if 1.0 in scales else scales[0])
            )
            order = filters.event_order or manifest["config"]["orders"][0]
            if scale not in scales or order not in manifest["config"]["orders"]:
                raise InvalidFilter("Scale/order not present in this F5 publication")
            filters = filters.model_copy(
                update={
                    "publication_id": identifier,
                    "scale": scale,
                    "event_order": order,
                }
            )
            state = currency(manifest, now or datetime.now(UTC))
            public = {**manifest, "published_at": row["published_at"].isoformat()}
            response = RiskResponse(
                status="current_scenario"
                if state.state == "current"
                else "archival_replay",
                publication=public,
                currency=state,
                filters=filters,
            )
            response.outcomes = db.execute(
                """SELECT scale, event_order, policy, repeats, totals, movement_cost,
                moved_bikes, vehicles, constraint_violations, gain_vs_no_action,
                solver FROM risk_policy_outcomes WHERE publication_id=%s
                ORDER BY scale, event_order, array_position(
                ARRAY['no_action','heuristic','optimizer'], policy)""",
                (identifier,),
            ).fetchall()
            station_clause, station_params = "", []
            if filters.station_id:
                station_clause = " AND station_id=%s"
                station_params = [filters.station_id]
            response.stations = db.execute(
                """SELECT station_id, eligible, reasons, gbfs_id, capacity, bikes,
                docks, lat, lon FROM risk_stations WHERE publication_id=%s"""
                + station_clause
                + " ORDER BY eligible DESC, station_id",
                [identifier, *station_params],
            ).fetchall()
            response.station_outcomes = db.execute(
                """SELECT policy, station_id, status, empty_fraction, empty_low,
                empty_high, full_fraction, full_low, full_high, repeats
                FROM risk_station_outcomes WHERE publication_id=%s AND scale=%s
                AND event_order=%s"""
                + station_clause
                + """ ORDER BY greatest(coalesce(empty_fraction,0),
                coalesce(full_fraction,0)) DESC, station_id, policy""",
                [identifier, scale, order, *station_params],
            ).fetchall()
            response.transfers = db.execute(
                """SELECT scale, policy, sequence, source, destination, bikes,
                vehicles, minutes, distance_km FROM risk_transfers
                WHERE publication_id=%s AND scale=%s ORDER BY policy, sequence""",
                (identifier, scale),
            ).fetchall()
            if filters.station_id and not response.stations:
                response.status = "empty"
                response.message = "Station is outside this F5 scenario cohort"
            return response
    except psycopg.Error:
        raise DatabaseUnavailable("Risk database unavailable") from None


@router.get("", response_model=RiskResponse)
def risk(filters: Annotated[RiskFilters, Query()]) -> RiskResponse:
    return read(filters)
