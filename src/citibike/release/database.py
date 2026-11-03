"""Checksummed migrations and read-only application role, separate from owner."""

import hashlib
from pathlib import Path

import psycopg
from psycopg import sql

from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect

MIGRATIONS = Path(__file__).parents[1] / "serving/migrations"


def migrate(settings: DatabaseSettings | None = None) -> None:
    with connect(settings) as db:
        # One transaction and one lock: no partially applied release schema.
        db.execute("SELECT pg_advisory_xact_lock(862408)")
        db.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            version = path.name.split("_")[0]
            content = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(content.encode()).hexdigest()
            applied = db.execute(
                "SELECT checksum FROM schema_migrations WHERE version=%s", (version,)
            ).fetchone()
            if applied and applied["checksum"] != checksum:
                raise ValueError("Applied migration checksum differs")
            if applied:
                continue
            db.execute(content)
            db.execute(
                "INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)",
                (version, checksum),
            )


def readiness(settings: DatabaseSettings | None = None) -> bool:
    try:
        with connect(settings, readonly=True) as db:
            rows = db.execute(
                "SELECT version,checksum FROM schema_migrations"
            ).fetchall()
        applied = {row["version"]: row["checksum"] for row in rows}
        return all(
            applied.get(path.name.split("_")[0])
            == hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest()
            for path in MIGRATIONS.glob("*.sql")
        )
    except (DatabaseUnavailable, psycopg.Error):
        return False


def reader(settings: DatabaseSettings, password: str) -> None:
    with connect(settings) as db:
        if not db.execute(
            "SELECT 1 FROM pg_roles WHERE rolname='bikeflow_reader'"
        ).fetchone():
            db.execute("CREATE ROLE bikeflow_reader LOGIN")
        db.execute(
            sql.SQL(
                "ALTER ROLE bikeflow_reader PASSWORD {} NOSUPERUSER NOCREATEDB "
                "NOCREATEROLE NOREPLICATION"
            ).format(sql.Literal(password))
        )
        db.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        db.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(settings.database)
            )
        )
        db.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO bikeflow_reader").format(
                sql.Identifier(settings.database)
            )
        )
        db.execute("GRANT USAGE ON SCHEMA public TO bikeflow_reader")
        db.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO bikeflow_reader")
        db.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            "GRANT SELECT ON TABLES TO bikeflow_reader"
        )
