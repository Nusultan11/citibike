"""Bounded F3 filters and explicit typed analytical response contract."""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AnalyticsFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start_date: date | None = None
    end_date: date | None = None
    station_id: str | None = Field(default=None, min_length=1, max_length=80)
    to_station: str | None = Field(default=None, min_length=1, max_length=80)
    vehicle: Literal["classic_bike", "electric_bike", "unknown"] | None = None
    membership: Literal["member", "casual", "unknown"] | None = None
    grain: Literal["hour", "day", "month"] = "day"
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=10000)
    event_type: Literal["start", "end"] = "start"
    compare_start: date | None = None
    compare_end: date | None = None

    @model_validator(mode="after")
    def dates(self) -> "AnalyticsFilters":
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Provide both start_date and end_date")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("Date interval must be nonempty and half-open")
        return self


class AnalyticsMetadata(BaseModel):
    publication_id: str
    source: str
    source_snapshot_id: str
    data_as_of: str
    published_at: datetime
    timezone: Literal["America/New_York"] = "America/New_York"
    scope: Literal["NYC"] = "NYC"
    pipeline_version: str
    geo_mapping_version: str
    quality_status: str
    data_mode: str
    period_start: str
    period_end: str
    population: str = (
        "F2 canonical accepted completed rides, started in [start,end);"
        " arrivals independently ended in [start,end)"
    )
    accepted_silver_rows: int
    coverage: dict[str, Any]


class AnalyticsMetrics(BaseModel):
    completed_published_rides: int | None = None
    average_duration_seconds: float | None = None
    duration_sum_us: int | None = None
    duration_count: int | None = None
    departures: int | None = None
    arrivals: int | None = None
    net_flow: int | None = None
    unknown_start_station: int | None = None
    unknown_end_station: int | None = None
    route_eligible_rides: int | None = None
    route_coverage: float | None = None
    geo_eligible_starts: int | None = None
    geo_coverage: float | None = None


class AnalyticsRow(BaseModel):
    key: str
    metrics: AnalyticsMetrics | None = None
    station_id: str | None = None
    from_station: str | None = None
    to_station: str | None = None
    vehicle: str | None = None
    membership: str | None = None
    dimension: str | None = None
    category: str | None = None
    n: int | None = None
    known_denominator: int | None = None
    known_share: float | None = None
    unknown_count: int | None = None
    cell_lat: int | None = None
    cell_lon: int | None = None
    district: str | None = None
    coverage: float | None = None


class ComparisonResult(BaseModel):
    comparable: bool | None = None
    previous_start: date | None = None
    previous_end: date | None = None
    previous_rides: int | None = None
    previous_coverage: dict[str, Any] = Field(default_factory=dict)
    relative_change: float | None = None
    reason: str | None = None


class AvailabilityMetrics(BaseModel):
    source: str | None = None
    scope: str | None = None
    station_id_namespace: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    observed_station_seconds: float | None = None
    target_station_seconds: float | None = None
    observed_coverage: float | None = Field(default=None, ge=0, le=1)
    rent_observed_station_seconds: float | None = None
    return_observed_station_seconds: float | None = None
    rent_target_station_seconds: float | None = None
    return_target_station_seconds: float | None = None
    rent_observed_coverage: float | None = Field(default=None, ge=0, le=1)
    return_observed_coverage: float | None = Field(default=None, ge=0, le=1)
    rentable_bikes_time_weighted: float | None = None
    returnable_docks_time_weighted: float | None = None
    empty_station_time_share: float | None = Field(default=None, ge=0, le=1)
    full_station_time_share: float | None = Field(default=None, ge=0, le=1)
    active_stations: int | None = None
    active_stations_at: datetime | None = None
    active_stations_policy: str | None = None
    time_weighted_population: str | None = None
    status_freshness_sec: float | None = None
    last_captured_status_updated: datetime | None = None
    last_valid_source_updated: datetime | None = None
    valid_interval_source_age_sec: float | None = None
    freshness_status: Literal["fresh", "stale"] | None = None
    distinct_updates: int | None = None
    availability_version: str | None = None
    policy: str | None = None
    sufficient_coverage: bool | None = None


class AnalyticsResponse(BaseModel):
    status: Literal["ok", "empty", "unavailable", "stale"]
    message: str | None = None
    publication: AnalyticsMetadata | None = None
    filters: AnalyticsFilters
    metrics: AnalyticsMetrics | None = None
    rows: list[AnalyticsRow] = Field(default_factory=list)
    total_groups: int | None = None
    coverage: dict[str, Any] = Field(default_factory=dict)
    comparison: ComparisonResult = Field(default_factory=ComparisonResult)
    availability: AvailabilityMetrics = Field(default_factory=AvailabilityMetrics)


class AvailabilityFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    publication_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    start: datetime | None = None
    end: datetime | None = None
    station_id: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def dates(self) -> "AvailabilityFilters":
        if (self.start is None) != (self.end is None):
            raise ValueError("Provide both UTC bounds")
        if self.start is not None and self.end is not None:
            if self.start.tzinfo is None or self.end.tzinfo is None:
                raise ValueError("Availability requires timezone-aware instants")
            if not 0 < (self.end - self.start).total_seconds() <= 31 * 86400:
                raise ValueError("Availability range must be positive and <=31 days")
        return self
