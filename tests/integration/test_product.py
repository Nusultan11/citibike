import shutil
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import create_app
from citibike.analytics.station_geo import build, checked, verify
from citibike.forecasting.artifacts import checked as f4_checked
from citibike.risk import Config
from citibike.risk.inputs import load
from citibike.risk.run import execute
from citibike.serving.analytics_publish import publish as publish_analytics
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.serving.forecast_publish import publish as publish_forecast
from citibike.serving.product_publish import publish_geo, publish_risk, risk_records
from citibike.serving.risk_api import RiskFilters
from citibike.serving.risk_api import read as read_risk
from citibike.serving.station_api import StationFilters
from citibike.serving.station_api import read as read_stations
from citibike.storage import read_json, write_json
from tests.fixtures_analytics import analytical_gold
from tests.integration.test_risk import controlled_gbfs

pytest_plugins = ("tests.integration.test_serving",)
PERIOD = {"start_date": "2024-10-01", "end_date": "2025-04-01"}


def extra_geo(
    database: DatabaseSettings, geo: str, rows: list[tuple[str, float | None]]
) -> None:
    with connect(database) as db:
        for station, lat in rows:
            db.execute(
                "INSERT INTO station_geo_monthly VALUES(%s,%s,'2024-10-01','start',"
                "%s,%s,NULL,1,%s,%s,%s)",
                (
                    geo,
                    station,
                    lat,
                    None if lat is None else -74.0,
                    0 if lat is None else 1,
                    0 if lat is None else 1,
                    0 if lat is None else 1,
                ),
            )


def test_station_geo_publication_api_parity_and_states(
    database: DatabaseSettings, tmp_path: Path
) -> None:
    gold = analytical_gold(tmp_path)
    publication = publish_analytics(gold, database)
    client = TestClient(create_app())
    before = client.get("/v4/stations", params=PERIOD).json()
    # Coordinates not yet published: explicit unavailable, metrics still exact.
    assert before["geo"] is None
    assert {r["coordinate_status"] for r in before["rows"]} == {"unavailable"}
    path = build(tmp_path, gold)
    report = checked(path)
    assert report["groups_without_coordinates"] >= 1  # 001 start with null coords
    assert verify(path)["status"] == "PASS"
    assert build(tmp_path, gold) == path  # immutable, content-addressed repeat
    geo = publish_geo(path, database)
    assert publish_geo(path, database) == geo
    extra_geo(database, geo, [("003", None), ("5980.1", 40.73), ("5980.10", 40.73)])
    response = client.get("/v4/stations", params=PERIOD)
    assert response.status_code == 200
    body = response.json()
    assert body["publication"]["publication_id"] == publication
    assert body["geo"]["geo_id"] == geo
    rows = {r["station_id"]: r for r in body["rows"]}
    f3 = client.get("/v2/analytics/stations", params={**PERIOD, "limit": 100}).json()
    for row in f3["rows"]:
        mine = rows[row["station_id"]]
        assert mine["departures"] == row["metrics"]["departures"]
        assert mine["arrivals"] == row["metrics"]["arrivals"]
        assert mine["net_flow"] == row["metrics"]["net_flow"]
    assert rows["001"]["coordinate_status"] == "ok"
    assert rows["001"]["coordinate_changes"] >= 1  # moved inside the period
    assert rows["003"]["coordinate_status"] == "missing"
    assert rows["003"]["lat"] is None and rows["003"]["departures"] is None
    assert rows["5980.1"]["split_partner"] == "5980.10"
    assert rows["5980.10"]["split_partner"] == "5980.1"
    assert body["coverage"]["stations_without_coordinates"] == 1
    assert body["coverage"]["suspected_id_split_stations"] == 2
    search = client.get("/v4/stations", params={**PERIOD, "q": "598"}).json()
    assert [r["station_id"] for r in search["rows"]] == ["5980.1", "5980.10"]
    detail = client.get("/v4/stations/001", params=PERIOD).json()
    assert [r["station_id"] for r in detail["rows"]] == ["001"]
    assert detail["history"] and detail["gbfs"]["status"] in {
        "fresh",
        "stale",
        "unavailable",
    }
    assert detail["gbfs"]["status"] != "fresh" or detail["gbfs"]["rentable_bikes"]
    missing = client.get("/v4/stations/nope", params=PERIOD).json()
    assert missing["status"] == "empty" and missing["rows"] == []
    for bad in (
        {"start_date": "2024-10-02", "end_date": "2024-10-01"},
        {"start_date": "2024-10-02"},
        {"vehicle": "scooter"},
        {"start_date": "2023-01-01", "end_date": "2023-02-01"},
    ):
        assert client.get("/v4/stations", params=bad).status_code == 422
    broken = DatabaseSettings(PGPORT=1)  # type: ignore[call-arg]
    with pytest.raises(DatabaseUnavailable):
        read_stations(StationFilters(), settings=broken)
    tampered = tmp_path / "tampered"
    shutil.copytree(path, tampered / path.name)
    (tampered / path.name / "station_geo_monthly.parquet").write_bytes(b"x")
    with pytest.raises(ValueError, match="checksum"):
        publish_geo(tampered / path.name, database)
    forged = tmp_path / "forged" / path.name
    shutil.copytree(path, forged)
    manifest = read_json(forged / "manifest.json")
    write_json(forged / "manifest.json", {**manifest, "source_snapshot_id": "1" * 64})
    with pytest.raises(ValueError, match="published F3"):
        publish_geo(forged, database)


