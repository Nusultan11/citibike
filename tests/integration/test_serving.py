from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from apps.api.main import create_app
from citibike.ingestion.archive import ingest_fixture
from citibike.processing.trips import process
from citibike.serving import publish as publishing
from citibike.serving.db import DatabaseSettings, connect
from citibike.serving.models import QueryFilters
from citibike.serving.read import get_kpi, get_publication, get_series
from tests.fixtures import fixture_archive, ride, write_zip


@pytest.fixture
def database(monkeypatch: pytest.MonkeyPatch) -> Iterator[DatabaseSettings]:
    # Only this uniquely created disposable database is ever dropped.
    admin = DatabaseSettings()
    name = f"bikeflow_test_{uuid4().hex}"
    with connect(admin) as connection:
        connection.autocommit = True
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    monkeypatch.setenv("PGDATABASE", name)
    settings = DatabaseSettings()
    try:
        with connect(settings) as connection:
            publishing.migrate(connection)
        yield settings
    finally:
        with connect(admin) as connection:
            connection.autocommit = True
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def fixture_result(tmp_path: Path) -> Path:
    source = ingest_fixture(
        fixture_archive(tmp_path / "first.zip"), "2026-04", tmp_path
    )
    return process(source, tmp_path)


def test_postgres_api_publication_and_filters(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    assert get_publication(database).status == "empty"
    with TestClient(create_app()) as client:
        empty = client.get("/v1/kpi").json()
        assert empty["status"] == "empty" and empty["completed_published_rides"] is None
    result = fixture_result(tmp_path)
    version = publishing.publish(result, database)
    publication = get_publication(database).publication
    assert publication is not None and publication.publication_id == version
    assert publication.canonical_rows == 5 and publication.excluded_rows == 5
    assert publishing.publish(result, database) == version
    assert get_publication(database).publication == publication
    assert get_kpi(QueryFilters(), database).completed_published_rides == 4
    assert (
        get_kpi(QueryFilters(station_id="001"), database).completed_published_rides == 3
    )
    arrivals = get_series(QueryFilters(station_id="002"), database)
    assert sum(p.arrivals_s or 0 for p in arrivals.points) == 4
    with connect(database, readonly=True) as connection:
        assert connection.execute("SELECT count(*) n FROM publications").fetchone() == {
            "n": 1
        }
        assert connection.execute("SHOW default_transaction_read_only").fetchone() == {
            "default_transaction_read_only": "on"
        }
    with TestClient(create_app()) as client:
        response = client.get(
            "/v1/kpi",
            params={
                "publication_id": version,
                "start_date": "2026-04-01",
                "end_date": "2026-04-02",
            },
        )
        assert response.status_code == 200
        assert response.json()["completed_published_rides"] == 3
        assert response.json()["filters"]["start"] == "2026-04-01T04:00:00Z"
        assert (
            client.get(
                "/v1/kpi", params={"start_date": "2026-03-01", "end_date": "2026-04-02"}
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/v1/kpi", params={"start_date": "2026-04-02", "end_date": "2026-04-01"}
            ).status_code
            == 422
        )
        assert client.get("/v1/kpi", params={"scope": "JC"}).status_code == 422
        assert (
            client.get("/v1/kpi", params={"station_id": "' OR 1=1 --"}).json()["status"]
            == "empty"
        )
        assert (
            client.get("/v1/kpi", params={"publication_id": "f" * 64}).status_code
            == 422
        )


def test_failed_publish_retains_previous_version(
    database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = publishing.publish(fixture_result(tmp_path), database)
    second = write_zip(tmp_path / "second.zip", {"one.csv": [ride(19)]})
    result = process(ingest_fixture(second, "2026-04", tmp_path), tmp_path)

    def fail_activate(*_args: object) -> None:
        raise RuntimeError("Injected failure before active-version switch")

    with monkeypatch.context() as patch:
        patch.setattr(publishing, "activate", fail_activate)
        with pytest.raises(RuntimeError, match="Injected failure"):
            publishing.publish(result, database)
    retained = get_publication(database).publication
    assert retained is not None and retained.publication_id == old
    assert get_kpi(QueryFilters(), database).completed_published_rides == 4
    with connect(database) as connection:
        assert connection.execute("SELECT count(*) n FROM publications").fetchone() == {
            "n": 1
        }
    new = publishing.publish(result, database)
    assert get_kpi(QueryFilters(), database).completed_published_rides == 1
    assert (
        get_kpi(QueryFilters(publication_id=old), database).completed_published_rides
        == 4
    )
    assert new != old


def test_changed_migration_blocks_without_rewriting(database: DatabaseSettings) -> None:
    with connect(database) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum='changed' WHERE version='001'"
        )
    with connect(database) as connection:
        with pytest.raises(ValueError, match="migration checksum"):
            publishing.migrate(connection)
