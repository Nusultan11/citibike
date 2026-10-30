"""Paired Poisson scenario attempts and discrete inventory/transport events."""

import importlib
import math
from typing import Any

from citibike.optimization.optimizer import Transfer, movement_cost, validate
from citibike.risk import VERSION, Config, Scenario
from citibike.storage import identity

Event = tuple[float, str, str, int]


def events(
    scenario: Scenario, config: Config, scale: float, order: str, repeat: int
) -> list[Event]:
    if (
        order not in config.orders
        or scale not in config.scales
        or not 0 <= repeat < config.repeats
    ):
        raise ValueError("Undeclared scenario draw")
    np = importlib.import_module("numpy")
    result: list[Event] = []
    for station in sorted(scenario.demand):
        for h, pair in enumerate(scenario.demand[station]):
            for k, kind in enumerate(("departures", "arrivals")):
                # Same base random stream across scales/orders; every policy receives
                # the very same materialized events, never makes its own random draws.
                seed = int(
                    identity([VERSION, config.seed, repeat, station, h, kind])[:16], 16
                )
                rng = np.random.Generator(np.random.PCG64(seed))
                n = int(rng.poisson(pair[k] * scale))
                if len(result) + n > config.max_events:
                    raise ValueError("Scenario event budget exceeded")
                for j, u in enumerate(rng.random(n)):
                    fraction = float(u)
                    if order != "uniform":
                        first = (kind == "departures") == (order == "departures_first")
                        fraction = fraction / 2 + (0 if first else 0.5)
                    minute = scenario.lead_minutes + h * 60 + fraction * 60
                    result.append((minute, station, kind, j))
    return sorted(result)


def simulate(
    scenario: Scenario, config: Config, draw: list[Event], transfers: list[Transfer]
) -> dict[str, Any]:
    validate(transfers, scenario, config)
    stations = {s.station_id: s for s in scenario.stations}
    if len(next(iter(scenario.demand.values()))) != config.hours:
        raise ValueError("Scenario/config horizon mismatch")
    state = {s.station_id: s.bikes for s in scenario.stations}
    accepted = {s: {"departures": 0, "arrivals": 0} for s in stations}
    rejected = {
        s: {
            "departures": 0,
            "arrivals": 0,
            "closed_departures": 0,
            "closed_arrivals": 0,
        }
        for s in stations
    }
    empty = {s: False for s in stations}
    full = {s: False for s in stations}
    cargo = [t.bikes for t in transfers]
    delivered = 0

    def observe() -> None:
        for sid, station in stations.items():
            if not 0 <= state[sid] <= station.slots:
                raise ValueError("Conservation/capacity violation")
            empty[sid] |= station.installed and station.renting and state[sid] == 0
            full[sid] |= (
                station.installed and station.returning and state[sid] == station.slots
            )
        expected = sum(s.bikes for s in scenario.stations) + sum(
            v["arrivals"] - v["departures"] for v in accepted.values()
        )
        if sum(state.values()) + sum(cargo) != expected:
            raise ValueError("Conservation violation: inventory + in-transit balance")

    # Pre-dispatch state counts toward ever-empty/full; dispatch at t=0.
    initial_cargo = sum(cargo)
    cargo = [0 for _ in transfers]
    observe()
    cargo = [t.bikes for t in transfers]
    for t in transfers:
        state[t.source] -= t.bikes
    observe()
    timeline: list[tuple[float, int, str, str, int]] = []
    for i, t in enumerate(transfers):
        timeline.append((t.minutes, 0, t.destination, "delivery", i))
    for minute, sid, kind, sequence in draw:
        if (
            sid not in stations
            or kind not in ("departures", "arrivals")
            or not math.isfinite(minute)
            or not scenario.lead_minutes
            <= minute
            < scenario.lead_minutes + config.hours * 60
        ):
            raise ValueError("Invalid attempt station/time/type")
        # Delivery wins exact timestamp ties; customer ties sort station/type/id.
        timeline.append((minute, 1, sid, kind, sequence))
    for _, _, sid, kind, index in sorted(timeline):
        station = stations[sid]
        if kind == "delivery":
            # Customer returns can fill docks before arrival. Never clip away cargo:
            # one delivery attempt, undeliverable bikes stay on that vehicle to end.
            moved = min(cargo[index], station.slots - state[sid])
            state[sid] += moved
            cargo[index] -= moved
            delivered += moved
        else:
            operating = station.installed and (
                station.renting if kind == "departures" else station.returning
            )
            feasible = (
                state[sid] > 0 if kind == "departures" else state[sid] < station.slots
            )
            if operating and feasible:
                state[sid] += -1 if kind == "departures" else 1
                accepted[sid][kind] += 1
            else:
                rejected[sid][kind] += 1
                if not operating:
                    rejected[sid]["closed_" + kind] += 1
        observe()
    lost_d = sum(r["departures"] for r in rejected.values())
    lost_a = sum(r["arrivals"] for r in rejected.values())
    cost = movement_cost(transfers, config)
    return {
        "accepted": accepted,
        "rejected": rejected,
        "final_bikes": state,
        "ever_empty": empty,
        "ever_full": full,
        "cargo_end": cargo,
        "unserved_departures": lost_d,
        "unserved_arrivals": lost_a,
        "unserved_total": lost_d + lost_a,
        "attempts": len(draw),
        "planned_moved": initial_cargo,
        "delivered": delivered,
        "undelivered": sum(cargo),
        "movement_cost": cost,
        "scenario_objective": lost_d + lost_a + config.cost_weight * cost,
        "constraint_violations": 0,
    }


def fraction(successes: int, n: int) -> dict[str, Any]:
    """Wilson interval describes Monte Carlo precision ONLY, not calibration."""
    p = successes / n
    z = 1.959963984540054
    denominator = 1 + z * z / n
    middle = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return {
        "outcomes": successes,
        "repeats": n,
        "fraction": p,
        "mc_standard_error": math.sqrt(p * (1 - p) / n),
        "mc_wilson_95": [max(0.0, middle - half), min(1.0, middle + half)],
    }


def summarize(outcomes: list[dict[str, Any]], scenario: Scenario) -> dict[str, Any]:
    n = len(outcomes)
    if not n:
        raise ValueError("Missing simulation outcomes, not zero risk")
    totals = {}
    for field in (
        "unserved_departures",
        "unserved_arrivals",
        "unserved_total",
        "scenario_objective",
        "delivered",
        "undelivered",
    ):
        values = sorted(float(r[field]) for r in outcomes)
        totals[field] = {
            "mean": sum(values) / n,
            "min": values[0],
            "max": values[-1],
            "p05": values[int((n - 1) * 0.05)],
            "p95": values[int((n - 1) * 0.95)],
        }
    risks: dict[str, dict[str, Any]] = {
        s.station_id: {
            "status": "ok"
            if s.installed and (s.renting or s.returning)
            else "not_operating",
            "empty": fraction(sum(r["ever_empty"][s.station_id] for r in outcomes), n)
            if s.installed and s.renting
            else None,
            "full": fraction(sum(r["ever_full"][s.station_id] for r in outcomes), n)
            if s.installed and s.returning
            else None,
            "renting": s.renting and s.installed,
            "returning": s.returning and s.installed,
        }
        for s in scenario.stations
    }
    for sid, reasons in scenario.exclusions.items():
        risks[sid] = {
            "status": "unavailable",
            "reasons": reasons,
            "empty": None,
            "full": None,
        }
    return {
        "totals": totals,
        "station_risk": risks,
        "repeats": n,
        "constraint_violations": sum(r["constraint_violations"] for r in outcomes),
    }
