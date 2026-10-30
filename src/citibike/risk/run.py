"""Immutable, paired F5 CLI execution; failed runs retain the last good result."""

import importlib.metadata
import json
import tempfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citibike.analytics.build import publish_directory
from citibike.ingestion.archive import checksum
from citibike.optimization.optimizer import Transfer, plan
from citibike.processing.resources import peak_rss_bytes
from citibike.risk import VERSION, Config, Scenario
from citibike.risk.simulator import events, simulate, summarize
from citibike.storage import identity, read_json, write_json

ASSUMPTIONS = [
    "SCENARIO ONLY: Poisson attempts use published observed trip forecasts as means, "
    "not latent-demand estimates.",
    "Independent station/hour/type counts; returns are exogenous, not linked to "
    "simulated rentals. Fleet is an open subsystem.",
    "Uniform within-hour events or all departures/arrivals in the first half-hour; "
    "demand scales are assumptions, not fitted intervals.",
    "No demand is modeled between as-of and the next complete forecast hour; "
    "lead-in hold may be optimistic.",
    "GBFS operating flags and capacity remain fixed after as-of; disabled/unknown "
    "slots capacity-bikes-docks stay blocked.",
    "GBFS TTL validates the INITIAL snapshot only, not future observations "
    "over the simulation horizon.",
    "All transport departs at as-of; one direct loaded trip per vehicle, "
    "no fleet reuse, depot routing or operator cost claims.",
    "Haversine/specified speed plus handling gives scenario travel times, "
    "not measured street routes.",
    "Delivery wins exact-time ties, then station/type/sequence; undeliverable cargo "
    "remains on its vehicle to horizon end.",
    "Optimizer is optimal only for L1 inventory-target + weighted movement-cost "
    "surrogate; simulated losses can worsen.",
    "Risk is ever-empty/full fraction including initial state among assumed draws; "
    "Monte Carlo Wilson intervals are not calibration.",
    "No field validation/operational uplift/economic saving claim; "
    "Seasonal Naive/unavailable F4 fallback retained.",
]


def checked(path: Path) -> dict[str, Any]:
    manifest = read_json(path / "manifest.json")
    if manifest["version"] != VERSION:
        raise ValueError("F5 artifact version mismatch")
    for name, expected in manifest["checksums"].items():
        relative = Path(name)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or checksum(path / relative) != expected
        ):
            raise ValueError("F5 artifact checksum/path mismatch")
    if manifest["id"] != identity(read_json(path / "inputs.json")):
        raise ValueError("F5 immutable input identity mismatch")
    return manifest