def test_risk_publication_currency_and_rollback(
    database: DatabaseSettings, forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    forecast = forecast_workflow["forecast"]
    publish_forecast(forecast, database)
    gbfs = controlled_gbfs(tmp_path, f4_checked(forecast)["station_ids"])
    as_of = datetime.now(UTC)
    config = Config(hours=1, repeats=2, scales=(0.0, 1.0), orders=("uniform",))
    scenario, gate = load(forecast, gbfs, as_of, config, database)
    assert scenario is not None
    path = execute(tmp_path, scenario, gate, config)
    records = risk_records(path)
    assert len(records["outcomes"]) == 2 * 1 * 3
    assert {s["station_id"] for s in records["stations"] if s["eligible"]} == set(
        f4_checked(forecast)["station_ids"]
    )
    identifier = publish_risk(path, database)
    assert publish_risk(path, database) == identifier
    client = TestClient(create_app())
    body = client.get("/v4/risk").json()
    assert body["publication"]["publication_id"] == identifier
    assert (
        body["filters"]["scale"] == 1.0 and body["filters"]["event_order"] == "uniform"
    )
    assert len(body["outcomes"]) == 6
    assert {o["policy"] for o in body["outcomes"]} == {
        "no_action",
        "heuristic",
        "optimizer",
    }
    for outcome in body["outcomes"]:
        assert outcome["constraint_violations"] == 0
        objective = outcome["totals"]["scenario_objective"]["mean"]
        assert objective >= outcome["totals"]["unserved_total"]["mean"] - 1e-9
    created = datetime.fromisoformat(body["publication"]["created_at"])
    assert body["publication"]["mode"] == (
        "recorded_as_of_replay" if created > as_of else "contemporaneous"
    )
    fresh = read_risk(RiskFilters(), database, now=as_of + timedelta(seconds=10))
    assert fresh.status == "current_scenario"
    old = read_risk(RiskFilters(), database, now=as_of + timedelta(minutes=5))
    assert old.status == "archival_replay" and old.currency is not None
    assert "not a current recommendation" in old.currency.reason
    assert client.get("/v4/risk", params={"scale": 3.5}).status_code == 422
    assert client.get("/v4/risk", params={"event_order": "x"}).status_code == 422
    outside = client.get("/v4/risk", params={"station_id": "not-in-cohort"}).json()
    assert outside["status"] == "empty"
    # Unavailable (stale) results are never publishable; the active one remains.
    stale, reason = load(forecast, gbfs, as_of + timedelta(minutes=2), config, database)
    assert stale is None
    stale_path = execute(tmp_path, None, reason, config)
    with pytest.raises(ValueError, match="Only verified successful"):
        publish_risk(stale_path, database)
    other = Config(**{**asdict(config), "seed": 7})
    scenario, gate = load(forecast, gbfs, as_of, other, database)
    assert scenario is not None
    second = execute(tmp_path, scenario, gate, other)
    with pytest.raises(RuntimeError, match="Injected"):
        publish_risk(second, database, fail_before_activate=True)
    assert read_risk(RiskFilters(), database).filters.publication_id == identifier
    with connect(database) as db:
        found = db.execute(
            "SELECT count(*) n FROM risk_publications WHERE publication_id=%s",
            (second.name,),
        ).fetchone()
    assert found is not None and found["n"] == 0
    assert publish_risk(second, database) == second.name
    assert read_risk(RiskFilters(), database).filters.publication_id == second.name
    pinned = client.get("/v4/risk", params={"publication_id": identifier}).json()
    assert pinned["publication"]["publication_id"] == identifier
    assert (
        client.get("/v4/risk", params={"publication_id": "f" * 64}).status_code == 422
    )
