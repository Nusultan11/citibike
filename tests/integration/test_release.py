"""Release perimeter + actual PostgreSQL dump/restore in disposable databases."""

import shutil
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from apps.api.main import create_app
from citibike.ingestion.archive import checksum
from citibike.release import database as release_db
from citibike.release import staging
from citibike.release.recovery import backup, restore, snapshot, verify
from citibike.serving.db import DatabaseSettings, connect
from citibike.serving.models import QueryFilters
from citibike.serving.publish import publish
from citibike.serving.read import get_kpi
from citibike.storage import read_json, write_json
from tests.integration.test_serving import fixture_result

pytest_plugins = ("tests.integration.test_serving",)


def test_http_security_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIKEFLOW_API_RATE_PER_MINUTE", "3")
    monkeypatch.setenv("BIKEFLOW_API_CORS_ORIGINS", '["http://127.0.0.1:13000"]')
    with TestClient(create_app()) as client:
        assert client.get("/health").headers["x-frame-options"] == "DENY"
        assert client.get("/health").headers["x-content-type-options"] == "nosniff"
        assert client.post("/v1/kpi", content=b"x" * 100_000).status_code == 413
        assert client.get("/v1/kpi?" + "x" * 8193).status_code == 431
        assert client.get("/health", headers={"x-test": "x" * 16385}).status_code == 431
        assert (
            client.get(
                "/health", headers={"origin": "https://evil.invalid"}
            ).headers.get("access-control-allow-origin")
            is None
        )
        assert (
            client.get("/health", headers={"origin": "http://127.0.0.1:13000"}).headers[
                "access-control-allow-origin"
            ]
            == "http://127.0.0.1:13000"
        )
        assert client.get("/v1/kpi?start_date=invalid").status_code == 422
        assert client.get("/not-found").status_code == 404
        assert client.post("/v1/kpi").status_code == 405
        response = client.get("/v1/kpi", headers={"X-Forwarded-For": "8.8.8.8"})
        assert response.status_code == 429 and response.headers["retry-after"] == "60"


def test_ready_and_sql_injection(database: DatabaseSettings, tmp_path: Path) -> None:
    release_db.migrate(database)
    publish(fixture_result(tmp_path), database)
    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 200
        response = client.get(
            "/v1/kpi", params={"station_id": "'; DROP TABLE publications;--"}
        )
        assert response.status_code == 200 and response.json()["status"] == "empty"
        assert client.get("/v1/kpi").json()["completed_published_rides"] == 4
    with connect(database) as db:
        db.execute(
            "UPDATE schema_migrations SET checksum='invalid' WHERE version='006'"
        )
    assert not release_db.readiness(database)
    with TestClient(create_app()) as client:
        assert client.get("/ready").status_code == 503


def test_migration_failure_is_atomic(
    database: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(database)
    shutil.copytree(release_db.MIGRATIONS, tmp_path / "migrations")
    (tmp_path / "migrations/007_failure.sql").write_text(
        "CREATE TABLE must_rollback(n int); INVALID SQL;", encoding="utf-8"
    )
    monkeypatch.setattr(release_db, "MIGRATIONS", tmp_path / "migrations")
    with pytest.raises(psycopg.Error):
        release_db.migrate(database)
    assert snapshot(database) == before


def test_real_dump_restore_and_corruption(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    release_db.migrate(database)
    publication = publish(fixture_result(tmp_path / "fixture"), database)
    store = tmp_path / "store"
    store.mkdir()
    (store / "raw.zip").write_bytes(b"immutable-controlled-source")
    with sqlite3.connect(store / "metadata.sqlite") as db:
        db.execute("CREATE TABLE runs(id text)")
        db.execute("INSERT INTO runs VALUES ('controlled-registry-metadata')")
    saved = backup(["docker", "compose"], database, store, tmp_path / "backup")
    restored, result = restore(
        ["docker", "compose"], database, saved, tmp_path / "restored"
    )
    try:
        assert result["comparison"] == "PASS"
        assert snapshot(database) == snapshot(restored)
        assert get_kpi(QueryFilters(), restored) == get_kpi(QueryFilters(), database)
        assert (
            get_kpi(
                QueryFilters(publication_id=publication), restored
            ).completed_published_rides
            == 4
        )
        with sqlite3.connect(tmp_path / "restored/metadata.sqlite") as db:
            assert db.execute("SELECT id FROM runs").fetchone() == (
                "controlled-registry-metadata",
            )
        (saved / "postgres.dump").write_bytes(b"broken")
        with pytest.raises(ValueError, match="checksum"):
            restore(["docker", "compose"], database, saved, tmp_path / "rejected")
        assert not (tmp_path / "rejected").exists()
        manifest = read_json(saved / "manifest.json")
        manifest["files"]["../outside"] = checksum(store / "raw.zip")
        write_json(saved / "manifest.json", manifest)
        with pytest.raises(ValueError):
            verify(saved)
        assert get_kpi(QueryFilters(), database).completed_published_rides == 4
    finally:
        with connect(database) as db:
            db.autocommit = True
            db.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(restored.database))
            )


