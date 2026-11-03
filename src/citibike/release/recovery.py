"""Verified PostgreSQL + immutable store recovery to NEW isolated destinations."""

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg import sql

from citibike.ingestion.archive import checksum
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import read_json, write_json


def snapshot(
    settings: DatabaseSettings, tables: list[str] | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with connect(settings, readonly=True) as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # Bounded, but full ordered digests of ~1M-row tables exceed the API 5s.
        db.execute("SET LOCAL statement_timeout='15min'")
        names = tables or [
            row["tablename"]
            for row in db.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "ORDER BY tablename"
            ).fetchall()
        ]
        for name in names:
            digest = hashlib.sha256()
            count = 0
            # Stable ordering independent of physical dump/restore order.
            with db.cursor(name="release_snapshot") as rows:
                rows.execute(
                    sql.SQL(
                        "SELECT row_to_json(t)::text AS payload FROM {} t "
                        'ORDER BY row_to_json(t)::text COLLATE "C"'
                    ).format(sql.Identifier(name))
                )
                for row in rows:
                    digest.update((row["payload"] + "\n").encode())
                    count += 1
            result[name] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def copy_store(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=False)
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Symlink in immutable store")
        if not path.is_file() or path.name.endswith(("-wal", "-shm", ".lock")):
            continue
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".sqlite" or path.name.endswith(".db"):
            # SQLite online-backup API includes committed WAL metadata.
            with sqlite3.connect(path) as original, sqlite3.connect(destination) as db:
                original.backup(db)
                if db.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                    raise ValueError("Invalid SQLite metadata")
        else:
            shutil.copyfile(path, destination)
            if checksum(path) != checksum(destination):
                raise ValueError("Immutable copy checksum mismatch")


def backup(
    compose: list[str], settings: DatabaseSettings, store: Path, target: Path
) -> Path:
    target.mkdir(parents=True, exist_ok=False)
    before = snapshot(settings)
    with (target / "postgres.dump").open("wb") as output:
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "db",
                "pg_dump",
                "-Fc",
                "--no-owner",
                "-U",
                settings.user,
                "-d",
                settings.database,
            ],
            stdout=output,
            stderr=subprocess.PIPE,
            check=True,
        )
    copy_store(store, target / "store")
    after = snapshot(settings)
    if before != after:
        raise ValueError("Writers changed during backup; quiesce and retry")
    write_json(
        target / "manifest.json",
        {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "database": before,
            "source_database": settings.database,
            "files": {
                p.relative_to(target).as_posix(): checksum(p)
                for p in sorted(target.rglob("*"))
                if p.is_file()
            },
            "scope": "quiesced staging PostgreSQL + explicit immutable/metadata store",
        },
    )
    return target


def verify(target: Path) -> dict[str, Any]:
    manifest = read_json(target / "manifest.json")
    if manifest["version"] != 1 or "postgres.dump" not in manifest["files"]:
        raise ValueError("Invalid backup manifest")
    for name, expected in manifest["files"].items():
        path = (target / name).resolve()
        if not path.is_relative_to(target.resolve()) or not path.is_file():
            raise ValueError("Unsafe or missing backup member")
        if checksum(path) != expected:
            raise ValueError("Backup checksum mismatch")
    return manifest


def restore(
    compose: list[str], settings: DatabaseSettings, source: Path, store: Path
) -> tuple[DatabaseSettings, dict[str, Any]]:
    manifest = verify(source)  # Validate BEFORE creating DB or writing artifacts.
    if store.exists():
        raise ValueError("Restore store must be a new directory")
    name = "bikeflow_restore_" + uuid4().hex
    with connect(settings) as db:
        db.autocommit = True
        db.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    restored = settings.model_copy(update={"database": name})
    # Never drop a database on failure. A failed isolated restore remains diagnosable.
    with (source / "postgres.dump").open("rb") as dump:
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "db",
                "pg_restore",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "-U",
                settings.user,
                "-d",
                name,
            ],
            stdin=dump,
            capture_output=True,
            check=True,
        )
    if snapshot(restored) != manifest["database"]:
        raise ValueError("Restored database differs")
    shutil.copytree(source / "store", store)
    for name, expected in manifest["files"].items():
        if name.startswith("store/") and checksum(store / name[6:]) != expected:
            raise ValueError("Restored artifact differs")
    return restored, {
        "database": restored.database,
        "tables": len(manifest["database"]),
        "files": len(manifest["files"]),
        "comparison": "PASS",
    }


def logical_digest(settings: DatabaseSettings) -> str:
    return hashlib.sha256(
        json.dumps(snapshot(settings), sort_keys=True).encode()
    ).hexdigest()


def model_parity(
    original: Path,
    restored: Path,
    bundle: str,
    dataset: str,
    origin: datetime,
    work: Path,
) -> dict[str, Any]:
    """Restored F4 bundle + dataset must reproduce the original forecast rows."""
    from citibike.forecasting.inference import predict

    rows = {}
    for label, root in (("original", original), ("restored", restored)):
        output = predict(
            work / label,
            root / "f4/models" / bundle,
            origin,
            activate=False,
            dataset_location=root / "f4/datasets" / dataset,
        )
        rows[label] = read_json(output / "forecasts.json")
    if rows["original"] != rows["restored"]:
        raise ValueError("Restored model artifacts do not reproduce forecasts")
    return {
        "comparison": "PASS",
        "origin": origin.isoformat(),
        "forecast_sha256": hashlib.sha256(
            json.dumps(rows["restored"], sort_keys=True).encode()
        ).hexdigest(),
    }
