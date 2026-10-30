"""Independent file/ledger oracle. Does not call simulation/optimization helpers."""

import hashlib
import json
import math
import statistics
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def equal(actual: Any, expected: Any) -> None:
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or actual.keys() != expected.keys():
            raise ValueError("Independent oracle dictionary mismatch")
        for k in expected:
            equal(actual[k], expected[k])
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError("Independent oracle list mismatch")
        for a, e in zip(actual, expected, strict=True):
            equal(a, e)
    elif isinstance(expected, float):
        if not isinstance(actual, (float, int)) or not math.isclose(
            actual, expected, abs_tol=1e-8, rel_tol=1e-10
        ):
            raise ValueError("Independent oracle numeric mismatch")
    elif actual != expected:
        raise ValueError("Independent oracle value mismatch")


def input_oracle(request: dict[str, Any]) -> dict[str, Any]:
    """Recompute source freshness/state from original JSON, not adapter helpers."""
    gate = request["gate"]
    p = gate["provenance"]
    if "gbfs_feeds" not in p:
        return {
            "source_artifacts": "NOT VERIFIED; controlled in-memory or load failure"
        }
    as_of = datetime.fromisoformat(gate["as_of"]).astimezone(UTC)
    collection = Path(p["gbfs_path"])
    bronze = collection.parents[2] / "bronze" / p["gbfs_collection_id"]
    equal(sha(collection / "quality.json"), p["gbfs_quality_sha256"])
    equal(sha(collection / "station_status.parquet"), p["gbfs_silver_sha256"])
    equal(sha(bronze / "manifest.json"), p["gbfs_bronze_manifest_sha256"])
    forecast = Path(p["forecast_path"])
    equal(sha(forecast / "manifest.json"), p["forecast_manifest_sha256"])
    f4 = json.loads((forecast / "manifest.json").read_text(encoding="utf-8"))
    published = datetime.fromisoformat(p["published_at"]).astimezone(UTC)
    if published > as_of and "forecast_not_published_as_of" not in gate["reasons"]:
        raise ValueError("Independent oracle forecast publication gate mismatch")
    if (
        datetime.fromisoformat(f4["valid_until"]) <= as_of
        and "forecast_expired" not in gate["reasons"]
    ):
        raise ValueError("Independent oracle forecast expiry mismatch")
    feeds = {}
    raw = {}
    computed = []
    for name in ("station_information", "station_status"):
        meta = p["gbfs_feeds"][name]
        equal(sha(bronze / (name + ".json")), meta["checksum"])
        value = json.loads((bronze / (name + ".json")).read_text(encoding="utf-8"))
        raw[name] = value
        if value["version"] == "2.3":
            updated = datetime.fromtimestamp(value["last_updated"], UTC)
        else:
            updated = datetime.fromisoformat(
                value["last_updated"].replace("Z", "+00:00")
            ).astimezone(UTC)
        equal(updated, datetime.fromisoformat(meta["last_updated"]).astimezone(UTC))
        equal(value["ttl"], meta["ttl"])
        received = datetime.fromisoformat(meta["received_at_utc"]).astimezone(UTC)
        expiry = min(
            received + timedelta(seconds=60), updated + timedelta(seconds=value["ttl"])
        )
        if received > as_of or updated > as_of:
            computed.append("future_gbfs_" + name)
        if value["ttl"] <= 0 or expiry <= as_of:
            computed.append("stale_gbfs_" + name)
        feeds[name] = {
            "source_age_at_asof_seconds": (as_of - updated).total_seconds(),
            "ttl": value["ttl"],
            "expiry": expiry.isoformat(),
            "received_at": received.isoformat(),
            "raw_sha256": meta["checksum"],
        }
    if set(computed) != {
        r
        for r in gate.get("reasons", [])
        if r.startswith(("future_gbfs_station_", "stale_gbfs_station_"))
    }:
        raise ValueError("Independent oracle GBFS freshness gate mismatch")
    scenario = request["scenario"]
    if scenario:
        if computed:
            raise ValueError("Independent oracle accepted stale/future input")
        info = {
            r["station_id"]: r for r in raw["station_information"]["data"]["stations"]
        }
        states = {r["station_id"]: r for r in raw["station_status"]["data"]["stations"]}
        aliases = Counter(r.get("short_name") for r in info.values())
        for s in scenario["stations"]:
            i, r = info[s["gbfs_id"]], states[s["gbfs_id"]]
            equal(i["short_name"], s["station_id"])
            equal(aliases[s["station_id"]], 1)
            equal(
                [i["capacity"], i["lat"], i["lon"]], [s["capacity"], s["lat"], s["lon"]]
            )
            equal(
                r[
                    "num_bikes_available"
                    if raw["station_status"]["version"] == "2.3"
                    else "num_vehicles_available"
                ],
                s["bikes"],
            )
            equal(r["num_docks_available"], s["docks"])
            equal(
                [bool(r[k]) for k in ("is_installed", "is_renting", "is_returning")],
                [s["installed"], s["renting"], s["returning"]],
            )
            reported = (
                datetime.fromtimestamp(r["last_reported"], UTC)
                if raw["station_status"]["version"] == "2.3"
                else datetime.fromisoformat(r["last_reported"].replace("Z", "+00:00"))
            )
            if (
                reported > as_of
                or reported
                + timedelta(seconds=min(60, p["gbfs_feeds"]["station_status"]["ttl"]))
                <= as_of
            ):
                raise ValueError("Independent oracle accepted stale/future station")
    return {
        "feeds": feeds,
        "computed_feed_rejections": computed,
        "raw_state_stations_checked": len(scenario["stations"]) if scenario else 0,
        "forecast_published_at": published.isoformat(),
    }


