import json
import zipfile
from pathlib import Path

import duckdb
import pytest

from citibike.audit import audit_archive
from citibike.contracts import period_bounds
from citibike.ingestion.archive import download, ingest_fixture
from citibike.processing.trips import process, verify_artifacts
from citibike.serving.publish import publish
from tests.fixtures import fixture_archive, ride, write_zip


def test_all_files_reconciled_and_idempotent(tmp_path: Path) -> None:
    archive = fixture_archive(tmp_path / "fixture.zip")
    bronze = ingest_fixture(archive, "2026-04", tmp_path / "data")
    original_manifest = (bronze / "manifest.json").read_bytes()
    result = process(bronze, tmp_path / "data")
    report = verify_artifacts(result)
    assert report["input_rows"] == 10
    assert report["dispositions"] == {
        "accepted": 5,
        "duplicate_exact": 1,
        "conflicting_id": 2,
        "invalid": 2,
    }
    assert len(report["files"]) == 2
    assert sum(f["input_rows"] for f in report["files"]) == 10
    assert report["metrics"] == {
        "canonical_rides": 5,
        "known_start_station_rides": 4,
        "known_end_station_rides": 5,
        "completed_published_rides": 4,
    }
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(result / "row_ledger.parquet")]
        ).fetchone() == (10,)
        assert db.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(result / "quarantine.parquet")]
        ).fetchone() == (4,)
        assert db.execute(
            "SELECT DISTINCT start_station_id FROM read_parquet(?) "
            "WHERE start_station_id IS NOT NULL",
            [str(result / "silver.parquet")],
        ).fetchall() == [("001",)]
        # Arrivals on May 1 are retained, not incorrectly bucketed by trip start.
        assert db.execute(
            "SELECT sum(arrivals_s) FROM read_parquet(?) "
            "WHERE hour_utc >= TIMESTAMPTZ '2026-05-01 04:00:00+00'",
            [str(result / "station_hours.parquet")],
        ).fetchone() == (1,)
    assert process(bronze, tmp_path / "data") == result
    assert ingest_fixture(archive, "2026-04", tmp_path / "data") == bronze
    assert (bronze / "manifest.json").read_bytes() == original_manifest
    assert verify_artifacts(result) == report


def test_dst_ambiguity_gap_and_normalization(tmp_path: Path) -> None:
    archive = write_zip(
        tmp_path / "dst.zip",
        {
            "dst.csv": [
                ride(
                    1, started_at="2025-11-02 00:30:00", ended_at="2025-11-02 02:30:00"
                ),
                ride(
                    2, started_at="2025-11-02 01:30:00", ended_at="2025-11-02 02:30:00"
                ),
                ride(
                    3, started_at="2025-03-09 02:30:00", ended_at="2025-03-09 03:30:00"
                ),
            ]
        },
    )
    result = process(ingest_fixture(archive, "2025-11", tmp_path), tmp_path)
    report = verify_artifacts(result)
    assert report["dispositions"] == {"accepted": 1, "invalid": 2}
    assert report["reason_counts"]["ambiguous_start_time"] == 1
    assert report["reason_counts"]["nonexistent_start_time"] == 1
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT duration_seconds FROM read_parquet(?)",
            [str(result / "silver.parquet")],
        ).fetchone() == (10800.0,)
    start, end = period_bounds("2025-11")
    assert (end - start).total_seconds() == (30 * 24 + 1) * 3600
    start, end = period_bounds("2025-03")
    assert (end - start).total_seconds() == (31 * 24 - 1) * 3600


def test_empty_corrupt_and_unknown_schema_block(tmp_path: Path) -> None:
    empty = write_zip(tmp_path / "empty.zip", {"empty.csv": []})
    with pytest.raises(ValueError, match="Empty source"):
        process(ingest_fixture(empty, "2026-04", tmp_path), tmp_path)
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        ingest_fixture(bad, "2026-04", tmp_path)
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("x.csv", "unexpected\nvalue\n")
    with pytest.raises(ValueError, match="Unsupported source schema"):
        process(ingest_fixture(bad, "2026-04", tmp_path), tmp_path)


