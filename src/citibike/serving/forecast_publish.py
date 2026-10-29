"""Shape/lineage gates and one-transaction forecast publication / rollback."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg.types.json import Jsonb

from citibike.features.contracts import SCHEMA, utc
from citibike.forecasting.artifacts import checked
from citibike.forecasting.inference import rows_valid
from citibike.serving.db import DatabaseSettings, connect
from citibike.serving.publish import migrate as migrate_f1
from citibike.storage import identity


def migrate(db: psycopg.Connection[dict[str, Any]]) -> None:
    migrate_f1(db)
    for name in ("004_forecasts.sql", "005_forecast_value_status.sql"):
        content = (Path(__file__).with_name("migrations") / name).read_text(
            encoding="utf-8"
        )
        version = name[:3]
        digest = hashlib.sha256(content.encode()).hexdigest()
        row = db.execute(
            "SELECT checksum FROM schema_migrations WHERE version=%s", (version,)
        ).fetchone()
        if row:
            if row["checksum"] != digest:
                raise ValueError("Applied forecast migration checksum differs")
            continue
        db.execute(content)
        db.execute(
            "INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)",
            (version, digest),
        )


def validate(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    report = checked(path)
    if report.get("kind") != "forecast" or report.get("feature_schema") != SCHEMA:
        raise ValueError("Forecast schema mismatch")
    if utc(report["valid_until"]) <= datetime.now(UTC):
        raise ValueError("Forecast window stale; previous publication retained")
    bundle = checked(Path(report["model_bundle_path"]))
    if bundle["id"] != report["model_bundle_id"] or bundle["feature_schema"] != SCHEMA:
        raise ValueError("Forecast model/schema mismatch")
    data = checked(Path(bundle["dataset_path"]))
    for key in ("dataset_id", "source_snapshot_id", "gold_publication_id"):
        expected = data["id"] if key == "dataset_id" else data[key]
        if report[key] != bundle[key] or report[key] != expected:
            raise ValueError("Forecast source/model/dataset provenance mismatch")
    if report["station_version"] != data["checksums"]["stations.parquet"]:
        raise ValueError("Forecast station version mismatch")
    expected = identity(
        [
            bundle["id"],
            report["forecast_origin_utc"],
            report["station_ids"],
            SCHEMA,
            report["forecast_recipe"],
        ]
    )
    if expected != report["publication_id"] or expected != report["id"]:
        raise ValueError("Forecast identity mismatch")
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        cursor = db.execute(
            "SELECT * FROM read_parquet(?)", [str(path / "forecasts.parquet")]
        )
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]
    rows_valid(rows, report["station_ids"])
    if (
        len(rows) != report["rows"]
        or sum(r["status"] == "ok" for r in rows) != report["valid_rows"]
    ):
        raise ValueError("Forecast row count mismatch")
    for row in rows:
        if utc(row["feature_available_at"]) > utc(row["forecast_origin_utc"]):
            raise ValueError("Future forecast feature")
        if row["model_id"] != bundle["models"][row["prediction_type"]]["model_id"]:
            raise ValueError("Forecast model identity mismatch")
    if not report["valid_rows"]:
        raise ValueError("All forecasts unavailable; previous publication retained")
    return report, rows


def activate(db: psycopg.Connection[dict[str, Any]], identifier: str) -> None:
    db.execute("SELECT pg_advisory_xact_lock(7061644)")
    if not db.execute(
        "SELECT 1 FROM ml_forecast_publications WHERE publication_id=%s", (identifier,)
    ).fetchone():
        raise ValueError("Unknown forecast publication")
    db.execute(
        """INSERT INTO ml_forecast_active VALUES(true,%s)
        ON CONFLICT(singleton) DO UPDATE SET publication_id=EXCLUDED.publication_id""",
        (identifier,),
    )


def publish(
    path: Path,
    settings: DatabaseSettings | None = None,
    *,
    fail_after_copy: bool = False,
    fail_before_activate: bool = False,
) -> str:
    report, rows = validate(path)
    identifier: str = report["publication_id"]
    with connect(settings) as db:
        db.execute("SELECT pg_advisory_xact_lock(7061644)")
        migrate(db)
        old = db.execute(
            "SELECT manifest FROM ml_forecast_publications WHERE publication_id=%s",
            (identifier,),
        ).fetchone()
        if old:
            if old["manifest"] != report:
                raise ValueError("Immutable forecast manifest changed")
        else:
            db.execute(
                "INSERT INTO ml_forecast_publications(publication_id,manifest) "
                "VALUES(%s,%s)",
                (identifier, Jsonb(report)),
            )
            columns = [
                "station_id",
                "forecast_origin_utc",
                "target_hour_utc",
                "horizon_h",
                "prediction_type",
                "predicted_value",
                "status",
                "reason",
                "model_id",
                "feature_available_at",
            ]
            with db.cursor().copy("COPY ml_forecasts FROM STDIN") as copier:
                for row in rows:
                    copier.write_row([identifier, *(row[k] for k in columns)])
            if fail_after_copy:
                raise RuntimeError("Injected failure after forecast COPY")
        actual = db.execute(
            """SELECT count(*) n,count(*) FILTER(WHERE status='ok') valid,
            sum(predicted_value) total FROM ml_forecasts WHERE publication_id=%s""",
            (identifier,),
        ).fetchone()
        if (
            actual is None
            or actual["n"] != len(rows)
            or actual["valid"] != report["valid_rows"]
        ):
            raise ValueError("PostgreSQL forecast reconciliation failed")
        if (
            abs(float(actual["total"]) - sum(r["predicted_value"] or 0 for r in rows))
            > 1e-7
        ):
            raise ValueError("PostgreSQL prediction sum mismatch")
        if fail_before_activate:
            raise RuntimeError("Injected failure before forecast activation")
        activate(db, identifier)
    return identifier
