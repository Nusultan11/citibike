from pathlib import Path

import duckdb

from citibike.ingestion.archive import ingest_fixture
from citibike.processing.trips import process, verify_artifacts
from tests.fixtures import ride, write_zip


def test_silver_types_raw_times_nullable_coordinates_and_lineage(
    tmp_path: Path,
) -> None:
    archive = write_zip(
        tmp_path / "typed.zip",
        {"one.csv": [ride(1, start_lat="bad", end_station_id="")]},
    )
    result = process(ingest_fixture(archive, "2026-04", tmp_path), tmp_path)
    with duckdb.connect() as db:
        schema = dict(
            (r[0], r[1])
            for r in db.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)",
                [str(result / "silver.parquet")],
            ).fetchall()
        )
        assert schema["started_at_utc"] == "TIMESTAMP WITH TIME ZONE"
        assert schema["start_station_id"] == "VARCHAR"
        assert schema["start_lat"] == "DOUBLE"
        row = db.execute(
            "SELECT started_at_raw,start_lat_raw,start_lat,end_station_id,"
            "source_row_number,schema_version,quality_reason_codes "
            "FROM read_parquet(?)",
            [str(result / "silver.parquet")],
        ).fetchone()
        assert row is not None
        assert row[:6] == (
            "2026-04-01 00:00:00",
            "bad",
            None,
            None,
            1,
            "citibike-modern-v1",
        )
        assert "invalid_coordinates" in row[6] and "missing_end_station" in row[6]
        gold = db.execute(
            "SELECT source_object_id,pipeline_version,silver_partition "
            "FROM read_parquet(?)",
            [str(result / "ride_hours.parquet")],
        ).fetchone()
        assert (
            gold is not None
            and gold[1] == "f1.0.0"
            and gold[2].endswith("/silver.parquet")
        )
    assert verify_artifacts(result)["metrics"]["completed_published_rides"] == 1