def test_changed_bronze_and_artifacts_rejected(tmp_path: Path) -> None:
    bronze = ingest_fixture(
        fixture_archive(tmp_path / "source.zip"), "2026-04", tmp_path
    )
    result = process(bronze, tmp_path)
    (result / "silver.parquet").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifact checksum"):
        process(bronze, tmp_path)
    (bronze / "source.zip").write_bytes(b"changed")
    with pytest.raises(ValueError, match="Bronze checksum"):
        process(bronze, tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "https://s3.amazonaws.com/tripdata/JC-202604-citibike-tripdata.csv.zip",
        "https://example.com/202604-citibike-tripdata.zip",
    ],
)
def test_source_scope_is_not_quality_error(url: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="official NYC"):
        download(url, "2026-04", tmp_path)


def test_profile_preserves_null_and_does_not_drop_duration_peaks(
    tmp_path: Path,
) -> None:
    archive = write_zip(
        tmp_path / "peaks.zip",
        {
            "one.csv": [
                ride(1, start_station_id="", ended_at="2026-04-03 00:00:00"),
            ]
        },
    )
    result = process(ingest_fixture(archive, "2026-04", tmp_path), tmp_path)
    report = json.loads((result / "quality.json").read_text(encoding="utf-8"))
    assert report["metrics"]["completed_published_rides"] == 1
    assert report["metrics"]["known_start_station_rides"] == 0
    assert report["reason_counts"]["long_duration"] == 1


def test_independent_csv_baseline(tmp_path: Path) -> None:
    bronze = ingest_fixture(
        fixture_archive(tmp_path / "source.zip"), "2026-04", tmp_path
    )
    audit = audit_archive(bronze, tmp_path / "audit")
    assert audit["input_rows"] == 10
    assert audit["canonical_rows_all"] == 5
    assert audit["completed_published_rides"] == 4
    assert audit["dispositions"] == {
        "accepted": 5,
        "duplicate_exact": 1,
        "conflicting_id": 2,
        "invalid": 2,
    }


def test_all_invalid_retains_quarantine_without_publication(tmp_path: Path) -> None:
    source = write_zip(tmp_path / "invalid.zip", {"one.csv": [ride(1, ride_id="")]})
    with pytest.raises(ValueError, match="quarantine retained"):
        process(ingest_fixture(source, "2026-04", tmp_path), tmp_path)
    quarantine = list((tmp_path / "failed").glob("*/quarantine.parquet"))
    assert len(quarantine) == 1
    with duckdb.connect() as db:
        assert db.execute(
            "SELECT count(*) FROM read_parquet(?)", [str(quarantine[0])]
        ).fetchone() == (1,)
    assert not list((tmp_path / "processed").glob("*/quality.json"))


def test_wrong_source_period_cannot_be_published(tmp_path: Path) -> None:
    source = write_zip(tmp_path / "wrong-month.zip", {"one.csv": [ride(1)]})
    result = process(ingest_fixture(source, "2026-03", tmp_path), tmp_path)
    with pytest.raises(ValueError, match="inside source period"):
        publish(result)


def test_utf8_source_names_and_provenance(tmp_path: Path) -> None:
    name = "данные/поездки.csv"
    archive = write_zip(tmp_path / "utf8.zip", {name: [ride(1)]})
    bronze = ingest_fixture(archive, "2026-04", tmp_path)
    result = process(bronze, tmp_path)
    report = verify_artifacts(result)
    assert report["source"]["source"] == (
        "Controlled test fixture — not real Citi Bike data"
    )
    assert report["files"][0]["file"] == name
    assert report["metrics"]["completed_published_rides"] == 1
