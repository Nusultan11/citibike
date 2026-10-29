"""Memory-bounded exact route serving cube over verified immutable Gold."""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.build import configured, publish_directory
from citibike.analytics.contracts import CATEGORIES
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.serving.route_window_contract import VERSION, window_id, window_months
from citibike.storage import read_json, write_json

KEYS = "vehicle,membership,from_station,to_station"
VALUES = (
    "sum(n)::BIGINT n,sum(duration_us)::BIGINT duration_us,"
    "sum(duration_n)::BIGINT duration_n"
)
SHARDS = 32


def sharded_cube(
    db: duckdb.DuckDBPyConnection, work: Path, stage: Path
) -> dict[str, Any]:
    db.execute(
        """COPY (SELECT *, (hash(from_station)%32)::INTEGER shard_bucket
        FROM input) TO $path (FORMAT PARQUET,PARTITION_BY(shard_bucket),
        ROW_GROUP_SIZE 32768,COMPRESSION ZSTD)""",
        {"path": str(work / "shards")},
    )
    totals = {
        (v, m): dict.fromkeys(("groups", "n", "duration_us", "duration_n"), 0)
        for v in ("*", *CATEGORIES["vehicle"])
        for m in ("*", *CATEGORIES["membership"])
    }
    files = {}
    row_count = 0
    for number in range(SHARDS):
        folder = work / "shards" / f"shard_bucket={number}"
        if not folder.exists():
            continue
        db.read_parquet(str(folder / "*.parquet"), hive_partitioning=False).create_view(
            "chunk", replace=True
        )
        db.execute("DROP TABLE IF EXISTS current_cube")
        db.execute(
            f"CREATE TABLE current_cube AS SELECT {KEYS},{VALUES} "
            "FROM chunk GROUP BY ALL"
        )
        for vehicle, member in [
            ("vehicle", "'*'"),
            ("'*'", "membership"),
            ("'*'", "'*'"),
        ]:
            db.execute(
                f"INSERT INTO current_cube SELECT {vehicle},{member},"
                f"from_station,to_station,{VALUES} FROM chunk GROUP BY ALL"
            )
        # A pair always has exactly one from-station hash bucket, hence unique
        # per-bucket grains imply a globally unique grain without a giant hash.
        if db.execute(
            f"SELECT count(*) FROM current_cube WHERE hash(from_station)%32<>{number}"
        ).fetchone() != (0,):
            raise ValueError("Route shard routing mismatch")
        if db.execute(
            f"SELECT count(*) FROM (SELECT {KEYS} FROM current_cube "
            "GROUP BY ALL HAVING count(*)<>1)"
        ).fetchone() != (0,):
            raise ValueError("Route serving grain violated")
        if db.execute(
            "SELECT count(*) FROM current_cube "
            "WHERE n<0 OR duration_us<0 OR duration_n<0"
        ).fetchone() != (0,):
            raise ValueError("Invalid route measures")
        for (vehicle, member), metrics in totals.items():
            conditions = []
            params: list[Any] = []
            for column, value in (("vehicle", vehicle), ("membership", member)):
                if value != "*":
                    conditions.append(column + "=?")
                    params.append(value)
            clause = " AND ".join(conditions) or "true"
            db.execute("DROP TABLE IF EXISTS expected")
            db.execute(
                f"CREATE TABLE expected AS SELECT from_station,to_station,{VALUES} "
                f"FROM chunk WHERE {clause} GROUP BY 1,2",
                params,
            )
            if db.execute(
                """SELECT count(*) FROM (
                (SELECT * FROM expected EXCEPT ALL
                 SELECT from_station,to_station,n,duration_us,duration_n
                 FROM current_cube WHERE vehicle=? AND membership=?)
                UNION ALL
                (SELECT from_station,to_station,n,duration_us,duration_n
                 FROM current_cube WHERE vehicle=? AND membership=?
                 EXCEPT ALL SELECT * FROM expected))""",
                [vehicle, member] * 2,
            ).fetchone() != (0,):
                raise ValueError("Independent route rollup mismatch")
            row = db.execute("""SELECT count(*),coalesce(sum(n),0),
                coalesce(sum(duration_us),0),coalesce(sum(duration_n),0)
                FROM expected""").fetchone()
            assert row is not None
            for field, value in zip(metrics, row, strict=True):
                metrics[field] += int(value)
        count = db.execute("SELECT count(*) FROM current_cube").fetchone()
        assert count is not None
        row_count += int(count[0])
        output = stage / f"routes-{number:02}.parquet"
        db.execute(
            f"COPY (SELECT * FROM current_cube ORDER BY {KEYS}) TO ? "
            "(FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 32768)",
            [str(output)],
        )
        files[output.name] = checksum(output)
        print(
            f"Route shard {number + 1}/{SHARDS}: {count[0]} groups; "
            "16 independent slices matched",
            flush=True,
        )
    # Empty route populations still have a typed, valid artifact.
    if not files:
        output = stage / "routes-empty.parquet"
        db.execute(
            f"COPY (SELECT {KEYS},n,duration_us,duration_n FROM input WHERE false) "
            "TO ? (FORMAT PARQUET)",
            [str(output)],
        )
        files[output.name] = checksum(output)
    return {
        "rows": row_count,
        "checksums": files,
        "slices": [
            {"vehicle": v, "membership": m, **metrics}
            for (v, m), metrics in totals.items()
        ],
        "independent_slice_mismatches": {f"{v}:{m}": 0 for v, m in totals},
        "grain": "unique within 32 disjoint verified from-station hash shards",
    }


