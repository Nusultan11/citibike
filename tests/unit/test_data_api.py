from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable


def test_database_outage_does_not_break_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable() -> None:
        raise DatabaseUnavailable("database details must not leak")

    monkeypatch.setattr("apps.api.main.get_publication", unavailable)
    with TestClient(create_app()) as client:
        assert client.get("/health").json() == {"status": "ok"}
        response = client.get("/v1/publication")
        assert response.status_code == 503
        assert response.json() == {
            "error": {
                "code": "database_unavailable",
                "message": "Serving database unavailable",
            }
        }


@pytest.mark.parametrize(
    "params",
    [
        {"scope": "JC"},
        {"start_date": "2026-04-01"},
        {"start_date": "2026-04-02", "end_date": "2026-04-01"},
        {"station_id": "   "},
        {"publication_id": "invalid"},
        {"start_date": "not-a-date"},
    ],
)
def test_invalid_filters_rejected_before_database(params: dict[str, str]) -> None:
    with TestClient(create_app()) as client:
        response = client.get("/v1/kpi", params=params)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


def test_database_env_uses_only_pg_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # No local .env; exercise CI's default path.
    for variable in ["PGUSER", "PGHOST", "PGPORT", "PGDATABASE", "PGPASSWORD"]:
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("USER", "runner")
    monkeypatch.setenv("PORT", "3000")
    settings = DatabaseSettings()
    assert settings.user == "bikeflow"
    assert settings.port == 5432
    assert settings.password.get_secret_value() == ""
    monkeypatch.setenv("PGUSER", "explicit_pg_user")
    monkeypatch.setenv("PGPORT", "5544")
    assert DatabaseSettings().user == "explicit_pg_user"
    assert DatabaseSettings().port == 5544
