from pathlib import Path

import duckdb
import pytest

from citibike.ingestion.archive import ingest_fixture
from citibike.processing import history_publish
from citibike.processing.historical import prepare
from citibike.processing.schemas import detect
from citibike.processing.trips import process, verify_artifacts
from citibike.quality.history_report import station_join_counts
from citibike.storage import read_json, write_json
from tests.fixtures import ride, write_zip


def test_global_reconciliation_incremental_dependencies_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_rows = [
        ride(1, started_at="2026-01-31 23:30:00", ended_at="2026-02-01 00:30:00"),
        ride(2, started_at="2026-01-31 10:00:00", ended_at="2026-01-31 11:00:00"),
        ride(9, ride_id=""),
    ]
    first_rows.insert(2, first_rows[1].copy())
    second_rows = [
        first_rows[0],
        {**first_rows[1], "member_casual": "casual"},
        ride(3, started_at="2026-02-28 23:30:00", ended_at="2026-03-01 00:30:00"),
    ]
    a = ingest_fixture(
        write_zip(tmp_path / "a.zip", {"a.csv": first_rows}), "2026-01", tmp_path
    )
    b = ingest_fixture(
        write_zip(tmp_path / "b.zip", {"b.csv": second_rows}), "2026-02", tmp_path
    )
    states = [prepare(a, tmp_path), prepare(b, tmp_path)]
    assert prepare(a, tmp_path) == states[0]
    window = ["2026-01", "2026-02"]
    wrong_reference = tmp_path / "wrong-state.json"
    write_json(
        wrong_reference,
        {**read_json(states[1]), "result": read_json(states[0])["result"]},
    )
    with pytest.raises(ValueError, match="state lineage"):
        history_publish.publish_history(
            [states[0], wrong_reference], tmp_path, window, required_months=2
        )
    assert not (tmp_path / "f2/active.json").exists()
    published = history_publish.publish_history(
        states, tmp_path, window, required_months=2
    )
    report = read_json(published / "manifest.json")
    assert report["input_rows"] == 7 and report["canonical_rows"] == 2
    assert report["cross_source"]["changed_row_counts"] == {
        "conflicting_id": 3,
        "duplicate_exact": 1,
    }
    assert report["cross_source"]["independent_row_mismatches"] == 0
    assert "2026-02" in report["partitions"][0]["dependency_months"]
    for partition in report["partitions"]:
        with duckdb.connect() as db:
            assert db.execute(
                "SELECT count(*) FROM read_parquet(?)",
                [str(Path(partition["path"]) / "ledger.parquet")],
            ).fetchone() == (partition["input_rows"],)
            assert db.execute(
                """SELECT count(*) FROM read_parquet(?) WHERE
                quality_status IS NULL OR quality_status<>'quarantined'
                OR schema_version IS NULL OR source_version IS NULL
                OR processed_at_utc IS NULL""",
                [str(Path(partition["path"]) / "quarantine.parquet")],
            ).fetchone() == (0,)
    active = (tmp_path / "f2/active.json").read_bytes()
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT ride_id,member_casual_raw FROM read_parquet(?) "
            "WHERE source_file='a.csv' AND source_row_number=3",
            [str(Path(report["partitions"][0]["path"]) / "quarantine.parquet")],
        ).fetchone() == (first_rows[1]["ride_id"], first_rows[1]["member_casual"])
    assert (
        history_publish.publish_history(states, tmp_path, window, required_months=2)
        == published
    )
    assert (tmp_path / "f2/active.json").read_bytes() == active
    changed_rows = [
        ride(4, started_at="2026-02-02 10:00:00", ended_at="2026-02-02 11:00:00")
    ]
    changed = ingest_fixture(
        write_zip(tmp_path / "c.zip", {"c.csv": changed_rows}), "2026-02", tmp_path
    )
    revised_states = [states[0], prepare(changed, tmp_path)]

    def fail(*_args: object) -> None:
        raise RuntimeError("Injected crash before pointer activation")

    with monkeypatch.context() as patch:
        patch.setattr(history_publish, "activate_snapshot", fail)
        with pytest.raises(RuntimeError, match="Injected crash"):
            history_publish.publish_history(
                revised_states, tmp_path, window, required_months=2
            )
    assert (tmp_path / "f2/active.json").read_bytes() == active
    revised = history_publish.publish_history(
        revised_states, tmp_path, window, required_months=2
    )
    new = read_json(revised / "manifest.json")
    runs = [read_json(p) for p in sorted((tmp_path / "f2/runs").glob("*.json"))]
    assert runs[-1]["mode"] == "activate_existing"
    assert runs[-1]["rebuilt_partitions"] == 0
    assert new["canonical_rows"] == 3
    assert "2026-03" in new["affected_dependency_months"]
    # Unchanged January source changes because February's conflict was removed.
    assert (
        new["partitions"][0]["partition_id"] != report["partitions"][0]["partition_id"]
    )
    assert (
        new["partitions"][0]["source_object_id"]
        == report["partitions"][0]["source_object_id"]
    )


def test_unknown_schema_and_incomplete_scope_block(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown source schema"):
        detect(["tripduration", "starttime", "usertype"])
    with pytest.raises(ValueError, match="Unknown source schema"):
        detect(["ride_id", "unreviewed_extra_field"])
    with pytest.raises(ValueError, match="Complete selected"):
        history_publish.publish_history([], tmp_path, [], required_months=24)


def test_modern_categories_are_preserved_without_legacy_guessing(
    tmp_path: Path,
) -> None:
    archive = write_zip(
        tmp_path / "categories.zip",
        {
            "one.csv": [
                ride(
                    1,
                    member_casual="Subscriber",
                    rideable_type="new_vehicle",
                    started_at="2026-04-01 00:00:00.125",
                    ended_at="2026-04-01 00:00:00.375",
                )
            ]
        },
    )
    source = ingest_fixture(archive, "2026-04", tmp_path)
    result = process(source, tmp_path, pipeline_version="f2.0.0")
    quality = verify_artifacts(result)
    assert quality["raw_categories"]["member_casual"] == {"Subscriber": 1}
    assert quality["reason_counts"]["unknown_member_category"] == 1
    assert quality["reason_counts"]["unknown_rideable_category"] == 1
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT member_casual,duration_seconds FROM read_parquet(?)",
            [str(result / "silver.parquet")],
        ).fetchone() == ("Subscriber", 0.25)


def test_station_join_cardinality_and_no_historical_backfill() -> None:
    with duckdb.connect() as db:
        db.execute("""CREATE TABLE trips(start_station_id VARCHAR,
            started_at_utc TIMESTAMPTZ);
            INSERT INTO trips VALUES ('001','2026-10-28 10:00:00+00'),
                ('001','2026-04-01 10:00:00+00'),('unknown','2026-10-28 10:00:00+00');
            CREATE TABLE station_history(station_id VARCHAR,trip_station_id VARCHAR,
                valid_from TIMESTAMPTZ,valid_to TIMESTAMPTZ);
            INSERT INTO station_history VALUES ('A','001',
                '2026-10-28 10:00:00+00','2026-10-28 11:00:00+00')""")
        assert station_join_counts(db) == (3, 1, 0)
        db.execute("""INSERT INTO station_history VALUES ('B','001',
            '2026-10-28 10:00:00+00','2026-10-28 10:30:00+00')""")
        with pytest.raises(ValueError, match="join explosion"):
            station_join_counts(db)
