from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class QueryFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None
    scope: Literal["NYC"] = "NYC"
    station_id: str | None = Field(default=None, min_length=1, max_length=80)
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Both dates are required; end_date is exclusive")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start_date must precede end_date")
        if self.station_id is not None and not self.station_id.strip():
            raise ValueError("station_id cannot be blank")
        return self


class Publication(BaseModel):
    publication_id: str
    source: str
    source_url: str
    source_version: str
    pipeline_version: str
    scope: Literal["NYC"]
    timezone: Literal["America/New_York"]
    period_start: datetime
    period_end: datetime
    data_as_of: datetime
    retrieved_at: datetime
    published_at: datetime
    quality_status: str
    input_rows: int
    canonical_rows: int
    excluded_rows: int
    quality_counts: dict[str, int]
    reason_counts: dict[str, int]
    coverage: dict[str, int]


class PublicationResponse(BaseModel):
    status: Literal["ok", "empty"]
    publication: Publication | None


class AppliedFilters(BaseModel):
    start: datetime
    end: datetime
    timezone: Literal["America/New_York"] = "America/New_York"
    scope: Literal["NYC"] = "NYC"
    station_id: str | None = None


class KpiResponse(BaseModel):
    status: Literal["ok", "empty"]
    metadata: Publication | None
    filters: AppliedFilters | None
    completed_published_rides: int | None


class HourPoint(BaseModel):
    hour_utc: datetime
    completed_published_rides: int
    departures_s: int | None = None
    arrivals_s: int | None = None
    net_trip_flow_s: int | None = None


class SeriesResponse(BaseModel):
    status: Literal["ok", "empty"]
    metadata: Publication | None
    filters: AppliedFilters | None
    points: list[HourPoint]
