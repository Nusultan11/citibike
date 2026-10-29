from pathlib import Path

import duckdb
import pytest

from citibike.ingestion.archive import ingest_fixture
from citibike.processing.trips import process, verify_artifacts
from citibike.quality.profiling import research
from citibike.storage import write_json
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


def test_research_effective_silver_counts_geography_churn_and_oracle(
    tmp_path: Path,
) -> None:
    rows = [
        ride(1, start_station_id="001", end_station_id="001"),
        ride(2, start_station_id="001", end_station_id="001"),
        ride(
            3,
            start_station_id="002",
            end_station_id="",
            started_at="2026-04-01 02:00:00",
            ended_at="2026-04-01 02:10:00",
        ),
        ride(
            4,
            start_station_id="001",
            end_station_id="001",
            started_at="2026-04-01 03:00:00",
            ended_at="2026-04-01 03:10:00",
        ),
    ]
    result = process(
        ingest_fixture(
            write_zip(tmp_path / "research.zip", {"one.csv": rows}), "2026-04", tmp_path
        ),
        tmp_path,
    )
    snapshot = tmp_path / "snapshot"
    manifest = {
        "window": ["2026-04"],
        "partitions": [{"path": str(result)}],
        "canonical_rows": 4,
    }
    write_json(snapshot / "manifest.json", manifest)
    r = research(snapshot, tmp_path / "spill")
    assert r["lineage"]["rides"] == 4
    assert r["station_universe"] == 2
    assert r["churn"][0]["first_seen_ids"] == 2
    assert r["coordinate_churn"]["multi_cell_ids"] == 0
    assert (
        sum(x["n"] for x in r["geography"] if x["prediction_type"] == "departures") == 4
    )
    assert r["independent_mismatches"] == 0
    assert r["label_oracle"]["mismatches"] == 0
    dep = next(x for x in r["monthly"] if x["prediction_type"] == "departures")
    assert dep["events"] == 4 and dep["eligible_hours"] == 4
    assert dep["zero_hours"] == 1 and dep["single_event_hours"] == 2
    assert dep["mean"] == 1
    assert r["shifts"] == []
    write_json(snapshot / "manifest.json", {**manifest, "canonical_rows": 5})
    with pytest.raises(ValueError, match="lineage/grain"):
        research(snapshot, tmp_path / "spill")


def test_research_shift_is_normalised_and_reproducible(tmp_path: Path) -> None:
    parts = []
    for month, station, volume in [("2026-04", "001", 2), ("2026-05", "002", 1)]:
        archive = write_zip(
            tmp_path / f"{month}.zip",
            {
                "one.csv": [
                    ride(
                        100 * int(month[-2:]) + i,
                        start_station_id=station,
                        end_station_id=station,
                        started_at=month + "-01 00:00:00",
                        ended_at=month + "-01 00:10:00",
                    )
                    for i in range(volume)
                ]
            },
        )
        path = process(ingest_fixture(archive, month, tmp_path), tmp_path)
        parts.append({"path": str(path)})
    snapshot = tmp_path / "snapshot"
    write_json(
        snapshot / "manifest.json",
        {"window": ["2026-04", "2026-05"], "partitions": parts, "canonical_rows": 3},
    )
    first = research(snapshot, tmp_path / "spill")
    second = research(snapshot, tmp_path / "spill")
    for field in (
        "monthly",
        "weekly",
        "churn",
        "histograms",
        "shifts",
        "geography",
        "label_oracle",
    ):
        assert first[field] == second[field]
    assert first["stable_24_month_ids"] == 0
    assert first["churn"][1]["entered_vs_previous"] == 1
    assert first["churn"][1]["absent_vs_previous"] == 1
    assert first["churn"][1]["jaccard_vs_previous"] == 0
    assert {r["total_variation"] for r in first["shifts"]} == {1.0}
