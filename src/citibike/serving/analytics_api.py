"""F3 routes; F1 remains an independent publication and API contract."""

from typing import Annotated

from fastapi import APIRouter, Query

from citibike.serving.analytics_models import (
    AnalyticsFilters,
    AnalyticsResponse,
    AvailabilityFilters,
)
from citibike.serving.analytics_read import availability, read

router = APIRouter(prefix="/v2/analytics", tags=["Historical analytics"])


@router.get("/publication", response_model=AnalyticsResponse)
def publication(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("publication", filters)


@router.get("/kpi", response_model=AnalyticsResponse)
def kpi(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("kpi", filters)


@router.get("/timeseries", response_model=AnalyticsResponse)
def timeseries(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("timeseries", filters)


@router.get("/stations", response_model=AnalyticsResponse)
def stations(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("stations", filters)


@router.get("/routes", response_model=AnalyticsResponse)
def routes(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("routes", filters)


@router.get("/categories", response_model=AnalyticsResponse)
def categories(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("categories", filters)


@router.get("/geography", response_model=AnalyticsResponse)
def geography(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("geography", filters)


@router.get("/peaks", response_model=AnalyticsResponse)
def peaks(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("peaks", filters)


@router.get("/compare", response_model=AnalyticsResponse)
def compare(filters: Annotated[AnalyticsFilters, Query()]) -> AnalyticsResponse:
    return read("compare", filters)


@router.get("/availability", response_model=AnalyticsResponse)
def gbfs(filters: Annotated[AvailabilityFilters, Query()]) -> AnalyticsResponse:
    return availability(filters)
