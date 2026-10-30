import copy
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from citibike.features.contracts import SCHEMA, target_hour
from citibike.optimization.optimizer import Transfer, distance
from citibike.risk import Config, Scenario, Station
from citibike.risk.inputs import adapt, load
from citibike.risk.run import checked, execute
from citibike.risk.simulator import events, simulate, summarize
from citibike.risk.verify import verify
from citibike.storage import read_json, write_json


def example() -> tuple[Scenario, Config]:
    as_of = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    stations = (
        Station("A", "uuid-A", 10, 8, 2, True, True, True, 40.7, -74.0),
        Station("B", "uuid-B", 10, 1, 9, True, True, True, 40.701, -74.0),
    )
    scenario = Scenario(
        as_of,
        as_of + timedelta(minutes=30),
        stations,
        {"A": ((0.0, 8.0),), "B": ((8.0, 0.0),)},
        {"data_mode": "controlled_fixture"},
    )
    config = Config(
        hours=1,
        repeats=4,
        scales=(0.0, 1.0, 2.0),
        vehicles=1,
        vehicle_capacity=4,
        handling_minutes=0,
    )
    return scenario, config


def input_example() -> tuple[
    dict[str, Any], list[dict[str, Any]], datetime, list[dict[str, Any]], dict[str, Any]
]:
    scenario, _ = example()
    as_of = scenario.as_of
    origin = as_of - timedelta(minutes=10)
    report = {
        "forecast_origin_utc": origin.isoformat(),
        "issued_at": (origin + timedelta(seconds=1)).isoformat(),
        "strict_source_available_at": (origin - timedelta(days=1)).isoformat(),
        "valid_until": (target_hour(origin, 24) + timedelta(hours=1)).isoformat(),
        "availability_mode": "strict",
        "feature_schema": SCHEMA,
        "status": "ok",
        "station_ids": ["A", "B"],
    }
    rows = [
        {
            "station_id": sid,
            "forecast_origin_utc": origin,
            "target_hour_utc": target_hour(origin, h),
            "horizon_h": h,
            "prediction_type": k,
            "predicted_value": 0.0,
            "status": "ok",
            "reason": None,
            "feature_available_at": origin - timedelta(days=1),
        }
        for sid in ("A", "B")
        for h in range(1, 25)
        for k in ("departures", "arrivals")
    ]
    gbfs = [
        {
            "station_id": s.gbfs_id,
            "trip_station_id": s.station_id,
            "capacity": s.capacity,
            "bikes_available": s.bikes,
            "docks_available": s.docks,
            "is_installed": s.installed,
            "is_renting": s.renting,
            "is_returning": s.returning,
            "lat": s.lat,
            "lon": s.lon,
            "quality_status": "valid",
            "quality_reason_codes": [],
            "ttl": 60,
            "last_reported": (as_of - timedelta(seconds=2)).isoformat(),
        }
        for s in scenario.stations
    ]
    feeds = {
        name: {
            "received_at_utc": (as_of - timedelta(seconds=1)).isoformat(),
            "last_updated": (as_of - timedelta(seconds=2)).isoformat(),
            "ttl": 60,
        }
        for name in ("station_information", "station_status")
    }
    return report, rows, as_of - timedelta(seconds=1), gbfs, feeds


def gate_for(s: Scenario) -> dict[str, Any]:
    return {
        "status": "ok",
        "as_of": s.as_of.isoformat(),
        "provenance": s.provenance,
        "coverage": {"requested": 2, "eligible": 2},
        "risk": "scenario-only",
    }


def test_order_capacity_blocked_slots_and_closed_attempts() -> None:
    s, c = example()
    one = replace(s.stations[0], capacity=3, bikes=1, docks=0)
    s = replace(s, stations=(one,), demand={"A": ((1, 1),)})
    first = simulate(s, c, [(31, "A", "arrivals", 0), (32, "A", "departures", 0)], [])
    second = simulate(s, c, [(31, "A", "departures", 0), (32, "A", "arrivals", 0)], [])
    assert first["unserved_arrivals"] == 1 and second["unserved_total"] == 0
    assert second["final_bikes"] == {"A": 1}  # two blocked slots stay blocked
    closed = replace(s, stations=(replace(one, renting=False, returning=False),))
    out = simulate(closed, c, [(31, "A", "departures", 0)], [])
    assert out["rejected"]["A"]["closed_departures"] == 1
    assert out["final_bikes"]["A"] == 1
    assert summarize([out], closed)["station_risk"]["A"]["empty"] is None