def build(gold: Path) -> Path:
    before = time.monotonic()
    report = read_json(gold / "manifest.json")
    identifier = window_id(report)
    target = gold.parent.parent / "route-windows" / identifier
    if target.exists():
        saved = read_json(target / "manifest.json")
        for name, expected in saved["checksums"].items():
            if checksum(target / name) != expected:
                raise ValueError("Route window checksum mismatch")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    months = window_months(report)
    inputs = []
    for part in months:
        for name in ("route_category_monthly", "route_monthly"):
            file = Path(part["path"]) / f"{name}.parquet"
            if checksum(file) != part["checksums"][file.name]:
                raise ValueError("Route input Gold checksum mismatch")
        inputs.append(
            {
                "month": part["month"],
                "partition_id": part["id"],
                "checksums": {
                    n: part["checksums"][n + ".parquet"]
                    for n in ("route_monthly", "route_category_monthly")
                },
            }
        )
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        work = Path(temporary)
        stage = work / "result"
        stage.mkdir()
        with configured(work / "compute.duckdb") as db:
            db.read_parquet(
                [
                    str(Path(p["path"]) / "route_category_monthly.parquet")
                    for p in months
                ]
            ).create_view("input")
            db.read_parquet(
                [str(Path(p["path"]) / "route_monthly.parquet") for p in months]
            ).create_view("base")
            materialized = sharded_cube(db, work, stage)
            expected_n = sum(p["metrics"]["route_n"] for p in months)
            if (
                next(
                    s["n"]
                    for s in materialized["slices"]
                    if s["vehicle"] == s["membership"] == "*"
                )
                != expected_n
            ):
                raise ValueError("Full route population reconciliation mismatch")
            monthly: list[dict[str, Any]] = []
            for relation, dimensions in [
                ("base", "'*' vehicle,'*' membership"),
                ("input", "vehicle,membership"),
            ]:
                rows = db.execute(
                    "SELECT strftime(local_month,'%Y-%m') month_key,"
                    f"{dimensions},count(*) group_count,{VALUES} "
                    f"FROM {relation} GROUP BY ALL"
                ).fetchall()
                monthly.extend(
                    dict(
                        zip(
                            [
                                "month",
                                "vehicle",
                                "membership",
                                "groups",
                                "n",
                                "duration_us",
                                "duration_n",
                            ],
                            row,
                            strict=True,
                        )
                    )
                    for row in rows
                )
        saved = {
            "window_id": identifier,
            "version": VERSION,
            "inputs": inputs,
            "period_start": report["period_start"],
            "period_end": report["period_end"],
            **materialized,
            "monthly_slices": monthly,
            "measurements": {
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
                "parquet_bytes": sum(p.stat().st_size for p in stage.glob("*.parquet")),
            },
        }
        write_json(stage / "manifest.json", saved)
        publish_directory(stage, target)
    return target


def prepare(gold: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "citibike.analytics.route_window", "--gold", str(gold)],
        check=True,
    )
    return (
        gold.parent.parent
        / "route-windows"
        / window_id(read_json(gold / "manifest.json"))
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gold", type=Path, required=True)
    print(build(parser.parse_args().gold))
