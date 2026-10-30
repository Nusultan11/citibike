"""Integer direct-trip allocation with an explicit inventory-target surrogate."""

import importlib
import math
import time
from dataclasses import asdict, dataclass
from typing import Any

from citibike.risk import Config, Scenario, Station


@dataclass(frozen=True)
class Transfer:
    source: str
    destination: str
    bikes: int
    vehicles: int
    minutes: float
    distance_km: float


def distance(a: Station, b: Station) -> float:
    p, q = math.radians(a.lat), math.radians(b.lat)
    delta = math.radians(b.lon - a.lon)
    h = (
        math.sin((q - p) / 2) ** 2
        + math.cos(p) * math.cos(q) * math.sin(delta / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def targets(scenario: Scenario, scale: float) -> dict[str, float]:
    result = {}
    for s in scenario.stations:
        d, a = (
            sum(pair[k] for pair in scenario.demand[s.station_id]) * scale
            for k in (0, 1)
        )
        result[s.station_id] = (
            max(0.0, min(s.slots, s.slots / 2 + d - a)) if d + a else float(s.bikes)
        )
    return result


def movement_cost(plan: list[Transfer], config: Config) -> float:
    return sum(
        t.bikes * t.distance_km * config.cost_per_bike_km
        + t.vehicles * config.cost_per_vehicle
        for t in plan
    )


def validate(plan: list[Transfer], scenario: Scenario, config: Config) -> None:
    stations = {s.station_id: s for s in scenario.stations}
    outgoing = dict.fromkeys(stations, 0)
    incoming = dict.fromkeys(stations, 0)
    pairs = set()
    for t in plan:
        if (
            t.source not in stations
            or t.destination not in stations
            or (t.source == t.destination or (t.source, t.destination) in pairs)
        ):
            raise ValueError("Invalid/duplicate transfer endpoints")
        pairs.add((t.source, t.destination))
        a, b = stations[t.source], stations[t.destination]
        if not all((a.installed, a.renting, b.installed, b.returning)):
            raise ValueError("Transfer violates operating status")
        if (
            type(t.bikes) is not int
            or type(t.vehicles) is not int
            or not (
                0 < t.bikes <= t.vehicles * config.vehicle_capacity and t.vehicles > 0
            )
        ):
            raise ValueError("Invalid transport capacity/nonnegative transfer")
        km = distance(a, b)
        minutes = config.handling_minutes + km / config.speed_kmh * 60
        if (
            abs(t.distance_km - km) > 1e-8
            or abs(t.minutes - minutes) > 1e-8
            or not (0 <= t.minutes < scenario.lead_minutes + config.hours * 60)
        ):
            raise ValueError("Transfer time/distance/horizon inconsistency")
        outgoing[t.source] += t.bikes
        incoming[t.destination] += t.bikes
    if sum(t.vehicles for t in plan) > config.vehicles:
        raise ValueError("Transport fleet limit exceeded")
    if any(
        outgoing[s.station_id] > s.bikes or incoming[s.station_id] > s.docks
        for s in scenario.stations
    ):
        raise ValueError("Transfer violates available stock/docks")


def plan(
    scenario: Scenario, config: Config, policy: str, scale: float
) -> dict[str, Any]:
    before = time.perf_counter()
    if len(next(iter(scenario.demand.values()))) != config.hours:
        raise ValueError("Scenario/config horizon mismatch")
    if policy not in ("no_action", "heuristic", "optimizer"):
        raise ValueError("Unknown transfer policy")
    desired = targets(scenario, scale)
    stations = sorted(scenario.stations, key=lambda s: s.station_id)
    donor = [
        s
        for s in stations
        if s.installed and s.renting and s.bikes > desired[s.station_id] + 1e-9
    ]
    recipient = [
        s
        for s in stations
        if s.installed and s.returning and s.bikes < desired[s.station_id] - 1e-9
    ]
    if policy == "heuristic":
        has_demand = any(
            sum(d + a for d, a in values) * scale > 0
            for values in scenario.demand.values()
        )
        donor = [
            s
            for s in stations
            if has_demand
            and s.installed
            and s.renting
            and s.bikes > s.slots * config.high_threshold
        ]
        recipient = [
            s
            for s in stations
            if has_demand
            and s.installed
            and s.returning
            and s.bikes < s.slots * config.low_threshold
        ]
    edges = []
    for a in donor:
        for b in recipient:
            km = distance(a, b)
            minutes = config.handling_minutes + km / config.speed_kmh * 60
            if minutes < scenario.lead_minutes + config.hours * 60:
                edges.append((a, b, km, minutes))
    transfers: list[Transfer] = []
    solver: dict[str, Any] = {"status": "not_needed"}
    if policy == "heuristic" and config.vehicles:
        fleet = config.vehicles
        stock = {s.station_id: s.bikes for s in stations}
        room = {s.station_id: s.docks for s in stations}
        for a, b, km, minutes in sorted(
            edges, key=lambda e: (e[2], e[0].station_id, e[1].station_id)
        ):
            if (
                stock[a.station_id] <= a.slots * config.high_threshold
                or stock[b.station_id] >= b.slots * config.low_threshold
            ):
                continue
            n = min(
                stock[a.station_id] - math.ceil(a.slots / 2),
                math.floor(b.slots / 2) - stock[b.station_id],
                room[b.station_id],
                fleet * config.vehicle_capacity,
            )
            if n <= 0:
                continue
            vehicles = math.ceil(n / config.vehicle_capacity)
            transfers.append(
                Transfer(a.station_id, b.station_id, n, vehicles, minutes, km)
            )
            fleet -= vehicles
            stock[a.station_id] -= n
            stock[b.station_id] += n
            room[b.station_id] -= n
    elif policy == "optimizer" and config.vehicles and edges:
        np = importlib.import_module("numpy")
        optimize = importlib.import_module("scipy.optimize")
        sparse = importlib.import_module("scipy.sparse")
        m, n = len(edges), len(stations)
        size = 2 * m + 2 * n
        c = np.zeros(size)
        upper = np.full(size, np.inf)
        for j, (a, b, km, _) in enumerate(edges):
            c[j] = config.cost_weight * km * config.cost_per_bike_km
            c[m + j] = config.cost_weight * config.cost_per_vehicle
            upper[j] = min(a.bikes, b.docks, config.vehicles * config.vehicle_capacity)
            upper[m + j] = config.vehicles
        c[2 * m :] = 1
        matrix: list[dict[int, float]] = []
        low: list[float] = []
        high: list[float] = []

        def row(values: dict[int, float], lower: float, higher: float) -> None:
            matrix.append(values)
            low.append(lower)
            high.append(higher)

        for k, s in enumerate(stations):
            ins = {j: 1.0 for j, e in enumerate(edges) if e[1] == s}
            outs = {j: 1.0 for j, e in enumerate(edges) if e[0] == s}
            balance = {
                **ins,
                **{j: -1.0 for j in outs},
                2 * m + k: -1.0,
                2 * m + n + k: 1.0,
            }
            row(
                balance,
                desired[s.station_id] - s.bikes,
                desired[s.station_id] - s.bikes,
            )
            row(outs, 0, s.bikes)
            row(ins, 0, s.docks)
        for j in range(m):
            row({j: 1, m + j: -config.vehicle_capacity}, -np.inf, 0)
        row({m + j: 1 for j in range(m)}, 0, config.vehicles)
        rr, cc, vv = [], [], []
        for k, values in enumerate(matrix):
            for j, v in values.items():
                rr.append(k)
                cc.append(j)
                vv.append(v)
        result = optimize.milp(
            c,
            integrality=np.r_[np.ones(2 * m), np.zeros(2 * n)],
            bounds=optimize.Bounds(np.zeros(size), upper),
            constraints=optimize.LinearConstraint(
                sparse.coo_array((vv, (rr, cc)), shape=(len(matrix), size)).tocsc(),
                np.array(low),
                np.array(high),
            ),
            options={
                "time_limit": config.solver_seconds,
                "mip_rel_gap": 0.0,
                "node_limit": 10000,
            },
        )
        solver = {
            "status": int(result.status),
            "message": str(result.message),
            "objective": float(result.fun) if result.fun is not None else None,
            "mip_gap": float(result.mip_gap)
            if result.get("mip_gap") is not None
            else None,
        }
        if not result.success or result.x is None:
            raise ValueError(f"Optimizer unavailable: {solver}")
        if any(abs(result.x[j] - round(result.x[j])) > 1e-5 for j in range(2 * m)):
            raise ValueError("Non-integral solver output")
        for j, (a, b, km, minutes) in enumerate(edges):
            bikes = round(float(result.x[j]))
            if bikes:
                transfers.append(
                    Transfer(
                        a.station_id,
                        b.station_id,
                        bikes,
                        round(float(result.x[m + j])),
                        minutes,
                        km,
                    )
                )
    validate(transfers, scenario, config)
    terminal = {s.station_id: s.bikes for s in stations}
    for t in transfers:
        terminal[t.source] -= t.bikes
        terminal[t.destination] += t.bikes
    cost = movement_cost(transfers, config)
    surrogate = (
        sum(abs(terminal[s] - desired[s]) for s in terminal) + config.cost_weight * cost
    )
    if (
        solver.get("objective") is not None
        and abs(surrogate - solver["objective"]) > 1e-6
    ):
        raise ValueError("Solver objective reconciliation failed")
    return {
        "policy": policy,
        "transfers": [asdict(t) for t in transfers],
        "targets": desired,
        "surrogate_objective": surrogate,
        "movement_cost": cost,
        "solver": solver,
        "elapsed_seconds": time.perf_counter() - before,
    }
