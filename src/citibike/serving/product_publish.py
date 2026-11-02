"""F6 publication of verified station geo and F5 scenario results to PostgreSQL.

Both are read models of already immutable, verified artifacts. HTTP requests only
read these tables; simulations, Silver scans and verification run here, offline.
"""

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import psycopg
from psycopg.types.json import Jsonb

from citibike.analytics.station_geo import checked as checked_geo
from citibike.serving.analytics_publish import migrate as migrate_analytics
from citibike.serving.db import DatabaseSettings, connect

MIGRATION = "006_product.sql"
POLICIES = ("no_action", "heuristic", "optimizer")
STATE = ("gbfs_id", "capacity", "bikes", "docks", "lat", "lon")


def migrate(db: psycopg.Connection[dict[str, Any]]) -> None:
    migrate_analytics(db)
    content = (Path(__file__).with_name("migrations") / MIGRATION).read_text(
        encoding="utf-8"
    )
    digest = hashlib.sha256(content.encode()).hexdigest()
    row = db.execute(
        "SELECT checksum FROM schema_migrations WHERE version=%s", (MIGRATION[:3],)
    ).fetchone()
    if row:
        if row["checksum"] != digest:
            raise ValueError("Applied product migration checksum differs")
        return
    db.execute(content)
    db.execute(
        "INSERT INTO schema_migrations(version,checksum) VALUES(%s,%s)",
        (MIGRATION[:3], digest),
    )


def publish_geo(path: Path, settings: DatabaseSettings | None = None) -> str:
    report = checked_geo(path)
    identifier: str = report["id"]
    with duckdb.connect() as local:
        cursor = local.execute(
            "SELECT station_id, local_month || '-01', event_type, lat, lon,"
            " station_name, events, coordinate_rows, modal_rows,"
            " distinct_coordinates FROM read_parquet(?) ORDER BY 1, 2, 3",
            [str(path / "station_geo_monthly.parquet")],
        )
        rows = cursor.fetchall()
    if len(rows) != report["rows"]:
        raise ValueError("Station geo row count mismatch")
    with connect(settings) as db:
        db.execute("SELECT pg_advisory_xact_lock(7061646)")
        migrate(db)
        analytics = db.execute(
            "SELECT manifest->>'source_snapshot_id' snapshot FROM"
            " analytics_publications WHERE publication_id=%s",
            (report["analytics_publication_id"],),
        ).fetchone()
        if analytics is None or analytics["snapshot"] != report["source_snapshot_id"]:
            raise ValueError("Station geo is not derived from a published F3 version")
        old = db.execute(
            "SELECT manifest FROM station_geo_publications WHERE geo_id=%s",
            (identifier,),
        ).fetchone()
        if old:
            if old["manifest"] != report:
                raise ValueError("Immutable station geo manifest changed")
            return identifier
        db.execute(
            "INSERT INTO station_geo_publications(geo_id,analytics_publication_id,"
            "manifest) VALUES(%s,%s,%s)",
            (identifier, report["analytics_publication_id"], Jsonb(report)),
        )
        with db.cursor().copy("COPY station_geo_monthly FROM STDIN") as copier:
            for row in rows:
                copier.write_row([identifier, *row])
        actual = db.execute(
            "SELECT count(*) n, coalesce(sum(events),0) events FROM"
            " station_geo_monthly WHERE geo_id=%s",
            (identifier,),
        ).fetchone()
        if actual is None or (actual["n"], actual["events"]) != (
            report["rows"],
            report["station_events"],
        ):
            raise ValueError("PostgreSQL station geo reconciliation failed")
    return identifier


