"""Immutable physical Gold partitions and transactional publication membership."""

import hashlib
import time
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from citibike.analytics.build import checked
from citibike.analytics.contracts import MARTS
from citibike.processing.resources import peak_rss_bytes
from citibike.serving.db import DatabaseSettings, connect
from citibike.serving.publish import migrate as migrate_f1
from citibike.storage import read_json, write_json


def migrate(db: psycopg.Connection[dict[str, Any]]) -> None:
    migrate_f1(db)
    content = (Path(__file__).with_name("migrations") / "002_analytics.sql").read_text(
        encoding="utf-8"
    )
    digest = hashlib.sha256(content.encode()).hexdigest()
    row = db.execute(
        "SELECT checksum FROM schema_migrations WHERE version='002'"
    ).fetchone()
    if row:
        if row["checksum"] != digest:
            raise ValueError("Applied analytics migration checksum differs")
        return
    db.execute(content)
    db.execute(
        "INSERT INTO schema_migrations(version,checksum) VALUES ('002',%s)", (digest,)
    )


def validate_gold(path: Path) -> dict[str, Any]:
    report = checked(path)
    if (
        sum(p["metrics"]["n"] for p in report["months"])
        != report["accepted_silver_rows"]
    ):
        raise ValueError("Gold whole-window source count mismatch")
    if (
        sum(p["metrics"]["arrival_rows"] for p in report["months"])
        != report["accepted_silver_rows"]
    ):
        raise ValueError("Gold whole-window arrival count mismatch")
    if report.get("source_manifest_checksum"):
        from citibike.ingestion.archive import checksum

        if (
            checksum(Path(report["source_snapshot"]) / "manifest.json")
            != report["source_manifest_checksum"]
        ):
            raise ValueError("F2 lineage manifest checksum mismatch")
    for part in report["months"]:
        actual = checked(Path(part["path"]))
        if actual != {k: v for k, v in part.items() if k != "path"}:
            raise ValueError("Gold partition manifest differs from publication")
        if any(actual["independent_group_mismatches"].values()):
            raise ValueError("Independent Silver reconciliation failed")
    checked(Path(report["availability"]["path"]))
    return report


def activate(db: psycopg.Connection[dict[str, Any]], identifier: str) -> None:
    if not db.execute(
        "SELECT 1 FROM analytics_publications WHERE publication_id=%s", (identifier,)
    ).fetchone():
        raise ValueError("Unknown immutable analytics publication")
    db.execute(
        """INSERT INTO analytics_active(singleton,publication_id) VALUES(true,%s)
        ON CONFLICT(singleton) DO UPDATE SET publication_id=EXCLUDED.publication_id""",
        (identifier,),
    )


def _copy_partition(
    db: psycopg.Connection[dict[str, Any]],
    part: dict[str, Any],
    name: str,
    temporary: Path,
) -> dict[str, Any]:
    before = time.monotonic()
    mart = MARTS[name]
    columns = ",".join([*mart.keys, *mart.measures])
    child = f"a_{hashlib.sha256(name.encode()).hexdigest()[:8]}_{part['id'][:32]}"
    parent = f"analytics_{name}"
    db.execute(
        sql.SQL(
            "CREATE TABLE {} (LIKE {} INCLUDING DEFAULTS INCLUDING CONSTRAINTS)"
        ).format(sql.Identifier(child), sql.Identifier(parent))
    )
    csv = temporary / "copy.csv"
    with duckdb.connect() as source:
        source.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        source.read_parquet(str(Path(part["path"]) / f"{name}.parquet")).create_view(
            "fact"
        )
        # Manifest IDs are hashes validated before they are used as literals.
        source.execute(
            (
                f"COPY (SELECT $partition AS partition_id,{columns} FROM fact) "
                "TO $output (FORMAT CSV, "
                "HEADER false)"
            ),
            {"partition": part["id"], "output": str(csv)},
        )
    with db.cursor().copy(
        sql.SQL("COPY {} FROM STDIN WITH (FORMAT CSV)").format(sql.Identifier(child))
    ) as copier:
        with csv.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                copier.write(block)
    csv.unlink()
    count = db.execute(
        sql.SQL("SELECT count(*) n FROM {}").format(sql.Identifier(child))
    ).fetchone()
    if not count or count["n"] != part["rows"][name]:
        raise ValueError("Serving COPY count mismatch")
    keys = [mart.time_key] + [k for k in mart.keys if k != mart.time_key]
    if "station_id" in keys:
        keys = ["station_id", mart.time_key] + [
            k for k in keys if k not in ("station_id", mart.time_key)
        ]
    db.execute(
        sql.SQL("CREATE UNIQUE INDEX ON {} ({})").format(
            sql.Identifier(child),
            sql.SQL(",").join(map(sql.Identifier, ["partition_id", *keys])),
        )
    )
    if "from_station" in keys:
        db.execute(
            sql.SQL("CREATE INDEX ON {} (from_station,{},to_station)").format(
                sql.Identifier(child), sql.Identifier(mart.time_key)
            )
        )
        db.execute(
            sql.SQL("CREATE INDEX ON {} (to_station,{})").format(
                sql.Identifier(child), sql.Identifier(mart.time_key)
            )
        )
    # Unique index checks grain independently after COPY. Aggregate sums are
    # checked against Gold inside the same transaction before attaching.
    with duckdb.connect() as source:
        sums = source.execute(
            "SELECT "
            + ",".join(f"coalesce(sum({c}),0)" for c in mart.measures)
            + " FROM read_parquet(?)",
            [str(Path(part["path"]) / f"{name}.parquet")],
        ).fetchone()
    actual = db.execute(
        sql.SQL("SELECT {} FROM {}").format(
            sql.SQL(",").join(
                sql.SQL("coalesce(sum({}),0) AS {}").format(
                    sql.Identifier(c), sql.Identifier(c)
                )
                for c in mart.measures
            ),
            sql.Identifier(child),
        )
    ).fetchone()
    if actual is None or tuple(actual.values()) != sums:
        raise ValueError("Serving additive reconciliation mismatch")
    db.execute(
        sql.SQL("ALTER TABLE {} ATTACH PARTITION {} FOR VALUES IN ({})").format(
            sql.Identifier(parent), sql.Identifier(child), sql.Literal(part["id"])
        )
    )
    db.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(child)))
    elapsed = round(time.monotonic() - before, 3)
    print(f"Serving {part['month']}/{name}: {count['n']} rows, {elapsed}s", flush=True)
    return {"mart": name, "rows": count["n"], "elapsed_seconds": elapsed}


