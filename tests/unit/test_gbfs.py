import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import httpx
import pytest

from citibike.ingestion.gbfs import (
    GBFS_PIPELINE,
    collect,
    envelope,
    feed_urls,
    normalize,
    rebuild_station_history,
)
from citibike.storage import read_json, write_json


def feeds(
    version: str = "2.3",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    now = datetime.now(UTC)
    value = int(now.timestamp()) if version == "2.3" else now.isoformat()
    base = {"version": version, "ttl": 60, "last_updated": value}
    urls = [
        {"name": name, "url": f"https://gbfs.lyft.com/test/{name}.json"}
        for name in ["station_information", "station_status"]
    ]
    discovery = {
        **base,
        "data": {"en": {"feeds": urls}} if version == "2.3" else {"feeds": urls},
    }
    info = {
        **base,
        "data": {
            "stations": [
                {
                    "station_id": "uuid-1",
                    "short_name": "001",
                    "name": "Confirmed station",
                    "lat": 40.7,
                    "lon": -74,
                    "capacity": 10,
                }
            ]
        },
    }
    flag = 1 if version == "2.3" else True
    status = {
        **base,
        "data": {
            "stations": [
                {
                    "station_id": "uuid-1",
                    "last_reported": value,
                    "num_bikes_available"
                    if version == "2.3"
                    else "num_vehicles_available": 0,
                    "num_docks_available": 10,
                    "is_renting": flag,
                    "is_returning": flag,
                    "is_installed": flag,
                }
            ]
        },
    }
    return discovery, info, status


def metadata(feed: dict[str, Any], received: datetime | None = None) -> dict[str, Any]:
    return {**envelope(feed, received or datetime.now(UTC)), "checksum": "a" * 64}


@pytest.mark.parametrize("version", ["2.3", "3.0"])
def test_versions_zero_unknown_stale_and_invalid_counts(version: str) -> None:
    discovery, info, status = feeds(version)
    assert len(feed_urls(discovery)) == 2
    rows, bad, observed = normalize(
        info, status, metadata(info), metadata(status), "test"
    )
    assert not bad and rows[0]["rentable_bikes"] == 0
    assert rows[0]["trip_station_id"] == "001" and len(observed) == 1
    delayed = metadata(status, datetime.now(UTC) + timedelta(seconds=61))
    stale, _, _ = normalize(info, status, metadata(info), delayed, "delayed")
    assert stale[0]["quality_status"] == "stale" and stale[0]["rentable_bikes"] is None
    missing = copy.deepcopy(status)
    missing["data"]["stations"][0].pop("num_docks_available")
    rows, _, _ = normalize(info, missing, metadata(info), metadata(missing), "missing")
    assert rows[0]["docks_available"] is None and rows[0]["returnable_docks"] is None
    invalid = copy.deepcopy(status)
    invalid["data"]["stations"][0]["num_docks_available"] = -1
    with pytest.raises(ValueError, match="All GBFS"):
        normalize(info, invalid, metadata(info), metadata(invalid), "invalid")


def test_missing_mismatch_duplicate_and_unknown_version() -> None:
    _, info, status = feeds()
    status["data"]["stations"][0]["station_id"] = "other"
    rows, _, _ = normalize(info, status, metadata(info), metadata(status), "mismatch")
    assert len(rows) == 2 and {r["quality_status"] for r in rows} == {
        "missing",
        "warning",
    }
    info["data"]["stations"].append(info["data"]["stations"][0])
    with pytest.raises(ValueError, match="duplicate station"):
        normalize(info, status, metadata(info), metadata(status), "duplicate")
    with pytest.raises(ValueError, match="Unsupported"):
        envelope({"version": "9.9", "ttl": 60, "last_updated": 1}, datetime.now(UTC))


def test_zero_ttl_means_refresh_always_not_automatically_stale() -> None:
    _, info, status = feeds("3.0")
    info["ttl"] = status["ttl"] = 0
    info_meta, status_meta = metadata(info), metadata(status)
    assert not info_meta["stale"] and not status_meta["stale"]
    rows, _, _ = normalize(info, status, info_meta, status_meta, "instant")
    assert rows[0]["rentable_bikes"] == 0
    late = metadata(status, datetime.now(UTC) + timedelta(seconds=301))
    assert late["stale"]


def test_unknown_timestamp_does_not_become_zero_availability() -> None:
    _, info, status = feeds()
    status["data"]["stations"][0].pop("last_reported")
    rows, _, _ = normalize(info, status, metadata(info), metadata(status), "unknown")
    assert rows[0]["quality_status"] == "unknown"
    assert rows[0]["bikes_available"] == 0
    assert rows[0]["rentable_bikes"] is None
    assert "unknown_last_reported" in rows[0]["quality_reason_codes"]


def test_real_collection_protocol_history_and_failure_rollback(tmp_path: Path) -> None:
    discovery, info, status = feeds()
    replies = {"gbfs": discovery, "station_information": info, "station_status": status}
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json=replies[name])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = collect(tmp_path, "first", client)
        assert collect(tmp_path, "first", client) == first and len(calls) == 3
        # A crash may leave verified Silver but no active pointers.
        (tmp_path / "gbfs/active.json").unlink()
        (tmp_path / "gbfs/station_history/active.json").unlink()
        assert collect(tmp_path, "first", client) == first and len(calls) == 3
        assert (tmp_path / "gbfs/active.json").exists()
        assert (tmp_path / "gbfs/station_history/active.json").exists()
        active_before = read_json(tmp_path / "gbfs/station_history/active.json")
        info["data"]["stations"][0]["capacity"] = 12
        collect(tmp_path, "changed", client)
        history = read_json(tmp_path / "gbfs/station_history/active.json")
        assert history["version"] != active_before["version"]
        with duckdb.connect() as db:
            intervals = db.execute(
                "SELECT valid_from,valid_to,capacity FROM read_parquet(?) "
                "ORDER BY valid_from",
                [str(Path(history["path"]) / "dim_station_history.parquet")],
            ).fetchall()
            assert len(intervals) == 2 and intervals[0][1] <= intervals[1][0]
            assert [r[2] for r in intervals] == [10, 12]
            assert all(r[0] > datetime(2026, 4, 1, tzinfo=UTC) for r in intervals)
        prior = (tmp_path / "gbfs/active.json").read_bytes()
        status["data"]["stations"][0]["num_bikes_available"] = -1
        with pytest.raises(ValueError, match="All GBFS"):
            collect(tmp_path, "invalid", client)
        assert (tmp_path / "gbfs/active.json").read_bytes() == prior
        assert (tmp_path / "gbfs/bronze/invalid/station_status.json").exists()


