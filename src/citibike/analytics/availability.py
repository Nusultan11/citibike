"""Conservative step intervals from verified F2 snapshots; no gap interpolation."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb

from citibike.analytics.contracts import GBFS_VERSION, VERSION
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json


def intervals(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    ordered = sorted(collections, key=lambda c: c["received"])
    for index, collection in enumerate(ordered):
        feeds = collection["feeds"]
        received = datetime.fromisoformat(collection["received"])
        expiries = [received + timedelta(seconds=60)]
        for name in ("station_information", "station_status"):
            feed = feeds[name]
            expiries.append(
                datetime.fromisoformat(feed["last_updated"])
                + timedelta(seconds=feed["ttl"])
            )
        if index + 1 < len(ordered):
            expiries.append(datetime.fromisoformat(ordered[index + 1]["received"]))
        for row in collection["rows"]:
            if row["quality_status"] != "valid" or not row["last_reported"]:
                continue
            if any(feeds[n]["ttl"] <= 0 for n in feeds):
                continue
            until = min(
                *expiries,
                datetime.fromisoformat(row["last_reported"])
                + timedelta(seconds=min(60, row["ttl"])),
            )
            if until <= received:
                continue
            # Valid non-operating status still contributes confirmed observed
            # time; rent/return measures stay null rather than zero availability.
            result.append(
                {
                    "station_id": row["station_id"],
                    "trip_station_id": row.get("trip_station_id"),
                    "valid_from": received.isoformat(),
                    "valid_to": until.isoformat(),
                    "rentable_bikes": row["rentable_bikes"],
                    "returnable_docks": row["returnable_docks"],
                    "collection_id": collection["id"],
                    "source_updated": feeds["station_status"]["last_updated"],
                }
            )
    return result


def build_availability(root: Path) -> dict[str, Any]:
    collections = []
    references = []
    for path in sorted((root / "gbfs/silver/f2-gbfs-2").glob("*/quality.json")):
        quality = read_json(path)
        if quality["pipeline_version"] != "f2-gbfs-2":
            raise ValueError("Unsupported GBFS normalization version")
        parquet = path.parent / "station_status.parquet"
        if checksum(parquet) != quality["checksum"]:
            raise ValueError("GBFS Silver checksum mismatch")
        # Read the checksummed artifact, not an unchecked JSON sidecar.
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'")
            cursor = db.execute("SELECT * FROM read_parquet(?)", [str(parquet)])
            names = [c[0] for c in cursor.description]
            rows = [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]
        for row in rows:
            for field in ("last_reported", "received_at_utc"):
                if row[field] is not None:
                    row[field] = row[field].isoformat()
        feeds = quality["manifest"]["feeds"]
        received = max(
            feeds[n]["received_at_utc"]
            for n in ("station_status", "station_information")
        )
        collections.append(
            {
                "id": path.parent.name,
                "received": received,
                "feeds": {
                    n: feeds[n] for n in ("station_information", "station_status")
                },
                "rows": rows,
            }
        )
        references.append(
            {
                "collection_id": path.parent.name,
                "silver_checksum": quality["checksum"],
                "quality_checksum": checksum(path),
                "path": str(parquet),
                "feeds": feeds,
            }
        )
    identifier = identity([GBFS_VERSION, references])
    target = root / "f3/availability" / identifier
    if target.exists():
        report = read_json(target / "manifest.json")
        if (
            checksum(target / "intervals.parquet")
            != report["checksums"]["intervals.parquet"]
        ):
            raise ValueError("GBFS interval checksum mismatch")
        return {"path": str(target), **report}
    target.parent.mkdir(parents=True, exist_ok=True)
    observed = intervals(collections)
    received = [c["received"] for c in collections]
    report = {
        "id": identifier,
        "pipeline_version": VERSION,
        "availability_version": GBFS_VERSION,
        "collections": references,
        "intervals": len(observed),
        "period_start": min(received) if received else None,
        "period_end": max([*received, *(r["valid_to"] for r in observed)])
        if received
        else None,
        "expected_stations": len(
            {r["station_id"] for collection in collections for r in collection["rows"]}
        ),
        "policy": (
            "valid only; step hold to earliest positive feed/report TTL, "
            "next receipt, or 60s; no interpolation; ratios require >=95% "
            "coverage, >=2 distinct updates and >=1 refresh cycle; "
            "confirmed non-operating time remains observed, with separate "
            "rental/return operating denominators"
        ),
    }
    with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
        stage = Path(temporary) / "result"
        stage.mkdir()
        write_json(stage / "intervals.json", observed)
        with duckdb.connect() as db:
            db.execute(
                """COPY (SELECT * FROM read_json($input, columns={
                station_id:'VARCHAR',trip_station_id:'VARCHAR',
                valid_from:'TIMESTAMPTZ',valid_to:'TIMESTAMPTZ',
                rentable_bikes:'BIGINT',returnable_docks:'BIGINT',
                collection_id:'VARCHAR',source_updated:'TIMESTAMPTZ'}))
                TO $output (FORMAT PARQUET)""",
                {
                    "input": str(stage / "intervals.json"),
                    "output": str(stage / "intervals.parquet"),
                },
            )
            if db.execute(
                """SELECT count(*) FROM (SELECT valid_from,
                lag(valid_to) OVER(PARTITION BY station_id
                    ORDER BY valid_from) previous_end
                FROM read_parquet(?)) WHERE valid_from<previous_end""",
                [str(stage / "intervals.parquet")],
            ).fetchone() != (0,):
                raise ValueError("GBFS overlapping station intervals")
        report["checksums"] = {
            "intervals.parquet": checksum(stage / "intervals.parquet")
        }
        write_json(stage / "manifest.json", report)
        from citibike.analytics.build import publish_directory

        publish_directory(stage, target)
    return {"path": str(target), **report}