def test_unapproved_release_cannot_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Unapproved deployment tried to mutate staging")

    monkeypatch.setattr(staging, "run", forbidden)
    approval = tmp_path / "approval.json"
    write_json(approval, {"release": "other", "approved": True})
    with pytest.raises(ValueError, match="approval"):
        staging.deploy("candidate", approval)


def test_seed_copies_real_tables_once(
    database: DatabaseSettings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release_db.migrate(database)
    publish(fixture_result(tmp_path / "fixture"), database)
    name = "bikeflow_seedtest_" + tmp_path.name.replace("-", "_")[-20:].lower()
    with connect(database) as db:
        db.autocommit = True
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    target = database.model_copy(update={"database": name})
    try:
        release_db.migrate(target)
        monkeypatch.setattr(staging, "owner", lambda: target)
        monkeypatch.setattr(staging, "ROOT", tmp_path / "staging")
        model = tmp_path / "model"
        model.mkdir()
        (model / "bundle.joblib").write_bytes(b"immutable-model")
        with pytest.raises(ValueError, match="must not be the staging"):
            staging.seed(target, {})
        first = staging.seed(database, {"f4/models/m": model})
        assert first["status"] == "seeded" and first["tables"]["ride_hours"] > 0
        assert snapshot(target, list(staging.SEED_TABLES)) == snapshot(
            database, list(staging.SEED_TABLES)
        )
        assert first["artifacts"] == {"f4/models/m": 1}
        assert staging.seed(database, {"f4/models/m": model})["status"] == "unchanged"
        with connect(target) as db:
            db.execute(
                "DELETE FROM ride_hours "
                "WHERE ctid IN (SELECT ctid FROM ride_hours LIMIT 1)"
            )
        with pytest.raises(ValueError, match="different data"):
            staging.seed(database, {})
    finally:
        with connect(database) as db:
            db.autocommit = True
            db.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))


def test_redeploy_keeps_rollback_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(staging, "ROOT", tmp_path)
    monkeypatch.setattr(staging, "ENV", tmp_path / "runtime.env")
    staging.ENV.write_text("RELEASE_ID=a\n", encoding="utf-8")
    monkeypatch.setattr(
        staging, "run", lambda *a, **k: calls.append((*a, k["release"]))
    )
    monkeypatch.setattr(staging, "migrate", lambda settings: None)
    monkeypatch.setattr(staging, "reader", lambda settings, password: None)
    monkeypatch.setattr(staging, "owner", lambda: None)
    monkeypatch.setattr(
        staging,
        "environment",
        lambda: {"STAGING_READER_PASSWORD": "x", "RELEASE_ID": "a"},
    )
    monkeypatch.setattr(
        staging, "smoke", lambda release: {"status": "PASS", "release": release}
    )
    for release in ("a", "b", "b"):
        approval = tmp_path / f"{release}.json"
        write_json(
            approval,
            {
                "release": release,
                "environment": "local_staging",
                "approved_by": "owner",
                "approved": True,
            },
        )
        staging.deploy(release, approval, build=False)
    assert read_json(tmp_path / "active.json") == {
        "release": "b",
        "previous": {"release": "a", "previous": None},
    }
    assert all("--no-build" in call and "never" in call for call in calls)
    assert staging.rollback()["rolled_back_from"] == "b"
    assert read_json(tmp_path / "active.json") == {"release": "a", "previous": None}
    with pytest.raises(ValueError, match="No previous"):
        staging.rollback()


def test_staging_publishes_only_loopback_ports() -> None:
    lines = Path("compose.staging.yaml").read_text(encoding="utf-8").splitlines()
    ports = [line for line in lines if "ports:" in line]
    assert len(ports) == 3
    assert all(
        all(
            item.strip(' "[]').startswith("127.0.0.1:")
            for item in p.split(":", 1)[1].split(",")
        )
        for p in ports
    )
    assert not any(name in "\n".join(lines) for name in ("prefect", "mlflow"))
