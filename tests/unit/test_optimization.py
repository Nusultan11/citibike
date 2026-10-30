from dataclasses import replace
from typing import Any

import pytest

from citibike.optimization.optimizer import Transfer, distance, plan, validate
from citibike.risk.simulator import events, simulate
from tests.unit.test_risk import example


def test_integer_optimizer_matches_independent_small_exhaustive_minimum() -> None:
    s, c = example()
    actual = plan(s, c, "optimizer", 1)
    a, b = s.stations
    # Enumerate the sole possible direct edge; independent target/cost calculation.
    candidates = []
    for n in range(c.vehicle_capacity + 1):
        lat_delta = abs(b.lat - a.lat) * 3.141592653589793 / 180
        km = 6371.0088 * lat_delta
        cost = n * km * c.cost_per_bike_km + (c.cost_per_vehicle if n else 0)
        candidates.append(
            abs(a.bikes - n - 0) + abs(b.bikes + n - 10) + c.cost_weight * cost
        )
    assert actual["solver"]["status"] == 0
    assert actual["surrogate_objective"] == pytest.approx(min(candidates))
    assert actual == {
        **plan(s, c, "optimizer", 1),
        "elapsed_seconds": actual["elapsed_seconds"],
    }
    assert plan(s, c, "optimizer", 0)["transfers"] == []
    assert plan(s, c, "heuristic", 0)["transfers"] == []
    assert plan(s, replace(c, vehicles=0), "optimizer", 1)["transfers"] == []
    draw = events(s, c, 1, "uniform", 0)
    for policy in ("no_action", "heuristic", "optimizer"):
        planned = plan(s, c, policy, 1)
        out = simulate(s, c, draw, [Transfer(**t) for t in planned["transfers"]])
        assert out["attempts"] == len(draw) and out["constraint_violations"] == 0


def test_threshold_heuristic_is_independent_of_demand_target_surrogate() -> None:
    s, c = example()
    reversed_demand = replace(s, demand={"A": ((8.0, 0.0),), "B": ((0.0, 8.0),)})
    transfers = plan(reversed_demand, c, "heuristic", 1)["transfers"]
    assert len(transfers) == 1 and transfers[0]["source"] == "A"
    assert transfers[0]["bikes"] == 3


@pytest.mark.parametrize(
    "case", ["negative", "stock", "docks", "fleet", "time", "closed", "duplicate"]
)
def test_invalid_plans_are_blocked(case: str) -> None:
    s, c = example()
    a, b = s.stations
    km = distance(a, b)
    t = Transfer("A", "B", 4, 1, km / c.speed_kmh * 60, km)
    if case == "negative":
        t = replace(t, bikes=-1)
    elif case == "stock":
        t = replace(t, bikes=9, vehicles=3)
        c = replace(c, vehicles=3)
    elif case == "docks":
        t = replace(t, source="B", destination="A", bikes=3)
    elif case == "fleet":
        t = replace(t, vehicles=2)
    elif case == "time":
        t = replace(t, minutes=9999)
    elif case == "closed":
        s = replace(s, stations=(a, replace(b, returning=False)))
    with pytest.raises(ValueError):
        validate([t, t] if case == "duplicate" else [t], s, c)


def test_solver_failure_is_explicit_and_keeps_other_policies(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citibike.risk.run as runner
    from tests.unit.test_risk import gate_for

    s, c = example()
    original = plan

    def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if args[2] == "optimizer":
            raise ValueError("controlled solver time limit")
        return original(*args, **kwargs)

    monkeypatch.setattr(runner, "plan", broken)
    result = runner.checked(runner.execute(tmp_path, s, gate_for(s), c))
    assert result["status"] == "partial" and len(result["failures"]) == 3
    assert not (tmp_path / "f5/active.json").exists()
    assert all(r["policies"]["optimizer"] is None for r in result["comparisons"])
