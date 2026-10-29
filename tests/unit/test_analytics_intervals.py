from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from citibike.analytics.availability import intervals
from citibike.analytics.build import publish_directory
from citibike.serving.analytics_models import AnalyticsFilters, AvailabilityFilters


def collection(
    start: datetime, *, ttl: int = 60, status: str = "valid"
) -> dict[str, Any]:
    feed = {"last_updated": start.isoformat(), "ttl": ttl}
    return {
        "id": start.isoformat(),
        "received": start.isoformat(),
        "feeds": {"station_information": feed, "station_status": feed},
        "rows": [
            {
                "station_id": "uuid",
                "trip_station_id": "001",
                "quality_status": status,
                "last_reported": start.isoformat(),
                "ttl": ttl,
                "rentable_bikes": 0,
                "returnable_docks": 5,
            }
        ],
    }


def test_gbfs_ttl_next_receipt_gaps_disabled_zero_and_stale() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows = intervals(
        [
            collection(start),
            collection(start + timedelta(seconds=15)),
            collection(start + timedelta(seconds=180)),
            collection(start + timedelta(seconds=250), status="stale"),
            collection(start + timedelta(seconds=300), ttl=0),
        ]
    )
    assert len(rows) == 3
    assert [
        (
            datetime.fromisoformat(r["valid_to"])
            - datetime.fromisoformat(r["valid_from"])
        ).total_seconds()
        for r in rows
    ] == [15, 60, 60]
    assert all(r["rentable_bikes"] == 0 for r in rows)
    disabled = collection(start)
    disabled["rows"][0]["rentable_bikes"] = None
    assert intervals([disabled])[0]["returnable_docks"] == 5
    disabled["rows"][0]["returnable_docks"] = None
    stopped = intervals([disabled])
    assert len(stopped) == 1
    assert stopped[0]["rentable_bikes"] is None
    assert stopped[0]["returnable_docks"] is None


def test_invalid_analytics_filters_and_aware_gbfs_instants() -> None:
    for values in (
        {"start_date": "2024-01-01"},
        {"limit": 101},
        {"vehicle": "cargo"},
        {"publication_id": "F1"},
        {"start_date": "2024-01-02", "end_date": "2024-01-01"},
    ):
        with pytest.raises(ValueError):
            AnalyticsFilters.model_validate(values)
    with pytest.raises(ValueError):
        AvailabilityFilters.model_validate(
            {"start": "2024-01-01T00:00:00", "end": "2024-01-02T00:00:00"}
        )


def test_transient_windows_atomic_rename_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir()
    target = tmp_path / "published"
    real = Path.rename
    attempts = 0

    def transient(path: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("simulated scanner handle")
        return real(path, destination)

    monkeypatch.setattr(Path, "rename", transient)
    monkeypatch.setattr("citibike.analytics.build.time.sleep", lambda _: None)
    publish_directory(stage, target)
    assert target.is_dir() and not stage.exists() and attempts == 3
