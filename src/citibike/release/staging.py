"""Fixed loopback staging namespace; owner-issued local release approval only."""

import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
from psycopg import sql
from pydantic import SecretStr

from citibike.release.database import migrate, reader
from citibike.release.recovery import copy_store, snapshot
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import read_json, write_json

ROOT = Path("data/staging")
ENV = ROOT / "runtime.env"
UP = ("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "120")
# Bounded real copy in FK order. The 84 GB F3 partition set and station geo
# (FK to F3) are deliberately NOT copied: metadata without partitions would
# publish false zero facts, so staging reports F3 as explicitly empty instead.
SEED_TABLES = (
    "publications",
    "ride_hours",
    "station_hours",
    "active_publication",
    "ml_forecast_publications",
    "ml_forecasts",
    "ml_forecast_active",
    "risk_publications",
    "risk_stations",
    "risk_policy_outcomes",
    "risk_station_outcomes",
    "risk_transfers",
    "risk_active",
)


def init() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: never rotate an existing database password implicitly.
    if not ENV.exists():
        with ENV.open("x", encoding="utf-8") as output:
            output.write(
                f"STAGING_DB_PASSWORD={secrets.token_hex(24)}\n"
                f"STAGING_READER_PASSWORD={secrets.token_hex(24)}\n"
                "RELEASE_ID=unreleased\n"
            )
        if os.name != "nt":
            ENV.chmod(0o600)
    (ROOT / "store").mkdir(exist_ok=True)


def environment() -> dict[str, str]:
    return dict(line.split("=", 1) for line in ENV.read_text().splitlines() if line)


def compose() -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV),
        "-f",
        "compose.staging.yaml",
        "--project-name",
        "bikeflow-staging",
    ]


def owner() -> DatabaseSettings:
    # Aliases only: field-name population would also read unrelated $USER.
    return DatabaseSettings.model_validate(
        {
            "PGHOST": "127.0.0.1",
            "PGPORT": 25432,
            "PGDATABASE": "bikeflow_staging",
            "PGUSER": "staging_owner",
            "PGPASSWORD": SecretStr(environment()["STAGING_DB_PASSWORD"]),
        }
    )


def run(*args: str, release: str | None = None) -> None:
    subprocess.run(
        [*compose(), *args],
        check=True,
        env={**os.environ, "RELEASE_ID": release or environment()["RELEASE_ID"]},
    )


def smoke(release: str) -> dict[str, Any]:
    with httpx.Client(timeout=15, trust_env=False) as client:
        ready = client.get("http://127.0.0.1:18000/ready")
        ready.raise_for_status()
        if ready.headers.get("x-bikeflow-release") != release:
            raise ValueError("Release identity mismatch")
        for path in ("/v1/publication", "/v1/kpi", "/v3/forecasts", "/v4/stations"):
            response = client.get("http://127.0.0.1:18000" + path)
            response.raise_for_status()
        for path in ("/", "/analytics", "/stations", "/forecasts", "/scenarios"):
            client.get("http://127.0.0.1:13000" + path).raise_for_status()
    return {"status": "PASS", "release": release, "api": 4, "pages": 5}


def deploy(release: str, approval: Path, *, build: bool = True) -> dict[str, Any]:
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", release):
        raise ValueError("Invalid release identity")
    approved = read_json(approval)
    if (
        approved.get("release") != release
        or approved.get("environment") != "local_staging"
        or approved.get("approved_by") != "owner"
        or not approved.get("approved")
    ):
        raise ValueError("Owner staging release approval required")
    previous = (
        read_json(ROOT / "active.json") if (ROOT / "active.json").exists() else None
    )
    # Images must exist BEFORE DB/schema/service mutation.
    if build:
        run("build", release=release)
    run(*UP, "db", release=release)
    migrate(owner())
    reader(owner(), environment()["STAGING_READER_PASSWORD"])
    try:
        run(*UP, "--no-deps", "api", "web", release=release)
        result = smoke(release)
    except Exception:
        if previous:
            run(*UP, "--no-deps", "api", "web", release=previous["release"])
            restored = smoke(previous["release"])
            print(f"Automatic rollback: {restored}", file=sys.stderr, flush=True)
        raise
    if previous and previous["release"] == release:
        previous = previous["previous"]  # Redeploy keeps the real rollback target.
    record(release, previous)
    return result


def record(release: str, previous: dict[str, Any] | None) -> None:
    write_json(ROOT / "active.json", {"release": release, "previous": previous})
    values = environment()
    values["RELEASE_ID"] = release
    ENV.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def rollback() -> dict[str, Any]:
    """Code/image rollback; forward-only migrations must stay backward compatible."""
    active = read_json(ROOT / "active.json")
    previous = active.get("previous")
    if not previous:
        raise ValueError("No previous staging release recorded")
    run(*UP, "--no-deps", "api", "web", release=previous["release"])
    result = smoke(previous["release"])
    record(previous["release"], previous.get("previous"))
    return {**result, "rolled_back_from": active["release"]}


def seed(source: DatabaseSettings, artifacts: dict[str, Path]) -> dict[str, Any]:
    """Copy real small publications + immutable artifacts; source is read-only."""
    target = owner()
    if (source.host, source.port, source.database) == (
        target.host,
        target.port,
        target.database,
    ):
        raise ValueError("Seed source must not be the staging database")
    tables = list(SEED_TABLES)
    before = snapshot(source, tables)
    current = snapshot(target, tables)
    if current == before:
        status = "unchanged"
    elif any(state["rows"] for state in current.values()):
        raise ValueError("Staging already holds different data; restore instead")
    else:
        status = "seeded"
        with connect(source, readonly=True) as src, connect(target) as dst:
            src.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            src.execute("SET LOCAL statement_timeout='15min'")
            for table in tables:
                columns = [
                    row["column_name"]
                    for row in dst.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name=%s "
                        "ORDER BY ordinal_position",
                        (table,),
                    ).fetchall()
                ]
                fields = sql.SQL(",").join(map(sql.Identifier, columns))
                relation = sql.Identifier(table)
                with (
                    src.cursor().copy(
                        sql.SQL(
                            "COPY (SELECT {} FROM {}) TO STDOUT (FORMAT BINARY)"
                        ).format(fields, relation)
                    ) as out,
                    dst.cursor().copy(
                        sql.SQL("COPY {} ({}) FROM STDIN (FORMAT BINARY)").format(
                            relation, fields
                        )
                    ) as into,
                ):
                    for block in out:
                        into.write(block)
        if snapshot(target, tables) != before:
            raise ValueError("Seeded staging data differs from source")
    if snapshot(source, tables) != before:
        raise ValueError("Seed source changed during copy")
    store = ROOT / "store"
    copied = {}
    for name, path in artifacts.items():
        destination = store / name
        if not destination.exists():
            copy_store(path, destination)
        copied[name] = sum(1 for p in destination.rglob("*") if p.is_file())
    return {
        "status": status,
        "tables": {k: v["rows"] for k, v in before.items()},
        "artifacts": copied,
        "excluded": "F3 analytics partitions (84 GB) and FK-dependent station geo",
    }
