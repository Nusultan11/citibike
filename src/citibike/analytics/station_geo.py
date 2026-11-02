"""F6 station coordinates per NYC event month from the F3 publication's Silver.

Measured need: serving Gold only has 0.01 degree event grid cells, not station
points. Coordinates are the modal reported endpoint pair of accepted Silver events
for one station ID, NYC event month and side. IDs are never merged or inferred.
"""

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.build import configured, publish_directory, silver_reference
from citibike.ingestion.archive import checksum
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json

VERSION = "f6-station-geo-1"
POLICY = (
    "accepted F2 Silver events of the pinned F3 publication; per station_id, NYC"
    " local event month (start: started_at_local, end: ended_at_local) and side;"
    " modal finite WGS84 pair, ties by lat, lon; modal name; no ID merge, no"
    " GBFS or geocoder fill; missing coordinates counted, never imputed"
)


def publication_silver(publication: Path) -> tuple[dict[str, Any], list[Path]]:
    manifest = read_json(publication / "manifest.json")
    seen: dict[str, Path] = {}
    for month in manifest["months"]:
        for reference in month["silver"]:
            seen[reference["partition_id"]] = Path(reference["path"])
    return manifest, [seen[k] for k in sorted(seen)]


PARTIAL = """SELECT station_id, strftime(local_time,'%Y-%m') local_month, side,
    CASE WHEN valid THEN lat END lat, CASE WHEN valid THEN lon END lon,
    station_name, count(*)::BIGINT n FROM (
      SELECT start_station_id AS station_id, started_at_local AS local_time,
        'start' AS side, start_lat AS lat, start_lng AS lon,
        start_station_name AS station_name,
        coalesce(isfinite(start_lat) AND isfinite(start_lng)
          AND start_lat BETWEEN -90 AND 90 AND start_lng BETWEEN -180 AND 180,
          false) AS valid
      FROM silver WHERE disposition='accepted' AND start_station_id IS NOT NULL
      UNION ALL
      SELECT end_station_id, ended_at_local, 'end', end_lat, end_lng,
        end_station_name,
        coalesce(isfinite(end_lat) AND isfinite(end_lng)
          AND end_lat BETWEEN -90 AND 90 AND end_lng BETWEEN -180 AND 180, false)
      FROM silver WHERE disposition='accepted' AND end_station_id IS NOT NULL)
    GROUP BY ALL"""

FINAL = """WITH pairs AS (
      SELECT station_id, local_month, side, lat, lon, sum(n)::BIGINT n
      FROM partials WHERE lat IS NOT NULL GROUP BY ALL),
    ranked AS (SELECT *, row_number() OVER (PARTITION BY station_id, local_month,
      side ORDER BY n DESC, lat, lon) r FROM pairs),
    names AS (SELECT station_id, local_month, side, station_name, sum(n) n
      FROM partials WHERE station_name IS NOT NULL GROUP BY ALL),
    named AS (SELECT *, row_number() OVER (PARTITION BY station_id, local_month,
      side ORDER BY n DESC, station_name) r FROM names),
    totals AS (SELECT station_id, local_month, side, sum(n)::BIGINT events,
      sum(n) FILTER (WHERE lat IS NOT NULL)::BIGINT coordinate_rows
      FROM partials GROUP BY ALL),
    spread AS (SELECT station_id, local_month, side,
      count(*)::INTEGER distinct_coordinates FROM pairs GROUP BY ALL)
    SELECT t.station_id, t.local_month, t.side event_type, k.lat, k.lon,
      m.station_name, t.events, coalesce(t.coordinate_rows, 0) coordinate_rows,
      coalesce(k.n, 0)::BIGINT modal_rows,
      coalesce(s.distinct_coordinates, 0) distinct_coordinates
    FROM totals t
    LEFT JOIN ranked k USING (station_id, local_month, side)
    LEFT JOIN named m USING (station_id, local_month, side)
    LEFT JOIN spread s USING (station_id, local_month, side)
    WHERE coalesce(k.r, 1)=1 AND coalesce(m.r, 1)=1
    ORDER BY 1, 2, 3"""