def execute(
    root: Path,
    scenario: Scenario | None,
    gate: dict[str, Any],
    config: Config,
    *,
    fail_after_events: bool = False,
) -> Path:
    if (scenario is None) != (gate["status"] != "ok"):
        raise ValueError("Scenario/input gate inconsistency")
    before = time.perf_counter()
    recipe = {
        str(p.relative_to(Path(__file__).parents[1])): checksum(p)
        for folder in (
            Path(__file__).parent,
            Path(__file__).parents[1] / "optimization",
        )
        for p in sorted(folder.glob("*.py"))
    }
    environment = {
        p: importlib.metadata.version(p) for p in ("numpy", "scipy", "duckdb")
    }
    request = {
        "version": VERSION,
        "config": asdict(config),
        "gate": gate,
        "scenario": asdict(scenario) if scenario else None,
        "recipe": recipe,
        "environment": environment,
    }
    identifier = identity(request)
    destination = root / "f5/results" / identifier
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        checked(destination)
    else:
        try:
            with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
                stage = Path(temporary) / "result"
                stage.mkdir()
                write_json(stage / "inputs.json", request)
                comparisons = []
                failures = []
                plans: dict[str, Any] = {}
                if scenario is not None:
                    for scale in config.scales:
                        for policy in ("no_action", "heuristic", "optimizer"):
                            key = f"{scale:g}:{policy}"
                            try:
                                plans[key] = plan(scenario, config, policy, scale)
                            except ValueError as exc:
                                plans[key] = {
                                    "policy": policy,
                                    "status": "unavailable",
                                    "reason": str(exc),
                                }
                                failures.append({"plan": key, "reason": str(exc)})
                    write_json(stage / "plans.json", plans)
                    with (
                        (stage / "events.jsonl").open("w", encoding="utf-8") as stream,
                        (stage / "outcomes.jsonl").open(
                            "w", encoding="utf-8"
                        ) as output,
                    ):
                        for scale in config.scales:
                            for order in config.orders:
                                collected: dict[str, list[dict[str, Any]]] = {
                                    p: []
                                    for p in ("no_action", "heuristic", "optimizer")
                                }
                                policy_seconds = dict.fromkeys(collected, 0.0)
                                for repeat in range(config.repeats):
                                    draw = events(
                                        scenario, config, scale, order, repeat
                                    )
                                    draw_id = identity(draw)
                                    cell = {
                                        "scale": scale,
                                        "order": order,
                                        "repeat": repeat,
                                        "events_sha256": draw_id,
                                    }
                                    stream.write(
                                        json.dumps({**cell, "events": draw}) + "\n"
                                    )
                                    if fail_after_events:
                                        raise RuntimeError(
                                            "Injected failure after events; "
                                            "previous result retained"
                                        )
                                    for policy, outcomes in collected.items():
                                        planned = plans[f"{scale:g}:{policy}"]
                                        if planned.get("status") == "unavailable":
                                            continue
                                        transfers = [
                                            Transfer(**t) for t in planned["transfers"]
                                        ]
                                        tick = time.perf_counter()
                                        outcome = simulate(
                                            scenario, config, draw, transfers
                                        )
                                        policy_seconds[policy] += (
                                            time.perf_counter() - tick
                                        )
                                        outcomes.append(outcome)
                                        output.write(
                                            json.dumps(
                                                {
                                                    **cell,
                                                    "policy": policy,
                                                    "outcome": outcome,
                                                }
                                            )
                                            + "\n"
                                        )
                                summaries = {
                                    p: summarize(v, scenario) if v else None
                                    for p, v in collected.items()
                                }
                                base = summaries["no_action"]
                                assert base is not None
                                gains = {
                                    p: base["totals"]["unserved_total"]["mean"]
                                    - v["totals"]["unserved_total"]["mean"]
                                    if v
                                    else None
                                    for p, v in summaries.items()
                                }
                                rss = peak_rss_bytes()
                                comparisons.append(
                                    {
                                        "scale": scale,
                                        "order": order,
                                        "policies": summaries,
                                        "resources": {
                                            p: {
                                                "simulation_wall_seconds": v,
                                                "peak_process_rss_bytes": rss,
                                                "scope": "Shared process high-water; "
                                                "not isolated marginal policy RAM",
                                            }
                                            for p, v in policy_seconds.items()
                                        },
                                        "simulated_unserved_gain_vs_no_action": gains,
                                    }
                                )
                status = (
                    "unavailable"
                    if scenario is None
                    else "partial"
                    if failures
                    else "ok"
                )
                report = {
                    "version": VERSION,
                    "id": identifier,
                    "status": status,
                    "created_at": datetime.now(UTC).isoformat(),
                    "data_as_of": gate["as_of"],
                    "data_mode": gate["provenance"].get("data_mode", "unknown"),
                    "gate": gate,
                    "comparisons": comparisons,
                    "failures": failures,
                    "assumptions": ASSUMPTIONS,
                    "operational_promotion": "NOT VERIFIED",
                    "measurements": {
                        "elapsed_seconds": time.perf_counter() - before,
                        "peak_rss_bytes": peak_rss_bytes(),
                    },
                    "checksums": {
                        p.name: checksum(p)
                        for p in sorted(stage.iterdir())
                        if p.is_file()
                    },
                }
                write_json(stage / "manifest.json", report)
                publish_directory(stage, destination)
        except Exception as exc:
            write_json(
                root / "f5/failed" / f"{identifier}.json",
                {
                    "id": identifier,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": str(exc)[:1000],
                    "request": request,
                    "elapsed_seconds": time.perf_counter() - before,
                },
            )
            raise
    result = checked(destination)
    write_json(
        root / "f5/last_attempt.json",
        {"id": identifier, "path": str(destination), "status": result["status"]},
    )
    if result["status"] == "ok":
        pointer = (
            "active.json"
            if result["data_mode"] == "real_official"
            else "active_controlled.json"
        )
        write_json(root / "f5" / pointer, {"id": identifier, "path": str(destination)})
    return destination