def test_dst_local_fold_is_normalized_before_lead_calculation() -> None:
    s, _ = example()
    local = ZoneInfo("America/New_York")
    repeated = replace(
        s,
        as_of=datetime(2026, 11, 1, 1, 30, tzinfo=local, fold=0),
        start=datetime(2026, 11, 1, 1, 0, tzinfo=local, fold=1),
    )
    assert repeated.lead_minutes == 30 and repeated.start.hour == 6


def test_delivery_keeps_undeliverable_cargo_and_balance() -> None:
    s, c = example()
    c = replace(c, handling_minutes=40)
    a, b = s.stations
    km = distance(a, b)
    t = Transfer("A", "B", 4, 1, 40 + km / c.speed_kmh * 60, km)
    draw = [(31 + i / 10, "B", "arrivals", i) for i in range(9)]
    out = simulate(s, c, draw, [t])
    assert out["undelivered"] == 4 and out["delivered"] == 0
    assert sum(out["final_bikes"].values()) + sum(out["cargo_end"]) == 9 + 9
    assert out["constraint_violations"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"vehicle_capacity": 0},
        {"vehicles": -1},
        {"hours": 25},
        {"repeats": True},
        {"scales": ()},
        {"scales": (float("nan"),)},
        {"orders": ("unknown",)},
        {"speed_kmh": 0},
        {"low_threshold": 0.9, "high_threshold": 0.1},
    ],
)
def test_invalid_scenario_parameters(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Config(**changes)


def test_zero_seed_repeatability_budget_and_invalid_attempt() -> None:
    s, c = example()
    assert events(s, c, 0, "uniform", 0) == []
    draw = events(s, c, 1, "uniform", 0)
    assert draw == events(s, c, 1, "uniform", 0)
    assert draw != events(s, replace(c, seed=43), 1, "uniform", 0)
    assert sum(e[2] == "arrivals" for e in draw) == sum(
        e[2] == "arrivals" for e in events(s, c, 1, "arrivals_first", 0)
    )
    with pytest.raises(ValueError, match="budget"):
        events(s, replace(c, max_events=1), 2, "uniform", 0)
    with pytest.raises(ValueError, match="attempt"):
        simulate(s, c, [(0, "A", "departures", 0)], [])
    with pytest.raises(ValueError, match="zero risk"):
        summarize([], s)


@pytest.mark.parametrize(
    "case",
    [
        "stale",
        "future_receipt",
        "future_publication",
        "missing",
        "mixed_origin",
        "ttl_zero",
        "expired",
    ],
)
def test_asof_gates_do_not_become_zero(case: str) -> None:
    report, rows, published, gbfs, feeds = input_example()
    s, c = example()
    if case == "stale":
        feeds["station_status"]["last_updated"] = (
            s.as_of - timedelta(minutes=2)
        ).isoformat()
    elif case == "future_receipt":
        feeds["station_information"]["received_at_utc"] = (
            s.as_of + timedelta(seconds=1)
        ).isoformat()
    elif case == "future_publication":
        published = s.as_of + timedelta(seconds=1)
    elif case == "missing":
        gbfs = []
    elif case == "mixed_origin":
        rows[0]["forecast_origin_utc"] += timedelta(seconds=1)
    elif case == "ttl_zero":
        feeds["station_status"]["ttl"] = 0
    else:
        report["valid_until"] = s.as_of.isoformat()
    scenario, gate = adapt(
        report,
        rows,
        published,
        gbfs,
        feeds,
        s.as_of,
        c,
        {"data_mode": "controlled_fixture"},
    )
    assert scenario is None and gate["status"] == "unavailable" and gate["risk"] is None


def test_station_coverage_unavailable_capacity_mapping_and_dst() -> None:
    s, c = example()
    report, rows, published, gbfs, feeds = input_example()
    rows[0].update(status="unavailable", predicted_value=None, reason="no_history")
    # Station A's first forecast hour is unavailable, station B remains valid.
    result, gate = adapt(report, rows, published, gbfs, feeds, s.as_of, c, s.provenance)
    assert result is not None and gate["coverage"]["fraction"] == 0.5
    assert result.start == datetime(
        2026, 11, 1, 6, tzinfo=UTC
    )  # repeated NY 01:00, UTC key
    summary = summarize([simulate(result, c, [], [])], result)
    assert summary["station_risk"]["A"]["empty"] is None
    report, rows, published, gbfs, feeds = input_example()
    gbfs[0]["bikes_available"] = 99
    result, gate = adapt(report, rows, published, gbfs, feeds, s.as_of, c, s.provenance)
    assert result is not None and "invalid_station_state" in str(gate)
    gbfs[0]["station_id"] = gbfs[1]["station_id"]
    result, gate = adapt(report, rows, published, gbfs, feeds, s.as_of, c, s.provenance)
    assert result is None and "duplicate_gbfs_station_id" in gate["reasons"]
    report, rows, published, gbfs, feeds = input_example()
    gbfs.append({**gbfs[0], "station_id": "other-uuid"})
    result, gate = adapt(report, rows, published, gbfs, feeds, s.as_of, c, s.provenance)
    assert result is not None and "ambiguous_station_mapping" in str(gate)
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(ValueError, match="Duplicate"):
        adapt(report, rows, published, gbfs, feeds, s.as_of, c, s.provenance)
    report, rows, published, gbfs, feeds = input_example()
    with pytest.raises(ValueError, match="24 hours"):
        adapt(report, rows[:-1], published, gbfs, feeds, s.as_of, c, s.provenance)
    unknown, reason = adapt(
        report,
        rows,
        published,
        gbfs,
        feeds,
        s.as_of,
        c,
        s.provenance,
        requested=["unknown"],
    )
    assert unknown is None and "no_eligible_stations" in reason["reasons"]
    unavailable, reason = load(Path("not-a-forecast"), Path("not-gbfs"), s.as_of, c)
    assert unavailable is None and reason["risk"] is None


def test_immutable_repeat_partial_failure_and_independent_oracle(
    tmp_path: Path,
) -> None:
    s, c = example()
    first = execute(tmp_path, s, gate_for(s), c)
    assert execute(tmp_path, s, gate_for(s), c) == first
    assert verify(first)["outcomes"] == 4 * 3 * 3 * 3
    assert not (tmp_path / "f5/active.json").exists()
    active = (tmp_path / "f5/active_controlled.json").read_bytes()
    with pytest.raises(RuntimeError, match="Injected"):
        execute(tmp_path, s, gate_for(s), replace(c, seed=43), fail_after_events=True)
    assert (tmp_path / "f5/active_controlled.json").read_bytes() == active
    assert len(list((tmp_path / "f5/failed").glob("*.json"))) == 1
    unavailable = execute(
        tmp_path,
        None,
        {
            "status": "unavailable",
            "as_of": s.as_of.isoformat(),
            "reasons": ["missing_gbfs"],
            "provenance": s.provenance,
            "risk": None,
        },
        c,
    )
    assert verify(unavailable)["real_e2e"] == "NOT VERIFIED"
    assert (tmp_path / "f5/active_controlled.json").read_bytes() == active
    # Bypass the content checksum deliberately: the independent ledger must still fail.
    path = first / "outcomes.jsonl"
    original = path.read_text()
    lines = original.splitlines()
    row = json.loads(lines[0])
    row["outcome"]["final_bikes"]["A"] += 1
    lines[0] = json.dumps(row)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest = read_json(first / "manifest.json")
    manifest["checksums"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(first / "manifest.json", manifest)
    checked(first)
    with pytest.raises(ValueError, match="oracle"):
        verify(first)
