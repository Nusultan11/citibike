"""Verified COPY/indexing of exact route rollups and monthly rank indices."""

import hashlib
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from citibike.ingestion.archive import checksum
from citibike.serving.route_window_contract import (
    month_table,
    window_months,
    window_table,
)
from citibike.storage import read_json


def publish_window(db: psycopg.Connection[dict[str, Any]], path: Path) -> None:
    before = time.monotonic()
    report = read_json(path / "manifest.json")
    identifier = report["window_id"]
    for name, expected in report["checksums"].items():
        if checksum(path / name) != expected:
            raise ValueError("Serving route rollup checksum mismatch")
    old = db.execute(
        "SELECT manifest FROM analytics_route_windows WHERE window_id=%s", (identifier,)
    ).fetchone()
    if old:
        if old["manifest"] != report:
            raise ValueError("Immutable route serving rollup changed")
        return
    table = window_table(identifier)
    db.execute(
        sql.SQL(
            "CREATE TABLE {} (LIKE analytics_route_window_facts INCLUDING CONSTRAINTS)"
        ).format(sql.Identifier(table))
    )
    with tempfile.TemporaryDirectory() as temporary:
        csv = Path(temporary) / "routes.csv"
        with duckdb.connect() as source:
            source.execute("SET threads=2; SET memory_limit='512MB'")
            source.execute(
                """COPY (SELECT $id window_id,vehicle,membership,
                from_station,to_station,n,duration_us,duration_n
                FROM read_parquet($input)) TO $output (FORMAT CSV,HEADER false)""",
                {
                    "id": identifier,
                    "input": [str(path / name) for name in report["checksums"]],
                    "output": str(csv),
                },
            )
        with db.cursor().copy(
            sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV)").format(
                sql.Identifier(table)
            )
        ) as copier:
            with csv.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    copier.write(chunk)
    actual = db.execute(
        sql.SQL(
            "SELECT vehicle,membership,count(*) groups,sum(n) "
            "n,sum(duration_us) duration_us,sum(duration_n) duration_n "
            "FROM {} GROUP BY 1,2"
        ).format(sql.Identifier(table))
    ).fetchall()
    by_slice = {
        (r["vehicle"], r["membership"]): {
            k: int(r[k]) for k in ("groups", "n", "duration_us", "duration_n")
        }
        for r in actual
    }
    for wanted in report["slices"]:
        got = by_slice.get(
            (wanted["vehicle"], wanted["membership"]),
            dict.fromkeys(("groups", "n", "duration_us", "duration_n"), 0),
        )
        if any(got[k] != wanted[k] for k in got):
            raise ValueError(
                "Route PG/Silver-backed Gold slice reconciliation mismatch"
            )
    if sum(r["groups"] for r in by_slice.values()) != report["rows"]:
        raise ValueError("Route COPY row count mismatch")
    for clause in [
        "CREATE UNIQUE INDEX ON {} (vehicle,membership,from_station,to_station)",
        (
            "CREATE INDEX ON {} (vehicle,membership,n "
            "DESC,from_station,to_station) "
            "INCLUDE(duration_us,duration_n)"
        ),
        (
            "CREATE INDEX ON {} (vehicle,membership,to_station,n "
            "DESC,from_station) INCLUDE(duration_us,duration_n)"
        ),
    ]:
        db.execute(sql.SQL(clause).format(sql.Identifier(table)))
    db.execute(
        sql.SQL(
            "ALTER TABLE analytics_route_window_facts ATTACH PARTITION {}"
            " FOR VALUES IN ({})"
        ).format(sql.Identifier(table), sql.Literal(identifier))
    )
    db.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(table)))
    db.execute(
        "INSERT INTO analytics_route_windows(window_id,manifest) VALUES(%s,%s)",
        (identifier, Jsonb(report)),
    )
    print(
        f"Serving route window {identifier}: {report['rows']} rows, "
        f"{time.monotonic() - before:.3f}s",
        flush=True,
    )


def month_indices(
    db: psycopg.Connection[dict[str, Any]], report: dict[str, Any]
) -> None:
    for part in window_months(report):
        for mart, keys in [
            ("route_monthly", "n DESC,from_station,to_station"),
            (
                "route_category_monthly",
                "vehicle,membership,n DESC,from_station,to_station",
            ),
        ]:
            table = month_table(mart, part["id"])
            index = "arank_" + hashlib.sha256(table.encode()).hexdigest()[:32]
            db.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {} ("
                    + keys
                    + ") INCLUDE(duration_us,duration_n)"
                ).format(sql.Identifier(index), sql.Identifier(table))
            )
