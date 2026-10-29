from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from citibike.serving import analytics_publish as publishing
from citibike.serving.analytics_models import AnalyticsFilters, AvailabilityFilters
from citibike.serving.analytics_publish import activate, publish
from citibike.serving.analytics_read import availability, read
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import read_json
from tests.fixtures_analytics import analytical_gold

pytest_plugins = ("tests.integration.test_serving",)


def test_comparable_period_change_and_dst_rejection(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    publish(analytical_gold(tmp_path, dense_comparison=True), database)
    filters = AnalyticsFilters(
        start_date=date(2025, 1, 12),
        end_date=date(2025, 1, 13),
        compare_start=date(2025, 1, 5),
        compare_end=date(2025, 1, 6),
    )
    response = read("compare", filters, database)
    assert response.comparison.comparable is True
    assert response.comparison.previous_rides == 24
    assert response.comparison.relative_change == 1.0
    dst = read(
        "compare",
        filters.model_copy(
            update={
                "start_date": date(2024, 11, 3),
                "end_date": date(2024, 11, 4),
                "compare_start": date(2024, 10, 27),
                "compare_end": date(2024, 10, 28),
            }
        ),
        database,
    )
    assert dst.comparison.comparable is False
    assert dst.comparison.relative_change is None
    zero = read(
        "compare",
        filters.model_copy(
            update={
                "compare_start": date(2025, 1, 19),
                "compare_end": date(2025, 1, 20),
            }
        ),
        database,
    )
    assert zero.comparison.relative_change is None


def test_analytics_postgres_http_filters_metrics_and_rollback(
    database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert read("kpi", AnalyticsFilters(), database).status == "empty"
    gold = analytical_gold(tmp_path)
    version = publish(gold, database)
    filters = AnalyticsFilters(start_date=date(2024, 10, 1), end_date=date(2024, 11, 1))
    result = read("kpi", filters, database)
    assert result.metrics is not None
    assert result.metrics.completed_published_rides == 2
    assert result.metrics.duration_count == 2
    assert result.metrics.average_duration_seconds == 210
    assert result.metrics.departures == 1 and result.metrics.arrivals == 1
    assert result.metrics.unknown_start_station == 1
    assert result.metrics.unknown_end_station == 1
    assert result.metrics.route_coverage == 0.5
    peaks = read("peaks", filters, database)
    assert peaks.coverage["peak_grain"] == "day"
    assert {r.dimension: r.n for r in peaks.rows} == {"departures": 1, "arrivals": 1}
    assert {r.key for r in peaks.rows} == {"2024-10-01"}
    hourly = read("timeseries", filters.model_copy(update={"grain": "hour"}), database)
    assert len(hourly.rows) == 744
    assert any(r.metrics is None for r in hourly.rows)
    categories = read("categories", filters, database)
    assert {
        (r.category, r.known_share) for r in categories.rows if r.dimension == "vehicle"
    } == {("classic_bike", 1.0), ("unknown", None)}
    assert read("routes", filters, database).total_groups == 1
    no_destination = read(
        "routes", filters.model_copy(update={"to_station": "missing"}), database
    )
    assert no_destination.status == "empty"
    assert no_destination.metrics is not None
    assert no_destination.metrics.completed_published_rides == 0
    assert no_destination.coverage["population_denominator"] == 2
    assert (
        read("routes", filters.model_copy(update={"offset": 10}), database).total_groups
        == 1
    )
    assert read("geography", filters, database).coverage["eligible_coverage"] == 0.5
    arrivals = read("kpi", filters.model_copy(update={"station_id": "001"}), database)
    assert arrivals.metrics is not None and arrivals.metrics.arrivals == 0
    with TestClient(create_app()) as client:
        for endpoint in (
            "kpi",
            "timeseries",
            "stations",
            "routes",
            "categories",
            "geography",
            "peaks",
        ):
            response = client.get(
                f"/v2/analytics/{endpoint}",
                params={"start_date": "2024-10-01", "end_date": "2024-11-01"},
            )
            assert response.status_code == 200, (endpoint, response.text)
            assert response.json()["publication"]["publication_id"] == version
        assert client.get("/health").json() == {"status": "ok"}
        for params in (
            {"start_date": "2024-10-01"},
            {"vehicle": "cargo"},
            {"grain": "year"},
            {"limit": 101},
            {"scope": "JC"},
            {"publication_id": "f" * 64},
            {"to_station": "001"},
        ):
            assert client.get("/v2/analytics/kpi", params=params).status_code == 422
        assert (
            client.get("/v2/analytics/timeseries", params={"grain": "hour"}).status_code
            == 422
        )
        assert (
            client.get("/v2/analytics/kpi", params={"station_id": "' OR 1=1--"}).json()[
                "status"
            ]
            == "empty"
        )
        assert (
            client.get(
                "/v2/analytics/availability", params={"vehicle": "classic_bike"}
            ).status_code
            == 422
        )
        assert client.get("/v1/kpi").json()["status"] == "empty"
    assert publish(gold, database) == version
    changed = analytical_gold(tmp_path, changed=True)
    changed_report = read_json(changed / "manifest.json")
    october = next(p for p in changed_report["months"] if p["month"] == "2024-10")
    parquet = Path(october["path"]) / "trip_hourly.parquet"
    original_bytes = parquet.read_bytes()
    try:
        parquet.write_bytes(original_bytes + b"corruption")
        with pytest.raises(ValueError, match="checksum mismatch"):
            publish(changed, database)
    finally:
        parquet.write_bytes(original_bytes)
    original_copy = publishing._copy_partition

    def failed_copy(*args: Any, **kwargs: Any) -> None:
        original_copy(*args, **kwargs)
        raise RuntimeError("Injected COPY transaction failure")

    with monkeypatch.context() as patch:
        patch.setattr(publishing, "_copy_partition", failed_copy)
        with pytest.raises(RuntimeError, match="COPY transaction failure"):
            publish(changed, database)
    with connect(database) as db:
        assert (
            db.execute(
                "SELECT 1 FROM analytics_partitions WHERE partition_id=%s",
                (october["id"],),
            ).fetchone()
            is None
        )
    with pytest.raises(RuntimeError, match="Injected failure"):
        publish(changed, database, fail_before_activate=True)
    retained = read("kpi", AnalyticsFilters(), database).publication
    assert retained is not None and retained.publication_id == version
    newer = publish(changed, database)
    assert newer != version
    current = read("kpi", filters, database).metrics
    assert current is not None and current.completed_published_rides == 3
    pinned = read(
        "kpi", filters.model_copy(update={"publication_id": version}), database
    ).metrics
    assert pinned is not None and pinned.completed_published_rides == 2
    with connect(database) as db:
        activate(db, version)
    restored = read("kpi", filters, database).metrics
    assert restored is not None and restored.completed_published_rides == 2
    assert read_json(gold / "manifest.json")["months"]
    with connect(database) as db:
        db.execute(
            "UPDATE schema_migrations SET checksum='changed' WHERE version='002'"
        )
    with pytest.raises(ValueError, match="migration checksum"):
        publish(gold, database)


def test_availability_weighting_and_gaps(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    gold = analytical_gold(tmp_path)
    version = publish(gold, database)
    start = datetime(2024, 10, 1, tzinfo=UTC)
    with connect(database) as db:
        row = db.execute(
            "SELECT manifest FROM analytics_publications WHERE publication_id=%s",
            (version,),
        ).fetchone()
        assert row is not None
        manifest = row["manifest"]
        manifest["availability"].update(
            {
                "period_start": start.isoformat(),
                "period_end": (start + timedelta(seconds=120)).isoformat(),
                "expected_stations": 1,
            }
        )
        from psycopg.types.json import Jsonb

        db.execute(
            "UPDATE analytics_publications SET manifest=%s WHERE publication_id=%s",
            (Jsonb(manifest), version),
        )
        for offset, bikes, docks in [(0, 0, 5), (60, 4, 0)]:
            a = start + timedelta(seconds=offset)
            db.execute(
                "INSERT INTO analytics_availability "
                "VALUES(%s,'uuid',NULL,%s,%s,%s,%s,%s,%s)",
                (
                    version,
                    a,
                    a + timedelta(seconds=55 if offset == 0 else 60),
                    bikes,
                    docks,
                    str(offset),
                    a,
                ),
            )
    result = availability(AvailabilityFilters(), database)
    assert result.status == "stale"
    assert result.availability.observed_coverage == pytest.approx(115 / 120)
    assert result.availability.rentable_bikes_time_weighted == pytest.approx(240 / 115)
    assert result.availability.empty_station_time_share == pytest.approx(55 / 115)
    assert result.availability.full_station_time_share == pytest.approx(60 / 115)
    assert result.availability.active_stations == 1
    gap = availability(
        AvailabilityFilters(start=start, end=start + timedelta(seconds=300)), database
    )
    assert gap.status == "unavailable"
    assert gap.availability.observed_coverage == pytest.approx(115 / 300)
    assert gap.availability.empty_station_time_share is None
    with connect(database) as db:
        db.execute(
            "UPDATE analytics_availability SET "
            "rentable_bikes=NULL,returnable_docks=NULL"
        )
    stopped = availability(AvailabilityFilters(), database)
    assert stopped.availability.observed_coverage == pytest.approx(115 / 120)
    assert stopped.availability.rentable_bikes_time_weighted is None
    assert stopped.availability.active_stations == 0
    assert stopped.availability.empty_station_time_share is None
    assert stopped.message is not None
    with connect(database) as db:
        db.execute("""UPDATE analytics_availability SET
            valid_to=valid_from+INTERVAL '60 seconds',
            rentable_bikes=CASE WHEN collection_id='60' THEN 0 END,
            returnable_docks=CASE WHEN collection_id='60' THEN 5 END""")
    half_operating = availability(AvailabilityFilters(), database)
    assert half_operating.availability.observed_coverage == 1
    assert half_operating.availability.rent_observed_station_seconds == 60
    assert half_operating.availability.rent_target_station_seconds == 60
    assert half_operating.availability.rent_observed_coverage == 1
    assert half_operating.availability.empty_station_time_share == 1
    assert half_operating.availability.full_station_time_share == 0
