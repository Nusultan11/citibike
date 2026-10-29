"""Controlled real transformations, never official-data / accuracy evidence."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from citibike.analytics.availability import build_availability
from citibike.analytics.build import checked, prepare_contribution, prepare_month
from citibike.analytics.contracts import GEO_VERSION, VERSION
from citibike.ingestion.archive import checksum
from citibike.processing.history_publish import PUBLICATION_VERSION
from citibike.storage import identity, write_json


def gold_fixture(root: Path) -> Path:
    end = (
        datetime.now(UTC)
        .astimezone(ZoneInfo("America/New_York"))
        .replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )
    source = root / "fixture_silver/hourly"
    source.mkdir(parents=True, exist_ok=True)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        db.execute(
            """CREATE TABLE silver AS SELECT
            sha256(s::VARCHAR||h::VARCHAR||j::VARCHAR) ride_id,
            h+INTERVAL '15 min' started_at_utc,
            h+INTERVAL '75 min' ended_at_utc,
            timezone('America/New_York',h+INTERVAL '15 min') started_at_local,
            timezone('America/New_York',h+INTERVAL '75 min') ended_at_local,
            lpad(s::VARCHAR,3,'0') start_station_id,
            lpad(s::VARCHAR,3,'0') end_station_id,
            'classic_bike' rideable_type,'member' member_casual,
            40.7 start_lat,-74.0 start_lng,40.7 end_lat,-74.0 end_lng,
            'accepted' disposition
            FROM generate_series(TIMESTAMPTZ '2024-10-01 04:00:00+00',
                ?::TIMESTAMPTZ,INTERVAL '1 hour') hours(h),
                range(4) stations(s),range(1,4) copies(j)
            WHERE j<=s+1 AND (s<>3 OR h>=TIMESTAMPTZ '2025-01-01 05:00:00+00')
            AND (extract(hour FROM h)::INT+s)%5<>0""",
            [end - timedelta(hours=2)],
        )
        # Deliberate genuine zeros, independent arrival hour, and station 003
        # first known in Jan 2025. City always has other observed stations.
        db.execute(
            "COPY silver TO ? (FORMAT PARQUET)", [str(source / "silver.parquet")]
        )
        count = db.execute("SELECT count(*) FROM silver").fetchone()
        assert count is not None
    write_json(
        source / "quality.json",
        {
            "pipeline_version": PUBLICATION_VERSION,
            "source_version": checksum(source / "silver.parquet"),
            "period": "2024-10",
            "data_mode": "controlled_fixture",
            "dispositions": {"accepted": count[0]},
            "checksums": {"silver.parquet": checksum(source / "silver.parquet")},
        },
    )
    contribution = prepare_contribution(source, root)
    parts = []
    for month in checked(contribution)["months"]:
        path = prepare_month(month, [contribution], root)
        parts.append({"path": str(path), **checked(path)})
    available = build_availability(root)
    identifier = identity(
        ["f4-fixture", VERSION, [p["id"] for p in parts], available["id"]]
    )
    target = root / "f3/publications" / identifier
    if (target / "manifest.json").exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    snapshot = root / "fixture_snapshot"
    snapshot.mkdir(exist_ok=True)
    write_json(
        snapshot / "manifest.json",
        {"data_mode": "controlled_fixture", "partitions": [{"path": str(source)}]},
    )
    write_json(
        target / "manifest.json",
        {
            "id": identifier,
            "publication_id": identifier,
            "pipeline_version": VERSION,
            "geo_mapping_version": GEO_VERSION,
            "source_snapshot_id": identity(
                ["fixture", checksum(source / "silver.parquet")]
            ),
            "source_snapshot": str(snapshot),
            "source_manifest_checksum": checksum(snapshot / "manifest.json"),
            "source": "controlled hourly fixture; not official data",
            "scope": "NYC",
            "timezone": "America/New_York",
            "data_mode": "controlled_fixture",
            "period_start": "2024-10-01T04:00:00+00:00",
            "period_end": end.isoformat(),
            "data_as_of": end.isoformat(),
            "data_as_of_kind": "controlled fixture",
            "accepted_silver_rows": count[0],
            "months": parts,
            "availability": available,
            "quality_status": "warning",
            "checksums": {},
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return target
