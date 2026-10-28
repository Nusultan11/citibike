"""API liveness, read-only published metrics and consistent JSON errors."""

from http import HTTPStatus
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException

from citibike.serving.db import DatabaseUnavailable
from citibike.serving.models import (
    KpiResponse,
    PublicationResponse,
    QueryFilters,
    SeriesResponse,
)
from citibike.serving.read import InvalidFilter, get_kpi, get_publication, get_series


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


def error_response(status: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(error=ErrorDetail(code=code, message=message))
    return JSONResponse(status_code=status, content=body.model_dump())


def create_app() -> FastAPI:
    application = FastAPI(title="BikeFlow Intelligence API", version="0.1.0")

    @application.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse()

    @application.get("/v1/publication", response_model=PublicationResponse)
    def publication() -> PublicationResponse:
        return get_publication()

    @application.get("/v1/kpi", response_model=KpiResponse)
    def kpi(filters: Annotated[QueryFilters, Query()]) -> KpiResponse:
        return get_kpi(filters)

    @application.get("/v1/timeseries", response_model=SeriesResponse)
    def timeseries(filters: Annotated[QueryFilters, Query()]) -> SeriesResponse:
        return get_series(filters)

    @application.exception_handler(DatabaseUnavailable)
    async def handle_database_error(
        _request: Request, _exc: DatabaseUnavailable
    ) -> JSONResponse:
        return error_response(
            503, "database_unavailable", "Serving database unavailable"
        )

    @application.exception_handler(InvalidFilter)
    async def handle_filter_error(
        _request: Request, exc: InvalidFilter
    ) -> JSONResponse:
        return error_response(422, "invalid_filter", str(exc))

    @application.exception_handler(HTTPException)
    async def handle_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(
            exc.status_code, "http_error"
        )
        response = error_response(
            exc.status_code, code, HTTPStatus(exc.status_code).phrase
        )
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(422, "validation_error", "Invalid request")

    @application.exception_handler(Exception)
    async def handle_unexpected_error(
        _request: Request, _exc: Exception
    ) -> JSONResponse:
        return error_response(500, "internal_error", "Internal server error")

    return application


app = create_app()