def build(data_dir: Path = Path("data"), publication: Path | None = None) -> Path:
    before = time.monotonic()
    if publication is None:
        pointer = read_json(data_dir / "f3/active.json")
        publication = Path(pointer["path"])
        if publication.name != pointer["publication_id"]:
            raise ValueError("F3 active pointer mismatch")
    manifest, partitions = publication_silver(publication)
    if manifest["publication_id"] != publication.name:
        raise ValueError("F3 publication identity mismatch")
    references = [silver_reference(p) for p in partitions]
    identifier = identity(
        [
            VERSION,
            manifest["publication_id"],
            manifest["source_snapshot_id"],
            [(r["partition_id"], r["checksum"]) for r in references],
        ]
    )
    target = data_dir / "f3/station-geo" / identifier
    if target.exists():
        checked(target)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        work = Path(temporary)
        stage = work / "result"
        stage.mkdir()
        with configured(work / "compute.duckdb") as db:
            db.execute(
                "CREATE TABLE partials(station_id VARCHAR, local_month VARCHAR,"
                " side VARCHAR, lat DOUBLE, lon DOUBLE, station_name VARCHAR,"
                " n BIGINT)"
            )
            accepted = 0
            for reference in references:
                source = (Path(reference["path"]) / "silver.parquet").as_posix()
                relation = "read_parquet('" + source.replace("'", "''") + "')"
                columns = {
                    r[0]
                    for r in db.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
                }
                # Names are descriptive only; a Silver without them keeps NULL names.
                names = ",".join(
                    f"NULL::VARCHAR AS {c}"
                    for c in ("start_station_name", "end_station_name")
                    if c not in columns
                )
                db.execute(
                    "CREATE OR REPLACE VIEW silver AS SELECT *"
                    + (f",{names}" if names else "")
                    + f" FROM {relation}"
                )
                db.execute("INSERT INTO partials " + PARTIAL)
                accepted += reference["rows"]
            db.execute("CREATE TABLE geo AS " + FINAL)
            output = stage / "station_geo_monthly.parquet"
            db.execute(f"COPY geo TO '{output.as_posix()}' (FORMAT parquet)")
            summary = db.execute(
                """SELECT count(*), count(DISTINCT station_id), sum(events),
                sum(coordinate_rows), count(*) FILTER (WHERE lat IS NULL),
                count(*) FILTER (WHERE distinct_coordinates>1) FROM geo"""
            ).fetchone()
            duplicates = db.execute(
                "SELECT count(*) - count(DISTINCT (station_id,local_month,"
                "event_type)) FROM geo"
            ).fetchone()
        assert summary is not None and duplicates is not None
        if duplicates[0]:
            raise ValueError("Station geo grain is not unique")
        report = {
            "id": identifier,
            "version": VERSION,
            "policy": POLICY,
            "analytics_publication_id": manifest["publication_id"],
            "source_snapshot_id": manifest["source_snapshot_id"],
            "data_mode": manifest["data_mode"],
            "silver": references,
            "accepted_silver_rows": accepted,
            "rows": summary[0],
            "stations": summary[1],
            "station_events": int(summary[2] or 0),
            "coordinate_events": int(summary[3] or 0),
            "groups_without_coordinates": summary[4],
            "groups_with_multiple_coordinates": summary[5],
            "checksums": {output.name: checksum(output)},
            "created_at": datetime.now(UTC).isoformat(),
            "measurements": {
                "elapsed_seconds": round(time.monotonic() - before, 3),
                "peak_rss_bytes": peak_rss_bytes(),
            },
        }
        write_json(stage / "manifest.json", report)
        publish_directory(stage, target)
    return target


def checked(path: Path) -> dict[str, Any]:
    report = read_json(path / "manifest.json")
    if report["version"] != VERSION or report["id"] != path.name:
        raise ValueError("Station geo version/identity mismatch")
    for name, expected in report["checksums"].items():
        if checksum(path / name) != expected:
            raise ValueError(f"Station geo checksum mismatch: {name}")
    return report


