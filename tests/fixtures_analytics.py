"""Synthetic UTC Silver for analytical edge cases; never official-data evidence."""

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

from citibike.analytics.availability import build_availability
from citibike.analytics.build import checked, prepare_contribution, prepare_month
from citibike.analytics.contracts import GEO_VERSION, VERSION
from citibike.ingestion.archive import checksum
from citibike.processing.history_publish import PUBLICATION_VERSION
from citibike.storage import identity, write_json


def analytical_silver(db: duckdb.DuckDBPyConnection) -> None:
    db.execute("SET TimeZone='UTC'")
    db.execute("""CREATE TABLE silver(ride_id VARCHAR,started_at_utc TIMESTAMPTZ,
        ended_at_utc TIMESTAMPTZ,started_at_local TIMESTAMP,ended_at_local TIMESTAMP,
        start_station_id VARCHAR,end_station_id VARCHAR,rideable_type VARCHAR,
        member_casual VARCHAR,start_lat DOUBLE,start_lng DOUBLE,
        end_lat DOUBLE,end_lng DOUBLE)""")
    records = [
        (
            "2024-10-01T04:00:00+00:00",
            "2024-10-01T04:05:00+00:00",
            "001",
            "002",
            "classic_bike",
            "member",
            40.78,
            -73.98,
        ),
        (
            "2024-10-01T03:55:00+00:00",
            "2024-10-01T04:05:00+00:00",
            "001",
            None,
            "electric_bike",
            "casual",
            None,
            None,
        ),
        (
            "2024-11-01T03:59:00+00:00",
            "2024-11-01T04:01:00+00:00",
            None,
            "001",
            "new_type",
            None,
            91.0,
            -74.0,
        ),
        (
            "2024-11-03T05:30:00+00:00",
            "2024-11-03T06:30:00+00:00",
            "002",
            "001",
            "classic_bike",
            "member",
            40.7,
            -74.0,
        ),
        (
            "2024-11-03T06:30:00+00:00",
            "2024-11-03T06:45:00+00:00",
            "002",
            "001",
            "electric_bike",
            "casual",
            40.7,
            -74.0,
        ),
        (
            "2025-03-09T06:50:00+00:00",
            "2025-03-09T07:10:00+00:00",
            "001",
            "002",
            "classic_bike",
            "member",
            89.999999999,
            179.999999999,
        ),
    ]
    for number, (a, b, start, end, vehicle, member, lat, lon) in enumerate(records, 1):
        begin, finish = datetime.fromisoformat(a), datetime.fromisoformat(b)
        local = ZoneInfo("America/New_York")
        db.execute(
            "INSERT INTO silver VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                str(number),
                begin,
                finish,
                begin.astimezone(local).replace(tzinfo=None),
                finish.astimezone(local).replace(tzinfo=None),
                start,
                end,
                vehicle,
                member,
                lat,
                lon,
                40.7,
                -74.0,
            ],
        )


def analytical_gold(
    root: Path, *, changed: bool = False, dense_comparison: bool = False
) -> Path:
    """Focused Gold/serving fixture; full F2 validation is covered by CI CLI."""
    sources = []
    with duckdb.connect() as db:
        analytical_silver(db)
        if dense_comparison:
            db.execute("""INSERT INTO silver SELECT
                'dense-'||d||'-'||h||'-'||j,
                timezone('America/New_York',d+h*INTERVAL '1 hour'),
                timezone('America/New_York',d+h*INTERVAL '1 hour')+INTERVAL '10 min',
                d+h*INTERVAL '1 hour',d+h*INTERVAL '1 hour'+INTERVAL '10 min',
                '001','002','classic_bike','member',40.7,-74.0,40.7,-74.0
                FROM (VALUES (TIMESTAMP '2025-01-05',1),
                    (TIMESTAMP '2025-01-12',2)) days(d,copies),
                range(24) hours(h),range(copies) duplicates(j)""")
        if changed:
            db.execute("""INSERT INTO silver SELECT * REPLACE('7' AS ride_id)
                FROM silver WHERE ride_id='3'""")
        db.execute(
            "ALTER TABLE silver ADD COLUMN disposition VARCHAR DEFAULT 'accepted'"
        )
        for name, condition, count in [
            (
                "changed_a" if changed else "a",
                "ride_id IN ('1','3','7')",
                3 if changed else 2,
            ),
            ("b", "ride_id NOT IN ('1','3','7')", 76 if dense_comparison else 4),
        ]:
            source = root / "fixture_silver" / name
            source.mkdir(parents=True, exist_ok=True)
            if not (source / "silver.parquet").exists():
                db.execute(
                    f"COPY (SELECT * FROM silver WHERE {condition}) "
                    "TO ? (FORMAT PARQUET)",
                    [str(source / "silver.parquet")],
                )
                write_json(
                    source / "quality.json",
                    {
                        "pipeline_version": PUBLICATION_VERSION,
                        "source_version": checksum(source / "silver.parquet"),
                        "period": "2024-10",
                        "data_mode": "controlled_fixture",
                        "dispositions": {"accepted": count},
                        "checksums": {
                            "silver.parquet": checksum(source / "silver.parquet")
                        },
                    },
                )
            sources.append(source)
    contributions = [prepare_contribution(source, root) for source in sources]
    # The fixture uses two source contributions so unchanged event months are
    # genuinely reused when a changed partition affects October/November only.
    parts = []
    for month in sorted(
        {m for contribution in contributions for m in checked(contribution)["months"]}
    ):
        path = prepare_month(month, contributions, root)
        parts.append({"path": str(path), **checked(path)})
    available = build_availability(root)
    identifier = identity([VERSION, [p["id"] for p in parts], available["id"]])
    target = root / "f3/publications" / identifier
    if target.exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    report = {
        "id": identifier,
        "publication_id": identifier,
        "pipeline_version": VERSION,
        "geo_mapping_version": GEO_VERSION,
        "source_snapshot_id": "0" * 64,
        "source": "controlled synthetic UTC fixture; not official data",
        "scope": "NYC",
        "timezone": "America/New_York",
        "data_mode": "controlled_fixture",
        "period_start": "2024-10-01T04:00:00+00:00",
        "period_end": "2025-04-01T04:00:00+00:00",
        "data_as_of": "2025-04-01T04:00:00+00:00",
        "data_as_of_kind": "controlled fixture timestamp",
        "accepted_silver_rows": (7 if changed else 6) + (72 if dense_comparison else 0),
        "months": parts,
        "availability": available,
        "quality_status": "warning",
        "checksums": {},
        "created_at": datetime.now(UTC).isoformat(),
    }
    write_json(target / "manifest.json", report)
    return target