def publish(
    path: Path,
    settings: DatabaseSettings | None = None,
    *,
    fail_before_activate: bool = False,
) -> str:
    import tempfile

    before = time.monotonic()
    report = validate_gold(path)
    identifier: str = report["publication_id"]
    measurements: list[dict[str, Any]] = []
    with connect(settings) as db:
        db.execute("SELECT pg_advisory_xact_lock(7061643)")
        migrate(db)
    # Each immutable physical month is committed independently. It is invisible
    # to readers until the complete publication membership is atomically activated.
    for part in report["months"]:
        with connect(settings) as db, tempfile.TemporaryDirectory() as temporary:
            db.execute("SET LOCAL statement_timeout='30min'")
            db.execute("SELECT pg_advisory_xact_lock(7061643)")
            existing = db.execute(
                "SELECT manifest FROM analytics_partitions WHERE partition_id=%s",
                (part["id"],),
            ).fetchone()
            if existing:
                if existing["manifest"] != part:
                    raise ValueError("Immutable serving partition changed")
                measurements.append({"month": part["month"], "mode": "reused"})
                continue
            copied = []
            for name in MARTS:
                copied.append(_copy_partition(db, part, name, Path(temporary)))
            db.execute(
                (
                    "INSERT INTO "
                    "analytics_partitions(partition_id,local_month,manifest) "
                    "VALUES(%s,%s,%s)"
                ),
                (part["id"], part["month"] + "-01", Jsonb(part)),
            )
        print(f"Serving month {part['month']} committed", flush=True)
        measurements.append(
            {"month": part["month"], "mode": "copied", "tables": copied}
        )
    with connect(settings) as db:
        db.execute("SELECT pg_advisory_xact_lock(7061643)")
        existing = db.execute(
            "SELECT manifest FROM analytics_publications WHERE publication_id=%s",
            (identifier,),
        ).fetchone()
        if existing and existing["manifest"] != report:
            raise ValueError("Immutable analytics publication changed")
        if not existing:
            db.execute(
                (
                    "INSERT INTO analytics_publications(publication_id,manifest) "
                    "VALUES(%s,%s)"
                ),
                (identifier, Jsonb(report)),
            )
            for part in report["months"]:
                db.execute(
                    "INSERT INTO analytics_publication_partitions VALUES(%s,%s,%s)",
                    (identifier, part["id"], part["month"] + "-01"),
                )
            availability = report["availability"]
            with duckdb.connect() as source:
                source.execute("SET TimeZone='UTC'")
                rows = source.execute(
                    "SELECT * FROM read_parquet(?)",
                    [str(Path(availability["path"]) / "intervals.parquet")],
                ).fetchall()
            with db.cursor().copy(
                "COPY "
                "analytics_availability(publication_id,station_id,trip_station_id,valid_from,valid_to,rentable_bikes,returnable_docks,collection_id,source_updated)"
                " FROM STDIN"
            ) as copier:
                for row in rows:
                    copier.write_row((identifier, *row))
        if fail_before_activate:
            raise RuntimeError("Injected failure before analytics activation")
        activate(db, identifier)
    print(
        f"Serving publication {identifier}: {time.monotonic() - before:.3f}s",
        flush=True,
    )
    write_json(
        path.parent.parent / "serving-runs" / f"{time.time_ns()}.json",
        {
            "publication_id": identifier,
            "elapsed_seconds": round(time.monotonic() - before, 3),
            "peak_rss_bytes": peak_rss_bytes(),
            "partitions": measurements,
            "activation": (
                "committed; immutable partition COPY/grain/additive sums checked"
            ),
        },
    )
    return identifier


def active_path(root: Path) -> Path:
    return Path(read_json(root / "f3/active.json")["path"])