def risk_records(path: Path) -> dict[str, Any]:
    """Validate an F5 result with the existing gates and derive read-model rows."""
    from citibike.risk.run import checked
    from citibike.risk.verify import verify

    manifest = checked(path)
    if manifest["status"] != "ok" or manifest["id"] != path.name:
        raise ValueError("Only verified successful F5 results are publishable")
    oracle = verify(path)
    if oracle.get("status") != "PASS":
        raise ValueError("Independent F5 oracle failed; publication blocked")
    request = json.loads((path / "inputs.json").read_text(encoding="utf-8"))
    plans = json.loads((path / "plans.json").read_text(encoding="utf-8"))
    scenario = request["scenario"]
    gate = manifest["gate"]
    coverage = gate["coverage"]
    stations = [
        {
            "station_id": s["station_id"],
            "eligible": True,
            "reasons": [],
            **{k: s[k] for k in STATE},
        }
        for s in scenario["stations"]
    ]
    stations += [
        {
            "station_id": station,
            "eligible": False,
            "reasons": sorted(set(reasons)),
            **dict.fromkeys(STATE),
        }
        for station, reasons in sorted(coverage["exclusions"].items())
    ]
    if (
        len({s["station_id"] for s in stations}) != len(stations)
        or len(stations) != coverage["requested"]
        or len(scenario["stations"]) != coverage["eligible"]
    ):
        raise ValueError("F5 station coverage does not reconcile")

    def plan(scale: float, policy: str) -> dict[str, Any]:
        found: list[dict[str, Any]] = [
            v for k, v in plans.items() if float(k.split(":")[0]) == scale
        ]
        selected = [v for v in found if v["policy"] == policy]
        if len(selected) != 1:
            raise ValueError("Missing unique F5 plan")
        return selected[0]

    outcomes, station_outcomes, transfers = [], [], []
    for cell in manifest["comparisons"]:
        scale, order = float(cell["scale"]), cell["order"]
        if sorted(cell["policies"]) != sorted(POLICIES):
            raise ValueError("F5 comparison must contain all three policies")
        for policy in POLICIES:
            value = cell["policies"][policy]
            chosen = plan(scale, policy)
            outcomes.append(
                {
                    "scale": scale,
                    "event_order": order,
                    "policy": policy,
                    "repeats": value["repeats"],
                    "totals": value["totals"],
                    "movement_cost": float(chosen["movement_cost"]),
                    "moved_bikes": sum(t["bikes"] for t in chosen["transfers"]),
                    "vehicles": sum(t["vehicles"] for t in chosen["transfers"]),
                    "constraint_violations": value["constraint_violations"],
                    "gain_vs_no_action": cell["simulated_unserved_gain_vs_no_action"][
                        policy
                    ],
                    "solver": chosen["solver"],
                }
            )
            for station, risk in sorted(value["station_risk"].items()):
                empty, full = risk.get("empty") or {}, risk.get("full") or {}
                station_outcomes.append(
                    {
                        "scale": scale,
                        "event_order": order,
                        "policy": policy,
                        "station_id": station,
                        "status": risk["status"],
                        "empty_fraction": empty.get("fraction"),
                        "empty_low": (empty.get("mc_wilson_95") or [None])[0],
                        "empty_high": (empty.get("mc_wilson_95") or [None, None])[1],
                        "full_fraction": full.get("fraction"),
                        "full_low": (full.get("mc_wilson_95") or [None])[0],
                        "full_high": (full.get("mc_wilson_95") or [None, None])[1],
                        "repeats": value["repeats"],
                    }
                )
    for key, chosen in sorted(plans.items()):
        if chosen["policy"] == "no_action":
            continue
        for sequence, t in enumerate(chosen["transfers"]):
            transfers.append(
                {
                    "scale": float(key.split(":")[0]),
                    "policy": chosen["policy"],
                    "sequence": sequence,
                    **{
                        k: t[k]
                        for k in (
                            "source",
                            "destination",
                            "bikes",
                            "vehicles",
                            "minutes",
                            "distance_km",
                        )
                    },
                }
            )
    created = datetime.fromisoformat(manifest["created_at"])
    as_of = datetime.fromisoformat(gate["as_of"])
    feeds = scenario["provenance"].get("gbfs_feeds", {})
    public = {
        "publication_id": manifest["id"],
        "version": manifest["version"],
        "status": manifest["status"],
        "created_at": manifest["created_at"],
        "data_as_of": manifest["data_as_of"],
        "data_mode": manifest["data_mode"],
        "mode": "recorded_as_of_replay" if created > as_of else "contemporaneous",
        "horizon_start": gate["start"],
        "horizon_end": gate["end"],
        "ttl_seconds": min(
            [60, *(f["ttl"] for f in feeds.values() if f.get("ttl", 0) > 0)]
        ),
        "coverage": {k: coverage[k] for k in ("requested", "eligible", "fraction")},
        "config": request["config"],
        "assumptions": manifest["assumptions"],
        "operational_promotion": manifest["operational_promotion"],
        "provenance": {
            k: v for k, v in scenario["provenance"].items() if k != "gbfs_feeds"
        },
        "gbfs_feeds": {
            name: {
                k: f.get(k)
                for k in ("version", "last_updated", "ttl", "received_at_utc", "url")
            }
            for name, f in feeds.items()
        },
        "checksums": manifest["checksums"],
        "measurements": manifest["measurements"],
        "verification": {
            k: oracle[k]
            for k in (
                "status",
                "paired_draws",
                "outcomes",
                "inventory_ledger_checks",
                "conservation_cost_risk_metric_mismatches",
            )
            if k in oracle
        },
        "objective": "simulated unserved attempts + cost_weight * movement cost",
    }
    return {
        "manifest": public,
        "stations": stations,
        "outcomes": outcomes,
        "station_outcomes": station_outcomes,
        "transfers": transfers,
    }


