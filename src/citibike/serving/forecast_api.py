"""Read-only forecast API; no model loading/training or artifact filesystem IO."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.serving.read import InvalidFilter

router = APIRouter(
    prefix="/v3/forecasts", tags=["Forecasts (published observed trips)"]
)


class ForecastFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    station_id: str | None = Field(default=None, min_length=1, max_length=80)
    prediction_type: Literal["departures", "arrivals"] | None = None
    horizon_h: int | None = Field(default=None, ge=1, le=24)
    limit: int = Field(default=100, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=10000)


class ForecastPoint(BaseModel):
    station_id: str
    forecast_origin_utc: datetime
    target_hour_utc: datetime
    horizon_h: int
    prediction_type: Literal["departures", "arrivals"]
    predicted_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    status: Literal["ok", "unavailable"]
    reason: str | None = None
    model_id: str
    feature_available_at: datetime


class ForecastResponse(BaseModel):
    status: Literal["ok", "empty", "stale", "unavailable"] = "empty"
    publication: dict[str, Any] | None = None
    total_rows: int = 0
    rows: list[ForecastPoint] = Field(default_factory=list)


def read(
    filters: ForecastFilters, settings: DatabaseSettings | None = None
) -> ForecastResponse:
    try:
        with connect(settings, readonly=True) as db:
            db.execute("SET LOCAL TimeZone='UTC'")
            relation = db.execute(
                "SELECT to_regclass('ml_forecast_publications') object"
            ).fetchone()
            assert relation is not None
            if relation["object"] is None:
                return ForecastResponse()
            row = db.execute(
                """SELECT manifest,published_at FROM ml_forecast_publications
                WHERE publication_id=coalesce(%s,(SELECT publication_id
                FROM ml_forecast_active WHERE singleton))""",
                (filters.publication_id,),
            ).fetchone()
            if row is None:
                if filters.publication_id:
                    raise InvalidFilter(
                        "Forecast publication not found; F1/F3 IDs separate"
                    )
                return ForecastResponse()
            manifest = row["manifest"]
            clause = ["publication_id=%s"]
            params: list[Any] = [manifest["publication_id"]]
            for name in ("station_id", "prediction_type", "horizon_h"):
                value = getattr(filters, name)
                if value is not None:
                    clause.append(name + "=%s")
                    params.append(value)
            where = " AND ".join(clause)
            count = db.execute(
                "SELECT count(*) n FROM ml_forecasts WHERE " + where, params
            ).fetchone()
            values = db.execute(
                """SELECT station_id,forecast_origin_utc,target_hour_utc,
                horizon_h,prediction_type,predicted_value,status,reason,model_id,
                feature_available_at FROM ml_forecasts WHERE """
                + where
                + " ORDER BY station_id,prediction_type,horizon_h LIMIT %s OFFSET %s",
                [*params, filters.limit, filters.offset],
            ).fetchall()
            public = {
                k: manifest[k]
                for k in (
                    "publication_id",
                    "model_bundle_id",
                    "dataset_id",
                    "feature_schema",
                    "station_version",
                    "source_snapshot_id",
                    "gold_publication_id",
                    "forecast_origin_utc",
                    "issued_at",
                    "data_as_of",
                    "data_as_of_kind",
                    "valid_until",
                    "quality_status",
                    "availability_mode",
                    "accuracy_evidence_mode",
                    "freshness",
                    "limitations",
                    "valid_rows",
                    "unavailable_rows",
                )
            }
            public["published_at"] = row["published_at"].isoformat()
            stale = datetime.fromisoformat(manifest["valid_until"]) < datetime.now(UTC)
            return ForecastResponse(
                status=(
                    "empty"
                    if not values
                    else "stale"
                    if stale
                    else "unavailable"
                    if all(v["status"] == "unavailable" for v in values)
                    else "ok"
                ),
                publication=public,
                total_rows=count["n"] if count else 0,
                rows=[ForecastPoint.model_validate(v) for v in values],
            )
    except psycopg.Error:
        raise DatabaseUnavailable("Forecast database unavailable") from None


@router.get("", response_model=ForecastResponse)
def forecasts(filters: Annotated[ForecastFilters, Query()]) -> ForecastResponse:
    return read(filters)