def replay(
    source: dict[str, Any],
    config: dict[str, Any],
    events: list[list[Any]],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    stations = {s["station_id"]: s for s in source["stations"]}
    inventory = {k: s["bikes"] for k, s in stations.items()}
    free = {k: s["docks"] for k, s in stations.items()}
    blocked = {k: s["capacity"] - s["bikes"] - s["docks"] for k, s in stations.items()}
    accepted = {k: {"departures": 0, "arrivals": 0} for k in stations}
    denied = {
        k: {
            "departures": 0,
            "arrivals": 0,
            "closed_departures": 0,
            "closed_arrivals": 0,
        }
        for k in stations
    }
    empty = {
        k: s["installed"] and s["renting"] and inventory[k] == 0
        for k, s in stations.items()
    }
    full = {
        k: s["installed"] and s["returning"] and free[k] == 0
        for k, s in stations.items()
    }
    cargo = [0 for _ in plan["transfers"]]
    initial = sum(inventory.values())
    flow = 0
    checks = 0

    def reconcile() -> None:
        nonlocal checks
        if sum(inventory.values()) + sum(cargo) != initial + flow:
            raise ValueError("Independent oracle conservation violation")
        for k, s in stations.items():
            if (
                min(inventory[k], free[k], blocked[k]) < 0
                or inventory[k] + free[k] + blocked[k] != s["capacity"]
            ):
                raise ValueError("Independent oracle capacity violation")
            empty[k] |= bool(s["installed"] and s["renting"] and inventory[k] == 0)
            full[k] |= bool(s["installed"] and s["returning"] and free[k] == 0)
        checks += 1

    cost = 0.0
    out: Counter[str] = Counter()
    incoming: Counter[str] = Counter()
    vehicles = 0
    delivery = []
    pairs = set()
    for i, t in enumerate(plan["transfers"]):
        a, b = stations[t["source"]], stations[t["destination"]]
        if t["source"] == t["destination"] or (t["source"], t["destination"]) in pairs:
            raise ValueError("Independent oracle endpoint mismatch")
        pairs.add((t["source"], t["destination"]))
        if not all((a["installed"], a["renting"], b["installed"], b["returning"])):
            raise ValueError("Independent oracle operating mismatch")
        if (
            type(t["bikes"]) is not int
            or type(t["vehicles"]) is not int
            or not 0 < t["bikes"] <= t["vehicles"] * config["vehicle_capacity"]
        ):
            raise ValueError("Independent oracle transport capacity mismatch")
        lat1, lat2 = [math.radians(s["lat"]) for s in (a, b)]
        angle = (
            math.sin((lat2 - lat1) / 2) ** 2
            + math.cos(lat1)
            * math.cos(lat2)
            * math.sin(math.radians(b["lon"] - a["lon"]) / 2) ** 2
        )
        km = 12742.0176 * math.atan2(
            math.sqrt(max(0, angle)), math.sqrt(max(0, 1 - angle))
        )
        duration = config["handling_minutes"] + 60 * km / config["speed_kmh"]
        equal(t["distance_km"], km)
        equal(t["minutes"], duration)
        lead = (
            datetime.fromisoformat(source["start"])
            - datetime.fromisoformat(source["as_of"])
        ).total_seconds() / 60
        if not 0 <= duration < lead + config["hours"] * 60:
            raise ValueError("Independent oracle transport horizon violation")
        cost += (
            km * t["bikes"] * config["cost_per_bike_km"]
            + t["vehicles"] * config["cost_per_vehicle"]
        )
        vehicles += t["vehicles"]
        out[t["source"]] += t["bikes"]
        incoming[t["destination"]] += t["bikes"]
        cargo[i] = t["bikes"]
        inventory[t["source"]] -= t["bikes"]
        free[t["source"]] += t["bikes"]
        delivery.append([duration, 0, t["destination"], "delivery", i])
    if vehicles > config["vehicles"] or any(
        out[k] > s["bikes"] or incoming[k] > s["docks"] for k, s in stations.items()
    ):
        raise ValueError("Independent oracle aggregate transport violation")
    reconcile()
    delivered = 0
    attempts: Counter[tuple[str, str]] = Counter()
    for _, _, sid, kind, j in sorted(
        delivery + [[e[0], 1, e[1], e[2], e[3]] for e in events]
    ):
        s = stations[sid]
        if kind == "delivery":
            n = min(free[sid], cargo[j])
            inventory[sid] += n
            free[sid] -= n
            cargo[j] -= n
            delivered += n
        else:
            attempts[(sid, kind)] += 1
            operating = (
                s["installed"] and s["renting" if kind == "departures" else "returning"]
            )
            allowed = inventory[sid] > 0 if kind == "departures" else free[sid] > 0
            if operating and allowed:
                delta = 1 if kind == "arrivals" else -1
                inventory[sid] += delta
                free[sid] -= delta
                flow += delta
                accepted[sid][kind] += 1
            else:
                denied[sid][kind] += 1
                if not operating:
                    denied[sid]["closed_" + kind] += 1
        reconcile()
    if any(
        accepted[k][kind] + denied[k][kind] != attempts[(k, kind)]
        for k in stations
        for kind in ("departures", "arrivals")
    ):
        raise ValueError("Independent oracle attempt accounting mismatch")
    lost = [
        sum(r[kind] for r in denied.values()) for kind in ("departures", "arrivals")
    ]
    return {
        "accepted": accepted,
        "rejected": denied,
        "final_bikes": inventory,
        "ever_empty": empty,
        "ever_full": full,
        "cargo_end": cargo,
        "unserved_departures": lost[0],
        "unserved_arrivals": lost[1],
        "unserved_total": sum(lost),
        "attempts": len(events),
        "planned_moved": sum(t["bikes"] for t in plan["transfers"]),
        "delivered": delivered,
        "undelivered": sum(cargo),
        "movement_cost": cost,
        "scenario_objective": sum(lost) + config["cost_weight"] * cost,
        "constraint_violations": 0,
    }, checks


def verify(path: Path, output: Path | None = None) -> dict[str, Any]:
    before = time.perf_counter()
    report = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in report["checksums"].items():
        p = Path(name)
        if p.is_absolute() or ".." in p.parts or sha(path / p) != expected:
            raise ValueError("Independent oracle artifact checksum mismatch")
    request = json.loads((path / "inputs.json").read_text(encoding="utf-8"))
    source_audit = input_oracle(request)
    identifier = hashlib.sha256(
        json.dumps(request, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    equal(report["id"], identifier)
    if request["scenario"] is None:
        equal(report["status"], "unavailable")
        equal(report["gate"]["risk"], None)
        equal(report["comparisons"], [])
        result = {
            "status": "PASS",
            "real_e2e": "NOT VERIFIED",
            "result_id": identifier,
            "input_gate": report["gate"],
            "paired_draws": 0,
            "outcomes": 0,
            "reason": "Unavailable input is preserved, not simulated zero risk",
        }
    else:
        config, source = request["config"], request["scenario"]
        plans = json.loads((path / "plans.json").read_text(encoding="utf-8"))
        comparisons = {(r["scale"], r["order"]): r for r in report["comparisons"]}
        group: dict[tuple[float, str, str], list[dict[str, Any]]] = {}
        draws, outcomes, checks = 0, 0, 0
        with (
            (path / "events.jsonl").open(encoding="utf-8") as events_file,
            (path / "outcomes.jsonl").open(encoding="utf-8") as results_file,
        ):
            for scale in config["scales"]:
                for order in config["orders"]:
                    for repeat in range(config["repeats"]):
                        e = json.loads(next(events_file))
                        digest = hashlib.sha256(
                            json.dumps(
                                e["events"], sort_keys=True, ensure_ascii=False
                            ).encode()
                        ).hexdigest()
                        equal(
                            [e["scale"], e["order"], e["repeat"]],
                            [scale, order, repeat],
                        )
                        equal(e["events_sha256"], digest)
                        draws += 1
                        for policy in ("no_action", "heuristic", "optimizer"):
                            plan = plans[f"{scale:g}:{policy}"]
                            if plan.get("status") == "unavailable":
                                continue
                            r = json.loads(next(results_file))
                            equal(
                                [
                                    r["scale"],
                                    r["order"],
                                    r["repeat"],
                                    r["policy"],
                                    r["events_sha256"],
                                ],
                                [scale, order, repeat, policy, digest],
                            )
                            expected, n = replay(source, config, e["events"], plan)
                            equal(r["outcome"], expected)
                            equal(plan["movement_cost"], expected["movement_cost"])
                            group.setdefault((scale, order, policy), []).append(
                                expected
                            )
                            outcomes += 1
                            checks += n
            if events_file.read().strip() or results_file.read().strip():
                raise ValueError("Independent oracle unexpected extra rows")
        for (scale, order, policy), rows in group.items():
            summary = comparisons[(scale, order)]["policies"][policy]
            equal(summary["repeats"], config["repeats"])
            equal(summary["constraint_violations"], 0)
            for field, observed in summary["totals"].items():
                values = sorted(r[field] for r in rows)
                n = len(values)
                equal(
                    observed,
                    {
                        "mean": statistics.fmean(values),
                        "min": values[0],
                        "max": values[-1],
                        "p05": values[int((n - 1) * 0.05)],
                        "p95": values[int((n - 1) * 0.95)],
                    },
                )
            for s in source["stations"]:
                for kind in ("empty", "full"):
                    count = sum(r["ever_" + kind][s["station_id"]] for r in rows)
                    risk = summary["station_risk"][s["station_id"]][kind]
                    operating = (
                        s["installed"]
                        and s["renting" if kind == "empty" else "returning"]
                    )
                    if not operating:
                        equal(risk, None)
                        continue
                    equal(risk["outcomes"], count)
                    equal(risk["fraction"], count / len(rows))
                    equal(risk["repeats"], len(rows))
                    n, p = len(rows), count / len(rows)
                    equal(risk["mc_standard_error"], math.sqrt(p * (1 - p) / n))
                    z = 1.959963984540054
                    middle = (p + z * z / (2 * n)) / (1 + z * z / n)
                    half = (
                        z
                        * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
                        / (1 + z * z / n)
                    )
                    equal(
                        risk["mc_wilson_95"],
                        [max(0.0, middle - half), min(1.0, middle + half)],
                    )
            for sid in source["exclusions"]:
                equal(summary["station_risk"][sid]["empty"], None)
                equal(summary["station_risk"][sid]["full"], None)
            base = comparisons[(scale, order)]["policies"]["no_action"]["totals"][
                "unserved_total"
            ]["mean"]
            equal(
                comparisons[(scale, order)]["simulated_unserved_gain_vs_no_action"][
                    policy
                ],
                base - statistics.fmean(r["unserved_total"] for r in rows),
            )
        result = {
            "status": "PASS",
            "result_id": identifier,
            "paired_draws": draws,
            "outcomes": outcomes,
            "inventory_ledger_checks": checks,
            "conservation_cost_risk_metric_mismatches": 0,
            "method": "Separate free-dock/inventory/cargo ledger; independent "
            "Haversine/cost, attempt accounting and summary metrics; "
            "no production helpers",
        }
    result["source_audit"] = source_audit
    result["elapsed_seconds"] = time.perf_counter() - before
    result["verifier_sha256"] = sha(Path(__file__))
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    return result