def verify(path: Path, sample: int = 24) -> dict[str, Any]:
    """Independent mode() recomputation from Silver for deterministic samples."""
    report = checked(path)
    output = path / "station_geo_monthly.parquet"
    with duckdb.connect() as db:
        db.execute("SET memory_limit='2GB'; SET threads=2")
        stations = [
            r[0]
            for r in db.execute(
                "SELECT station_id FROM read_parquet(?) GROUP BY 1 "
                "ORDER BY md5(station_id) LIMIT ?",
                [str(output), sample],
            ).fetchall()
        ]
        files = [str(Path(r["path"]) / "silver.parquet") for r in report["silver"]]
        oracle = db.execute(
            """SELECT station_id, strftime(t,'%Y-%m') m, side,
            count(*) events, count(lat) coords
            FROM (SELECT start_station_id station_id, started_at_local t,
                'start' side, CASE WHEN isfinite(start_lat) AND isfinite(start_lng)
                THEN start_lat END lat FROM read_parquet(?)
                WHERE disposition='accepted' AND list_contains(?, start_station_id)
              UNION ALL SELECT end_station_id, ended_at_local, 'end',
                CASE WHEN isfinite(end_lat) AND isfinite(end_lng) THEN end_lat END
                FROM read_parquet(?)
                WHERE disposition='accepted' AND list_contains(?, end_station_id))
            GROUP BY ALL""",
            [files, stations, files, stations],
        ).fetchall()
        published = {
            (r[0], r[1], r[2]): r[3:]
            for r in db.execute(
                "SELECT station_id, local_month, event_type, events, "
                "coordinate_rows, lat, lon, modal_rows FROM read_parquet(?) "
                "WHERE list_contains(?, station_id)",
                [str(output), stations],
            ).fetchall()
        }
        mode = {
            (r[0], r[1], r[2]): r[3:]
            for r in db.execute(
                """SELECT station_id, m, side, max(n) top,
                min(lat) FILTER (WHERE r=1), min(lon) FILTER (WHERE r=1) FROM (
                  SELECT *, rank() OVER (PARTITION BY station_id, m, side
                    ORDER BY n DESC, lat, lon) r FROM (
                    SELECT station_id, strftime(t,'%Y-%m') m, side, lat, lon,
                      count(*) n FROM (
                      SELECT start_station_id station_id, started_at_local t,
                        'start' side, start_lat lat, start_lng lon
                      FROM read_parquet(?) WHERE disposition='accepted'
                        AND list_contains(?, start_station_id) AND isfinite(start_lat)
                        AND isfinite(start_lng)
                      UNION ALL SELECT end_station_id, ended_at_local, 'end',
                        end_lat, end_lng FROM read_parquet(?)
                      WHERE disposition='accepted' AND list_contains(?, end_station_id)
                        AND isfinite(end_lat) AND isfinite(end_lng))
                    GROUP BY ALL))
                GROUP BY 1, 2, 3""",
                [files, stations, files, stations],
            ).fetchall()
        }
    mismatches = []
    for station, month, side, events, coords in oracle:
        value = published.get((station, month, side))
        expected = mode.get((station, month, side), (0, None, None))
        if value is None or value[:2] != (events, coords):
            mismatches.append([station, month, side, "counts"])
        elif value[2:] != (expected[1], expected[2], expected[0]) and coords:
            mismatches.append([station, month, side, "modal"])
    if len(oracle) != len(published):
        mismatches.append(["groups", len(oracle), len(published)])
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "geo_id": report["id"],
        "sample_stations": len(stations),
        "groups_checked": len(oracle),
        "mismatches": mismatches,
        "method": "independent DuckDB count/rank over raw Silver files",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--geo", type=Path)
    parser.add_argument("--sample", type=int, default=24)
    args = parser.parse_args()
    if args.command == "build":
        path = build(args.data_dir)
        print(json.dumps({"path": str(path), **checked(path)}, default=str)[:2000])
    else:
        if args.geo is None:
            parser.error("verify requires --geo")
        result = verify(args.geo, args.sample)
        print(json.dumps(result))
        if result["status"] != "PASS":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
