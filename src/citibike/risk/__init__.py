"""F5 scenario contracts; simulated outcomes are not calibrated failure risks."""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

VERSION = "f5.0.0"


@dataclass(frozen=True)
class Config:
    hours: int = 4
    repeats: int = 32
    seed: int = 42
    scales: tuple[float, ...] = (0.5, 1.0, 1.5)
    orders: tuple[str, ...] = ("uniform", "departures_first", "arrivals_first")
    vehicles: int = 2
    vehicle_capacity: int = 10
    speed_kmh: float = 15.0
    handling_minutes: float = 10.0
    cost_per_bike_km: float = 1.0
    cost_per_vehicle: float = 1.0
    cost_weight: float = 0.1
    low_threshold: float = 0.25
    high_threshold: float = 0.75
    solver_seconds: float = 10.0
    max_events: int = 200_000

    def __post_init__(self) -> None:
        for name, minimum, maximum in (
            ("hours", 1, 24),
            ("repeats", 1, 128),
            ("seed", 0, 2**32 - 1),
            ("vehicles", 0, 16),
            ("vehicle_capacity", 1, 100),
            ("max_events", 1, 200_000),
        ):
            v = getattr(self, name)
            if type(v) is not int or not minimum <= v <= maximum:
                raise ValueError(f"Invalid bounded scenario parameter: {name}")
        for name in (
            "speed_kmh",
            "handling_minutes",
            "cost_per_bike_km",
            "cost_per_vehicle",
            "cost_weight",
            "solver_seconds",
            "low_threshold",
            "high_threshold",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not math.isfinite(v) or v < 0:
                raise ValueError(f"Invalid scenario parameter: {name}")
        if not 0 < self.speed_kmh <= 120 or not 0 < self.solver_seconds <= 30:
            raise ValueError("Invalid speed/solver budget")
        if self.handling_minutes > 120:
            raise ValueError("Handling time exceeds bounded scenario")
        if not 0 <= self.low_threshold < self.high_threshold <= 1:
            raise ValueError("Invalid heuristic thresholds")
        if (
            not self.scales
            or len(set(self.scales)) != len(self.scales)
            or any(
                isinstance(s, bool) or not math.isfinite(s) or not 0 <= s <= 4
                for s in self.scales
            )
            or len(self.scales) > 5
        ):
            raise ValueError("Invalid demand scales")
        if (
            not self.orders
            or len(set(self.orders)) != len(self.orders)
            or any(
                o not in ("uniform", "departures_first", "arrivals_first")
                for o in self.orders
            )
        ):
            raise ValueError("Invalid event orders")


@dataclass(frozen=True)
class Station:
    station_id: str
    gbfs_id: str
    capacity: int
    bikes: int
    docks: int
    installed: bool
    renting: bool
    returning: bool
    lat: float
    lon: float

    def __post_init__(self) -> None:
        if not self.station_id or not self.gbfs_id:
            raise ValueError("Missing station identity")
        if (
            any(
                type(v) is not int or v < 0
                for v in (self.capacity, self.bikes, self.docks)
            )
            or self.bikes + self.docks > self.capacity
        ):
            raise ValueError("Capacity inconsistency")
        if any(
            type(v) is not bool for v in (self.installed, self.renting, self.returning)
        ):
            raise ValueError("Unknown operating status")
        if (
            not math.isfinite(self.lat)
            or not math.isfinite(self.lon)
            or (abs(self.lat) > 90 or abs(self.lon) > 180)
        ):
            raise ValueError("Invalid station coordinates")

    @property
    def slots(self) -> int:
        # Unavailable/disabled capacity remains blocked, never invented as free docks.
        return self.bikes + self.docks


@dataclass(frozen=True)
class Scenario:
    as_of: datetime
    start: datetime
    stations: tuple[Station, ...]
    demand: dict[str, tuple[tuple[float, float], ...]]
    provenance: dict[str, Any] = field(default_factory=dict)
    exclusions: dict[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None or self.start.tzinfo is None:
            raise ValueError("Scenario requires aware as-of and <=1h lead-in")
        object.__setattr__(self, "as_of", self.as_of.astimezone(UTC))
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        if not 0 <= (self.start - self.as_of).total_seconds() <= 3600:
            raise ValueError("Scenario requires aware as-of and <=1h lead-in")
        ids = [s.station_id for s in self.stations]
        if not 1 <= len(ids) <= 64 or len(set(ids)) != len(ids):
            raise ValueError("Duplicate/invalid bounded station cohort")
        if len({s.gbfs_id for s in self.stations}) != len(ids):
            raise ValueError("Duplicate GBFS station mapping")
        if (
            set(self.demand) != set(ids)
            or len({len(v) for v in self.demand.values()}) != 1
        ):
            raise ValueError("Incomplete demand coverage")
        for values in self.demand.values():
            if not 1 <= len(values) <= 24 or any(
                len(pair) != 2
                or any(not math.isfinite(v) or not 0 <= v <= 1000 for v in pair)
                for pair in values
            ):
                raise ValueError("Invalid bounded demand rates")

    @property
    def lead_minutes(self) -> float:
        return (self.start - self.as_of).total_seconds() / 60