def test_station_history_gaps_alias_reuse_and_conflicting_observations(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 10, 28, tzinfo=UTC)

    def observation(station: str, offset: int, capacity: int = 10) -> dict[str, Any]:
        at = start + timedelta(seconds=offset)
        return {
            "station_id": station,
            "trip_station_id": "001",
            "name": station,
            "lat": 40.7,
            "lon": -74.0,
            "capacity": capacity,
            "observed_at": at.isoformat(),
            "valid_until": (at + timedelta(seconds=60)).isoformat(),
            "source_checksum": "a" * 64,
            "collection_id": str(offset),
        }

    directory = tmp_path / "gbfs/silver" / GBFS_PIPELINE
    write_json(
        directory / "first/station_observations.json",
        [
            observation("A", 0),
            observation("A", 30),
            observation("A", 180),
            observation("B", 200),
        ],
    )
    history = rebuild_station_history(tmp_path)
    rows = json.loads((history / "intervals.json").read_text(encoding="utf-8"))
    assert len(rows) == 3
    assert rows[0]["valid_to"] == (start + timedelta(seconds=90)).isoformat()
    assert rows[1]["valid_from"] == (start + timedelta(seconds=180)).isoformat()
    assert rows[1]["valid_to"] == rows[2]["valid_from"]
    previous = (tmp_path / "gbfs/station_history/active.json").read_bytes()
    write_json(
        directory / "conflict/station_observations.json",
        [
            observation("A", 0, capacity=11),
        ],
    )
    with pytest.raises(ValueError, match="same timestamp"):
        rebuild_station_history(tmp_path)
    assert (tmp_path / "gbfs/station_history/active.json").read_bytes() == previous
