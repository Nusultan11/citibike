from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from citibike.forecasting.artifacts import checked
from citibike.forecasting.inference import predict
from citibike.serving.db import DatabaseSettings, connect
from citibike.serving.forecast_api import ForecastFilters, read
from citibike.serving.forecast_publish import activate, publish
from citibike.storage import read_json

pytest_plugins = ("tests.integration.test_serving",)


def test_forecast_atomic_idempotent_rollback_and_http(
    database: DatabaseSettings,
    forecast_workflow: dict[str, Path],
) -> None:
    p = forecast_workflow
    assert read(ForecastFilters(), database).status == "empty"
    version = publish(p["forecast"], database)
    with pytest.raises(ValueError, match="schema mismatch"):
        publish(p["model"], database)
    assert publish(p["forecast"], database) == version
    result = read(ForecastFilters(limit=200), database)
    assert (
        result.status == "ok" and result.total_rows == 192 and len(result.rows) == 192
    )
    assert all(r.feature_available_at <= r.forecast_origin_utc for r in result.rows)
    with connect(database) as db:
        with pytest.raises(psycopg.errors.CheckViolation):
            with db.transaction():
                db.execute(
                    "UPDATE ml_forecasts SET predicted_value=NULL "
                    "WHERE publication_id=%s AND status='ok'",
                    (version,),
                )
    station = checked(p["forecast"])["station_ids"][0]
    newer = predict(p["root"], p["model"], stations=[station])
    new_id = checked(newer)["publication_id"]
    for flag in ("fail_after_copy", "fail_before_activate"):
        with pytest.raises(RuntimeError, match="Injected failure"):
            publish(newer, database, **{flag: True})
        current = read(ForecastFilters(), database).publication
        assert current is not None and current["publication_id"] == version
        with connect(database, readonly=True) as db:
            assert db.execute(
                "SELECT count(*) n FROM ml_forecast_publications "
                "WHERE publication_id=%s",
                (new_id,),
            ).fetchone() == {"n": 0}
    assert publish(newer, database) == new_id
    assert read(ForecastFilters(), database).total_rows == 48
    with connect(database) as db:
        activate(db, version)
    assert read(ForecastFilters(), database).total_rows == 192
    assert read(ForecastFilters(publication_id=new_id), database).total_rows == 48
    with TestClient(create_app()) as client:
        response = client.get(
            "/v3/forecasts",
            params={
                "station_id": station,
                "prediction_type": "arrivals",
                "horizon_h": 24,
            },
        )
        assert response.status_code == 200
        assert len(response.json()["rows"]) == 1
        assert response.json()["publication"]["availability_mode"] == "strict"
        for query in (
            {"horizon_h": 25},
            {"prediction_type": "demand"},
            {"limit": 201},
            {
                "publication_id": read_json(p["gold"] / "manifest.json")[
                    "publication_id"
                ]
            },
        ):
            assert client.get("/v3/forecasts", params=query).status_code == 422
        assert (
            client.get("/v3/forecasts", params={"station_id": "' OR 1=1--"}).json()[
                "total_rows"
            ]
            == 0
        )
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/v1/kpi").json()["status"] == "empty"
    unavailable = predict(p["root"], p["model"], stations=["not_known"])
    with pytest.raises(ValueError, match="All forecasts unavailable"):
        publish(unavailable, database)
    assert read(ForecastFilters(), database).total_rows == 192
    with connect(database) as db:
        db.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version='004'"
        )
    with pytest.raises(ValueError, match="migration checksum"):
        publish(p["forecast"], database)