def activate_risk(db: psycopg.Connection[dict[str, Any]], identifier: str) -> None:
    db.execute(
        """INSERT INTO risk_active VALUES(true,%s) ON CONFLICT(singleton)
        DO UPDATE SET publication_id=EXCLUDED.publication_id""",
        (identifier,),
    )


def publish_risk(
    path: Path,
    settings: DatabaseSettings | None = None,
    *,
    fail_before_activate: bool = False,
) -> str:
    records = risk_records(path)
    public = records["manifest"]
    identifier: str = public["publication_id"]
    tables = {
        "risk_stations": (
            "station_id",
            "eligible",
            "reasons",
            "gbfs_id",
            "capacity",
            "bikes",
            "docks",
            "lat",
            "lon",
        ),
        "risk_policy_outcomes": (
            "scale",
            "event_order",
            "policy",
            "repeats",
            "totals",
            "movement_cost",
            "moved_bikes",
            "vehicles",
            "constraint_violations",
            "gain_vs_no_action",
            "solver",
        ),
        "risk_station_outcomes": (
            "scale",
            "event_order",
            "policy",
            "station_id",
            "status",
            "empty_fraction",
            "empty_low",
            "empty_high",
            "full_fraction",
            "full_low",
            "full_high",
            "repeats",
        ),
        "risk_transfers": (
            "scale",
            "policy",
            "sequence",
            "source",
            "destination",
            "bikes",
            "vehicles",
            "minutes",
            "distance_km",
        ),
    }
    sources = {
        "risk_stations": "stations",
        "risk_policy_outcomes": "outcomes",
        "risk_station_outcomes": "station_outcomes",
        "risk_transfers": "transfers",
    }
    with connect(settings) as db:
        db.execute("SELECT pg_advisory_xact_lock(7061647)")
        migrate(db)
        old = db.execute(
            "SELECT manifest FROM risk_publications WHERE publication_id=%s",
            (identifier,),
        ).fetchone()
        if old:
            if old["manifest"] != json.loads(json.dumps(public, default=str)):
                raise ValueError("Immutable risk manifest changed")
        else:
            db.execute(
                "INSERT INTO risk_publications(publication_id,manifest) VALUES(%s,%s)",
                (identifier, Jsonb(public)),
            )
            for table, columns in tables.items():
                for row in records[sources[table]]:
                    values = [
                        Jsonb(row[c]) if c in ("totals", "solver") else row[c]
                        for c in columns
                    ]
                    db.execute(
                        f"INSERT INTO {table}(publication_id,{','.join(columns)}) "
                        f"VALUES(%s{',%s' * len(columns)})",
                        [identifier, *values],
                    )
        for table in tables:
            found = db.execute(
                f"SELECT count(*) n FROM {table} WHERE publication_id=%s",
                (identifier,),
            ).fetchone()
            if found is None or found["n"] != len(records[sources[table]]):
                raise ValueError("PostgreSQL risk reconciliation failed")
        if fail_before_activate:
            raise RuntimeError("Injected failure before risk activation")
        activate_risk(db, identifier)
    return identifier


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stations", "risk"))
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args()
    identifier = (
        publish_geo(args.path)
        if args.command == "stations"
        else publish_risk(args.path)
    )
    print(json.dumps({"published": args.command, "id": identifier}))


if __name__ == "__main__":
    main()
