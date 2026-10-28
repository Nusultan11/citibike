"""Transactional COPY and a single atomic active-version switch."""

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from citibike.audit import audit_archive
from citibike.processing.trips import verify_artifacts
from citibike.serving.db import DatabaseSettings, connect


def migrate(connection: psycopg.Connection[dict[str, Any]]) -> None:
    path = Path(__file__).with_name("migrations") / "001_initial.sql"
    content = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(content.encode()).hexdigest()
    connection.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
        version TEXT PRIMARY KEY, checksum TEXT NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
    row = connection.execute(
        "SELECT checksum FROM schema_migrations WHERE version=%s", ("001",)
    ).fetchone()
    if row:
        if row["checksum"] != digest:
            raise ValueError("Applied migration checksum differs")
        return
    connection.execute(content)
    connection.execute(
        "INSERT INTO schema_migrations(version,checksum) VALUES (%s,%s)",
        ("001", digest),
    )


def activate(connection: psycopg.Connection[dict[str, Any]], version: str) -> None:
    connection.execute(
        """INSERT INTO active_publication(singleton,publication_id)
        VALUES (true,%s) ON CONFLICT(singleton) DO UPDATE
        SET publication_id=EXCLUDED.publication_id""",
        (version,),
    )


def publish(result: Path, settings: DatabaseSettings | None = None) -> str:
    report = verify_artifacts(result)
    if report["metrics"]["completed_published_rides"] <= 0:
        raise ValueError("No canonical rows inside source period; publication blocked")
    version: str = report["dataset_id"]
    source = report["source"]
    root = result.parent.parent
    receipt = root / "evidence" / f"{source['checksum']}-baseline.json"
    if receipt.exists():
        baseline = json.loads(receipt.read_text(encoding="utf-8"))
    else:
        baseline = audit_archive(
            root / "bronze" / "NYC" / source["checksum"], root / "work"
        )
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8") as output:
            json.dump(baseline, output, indent=2)
    if (
        baseline["source_checksum"] != source["checksum"]
        or baseline["input_rows"] != report["input_rows"]
        or baseline["dispositions"] != report["dispositions"]
        or baseline["completed_published_rides"]
        != report["metrics"]["completed_published_rides"]
    ):
        raise ValueError(
            "Independent raw CSV reconciliation failed; publication blocked"
        )
    metadata = {
        "publication_id": version,
        "source": source["source"],
        "source_url": source["source_url"],
        "source_version": source["checksum"],
        "pipeline_version": report["pipeline_version"],
        "scope": "NYC",
        "timezone": "America/New_York",
        "period_start": report["period_start"],
        "period_end": report["period_end"],
        "data_as_of": report["data_as_of"],
        "retrieved_at": source["retrieved_at"],
        "quality_status": report["quality_status"],
        "input_rows": report["input_rows"],
        "canonical_rows": report["metrics"]["canonical_rides"],
        "excluded_rows": report["input_rows"] - report["metrics"]["canonical_rides"],
        "quality_counts": report["dispositions"],
        "reason_counts": report["reason_counts"],
        "files": report["files"],
        "coverage": report["metrics"],
        "artifact_checksums": report["artifact_checksums"],
        "independent_audit": {
            "algorithm": baseline["algorithm"],
            "completed_published_rides": baseline["completed_published_rides"],
            "source_checksum": baseline["source_checksum"],
        },
    }
    with connect(settings) as connection:
        connection.execute("SELECT pg_advisory_xact_lock(7061641)")
        migrate(connection)
        existing = connection.execute(
            "SELECT metadata FROM publications WHERE id=%s", (version,)
        ).fetchone()
        if existing:
            if existing["metadata"] != metadata:
                raise ValueError("Immutable publication metadata differs")
            activate(connection, version)
            return version
        connection.execute(
            "INSERT INTO publications(id,metadata) VALUES(%s,%s)",
            (version, Jsonb(metadata)),
        )
        with duckdb.connect() as data:
            data.execute("SET TimeZone='UTC'; SET memory_limit='256MB'; SET threads=2")
            for name, columns in {
                "ride_hours": ["hour_utc", "completed_published_rides"],
                "station_hours": [
                    "station_id",
                    "hour_utc",
                    "departures_s",
                    "arrivals_s",
                ],
            }.items():
                rows = data.execute(
                    f"SELECT {','.join(columns)} FROM read_parquet(?)",
                    [str(result / f"{name}.parquet")],
                )
                statement = sql.SQL("COPY {} ({}) FROM STDIN").format(
                    sql.Identifier(name),
                    sql.SQL(",").join(
                        sql.Identifier(c) for c in ["publication_id", *columns]
                    ),
                )
                with connection.cursor().copy(statement) as copy:
                    while batch := rows.fetchmany(10000):
                        for row in batch:
                            copy.write_row((version, *row))
        totals = connection.execute(
            """
            SELECT (SELECT sum(completed_published_rides) FROM ride_hours
                    WHERE publication_id=%s) rides,
                   (SELECT coalesce(sum(departures_s),0) FROM station_hours
                    WHERE publication_id=%s) departures,
                   (SELECT coalesce(sum(arrivals_s),0) FROM station_hours
                    WHERE publication_id=%s) arrivals
        """,
            (version, version, version),
        ).fetchone()
        metrics = report["metrics"]
        if not totals or tuple(totals.values()) != (
            metrics["canonical_rides"],
            metrics["known_start_station_rides"],
            metrics["known_end_station_rides"],
        ):
            raise ValueError("PostgreSQL publication reconciliation failed")
        activate(connection, version)
    return version
