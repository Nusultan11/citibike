"""Real-official F7 CLI protocol; disposable DB, immutable F4, no training."""

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from psycopg import sql

from citibike.automation.config import Config
from citibike.automation.registry import client, lineage
from citibike.ingestion.archive import checksum
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--work-dir", type=Path, default=Path(".cache/f7-real"))
    parser.add_argument(
        "--output", type=Path, default=Path(".cache/f7-real-proof.json")
    )
    args = parser.parse_args()
    # These imports follow automation's local-only telemetry defaults.
    from prefect.settings import get_current_settings
    from prefect.testing.utilities import prefect_test_harness

    root = args.work_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    pointers = {
        str(p): p.read_bytes()
        for p in args.data_dir.rglob("active*.json")
        if "automation" not in p.parts
    }
    model_path = Path(
        read_json(args.data_dir / "f4/active_models.json")["path"]
    ).resolve()
    config = Config(
        data_dir=root,
        model_bundle=model_path,
        fallback_bundle=model_path,
        mode="real_official",
    )
    config_path = root / "config.json"
    write_json(config_path, config.model_dump(mode="json"))
    admin = DatabaseSettings()
    name = "bikeflow_test_" + uuid4().hex
    with connect(admin) as db:
        db.autocommit = True
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    original_database = os.environ.get("PGDATABASE")
    results: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "real_official",
        "scope": (
            "existing immutable F4 + fresh source requests; isolated DB/Registry; "
            "no model fit or full history transform"
        ),
        "base_model_path": str(model_path),
        "base_model_manifest_sha256": checksum(model_path / "manifest.json"),
        "commands": [],
    }
    try:
        os.environ["PGDATABASE"] = name
        with prefect_test_harness(server_startup_timeout=90):
            environment = {
                **os.environ,
                "PREFECT_API_URL": str(get_current_settings().api.url),
            }

            def command(operation: str) -> dict[str, Any]:
                invocation = [
                    sys.executable,
                    "-m",
                    "citibike.automation",
                    "run",
                    "--config",
                    str(config_path),
                    "--operation",
                    operation,
                ]
                before = datetime.now(UTC)
                completed = subprocess.run(
                    invocation,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=360,
                )
                (
                    root / f"{operation}-{len(results['commands'])}.stderr.log"
                ).write_text(completed.stderr, encoding="utf-8")
                result: dict[str, Any] = json.loads(completed.stdout)
                results["commands"].append(
                    {
                        "command": (
                            "python -m citibike.automation run "
                            "--config <isolated-real-config> --operation "
                        )
                        + operation,
                        "returncode": completed.returncode,
                        "started_at": before.isoformat(),
                        "result": result,
                    }
                )
                return result

            first = command("forecast")
            repeat = command("forecast")
            results["forecast_idempotent"] = (
                first.get("id") is not None
                and first.get("id") == repeat.get("id")
                and repeat.get("status") == "unchanged"
            )
            command("review")
            # No synthetic freshness is introduced if official feeds are stale.
            command("gbfs")
            # An empty local warehouse must fail the cold-bootstrap budget before
            # any 24-month download/preprocessing; discovery itself is real.
            command("trips")
            with connect(readonly=True) as db:
                results["forecast_publications"] = db.execute(
                    "SELECT count(*) n FROM ml_forecast_publications"
                ).fetchone()
                results["forecast_active"] = db.execute(
                    "SELECT publication_id FROM ml_forecast_active"
                ).fetchone()
            api = client(config.state_dir / "registry")
            versions = list(api.search_model_versions("name='bikeflow-f4'"))
            results["registry"] = {
                "versions": [
                    {"version": v.version, "run_id": v.run_id} for v in versions
                ],
                "aliases": api.get_registered_model("bikeflow-f4").aliases,
                "lineage": lineage(
                    config.state_dir / "registry", "bikeflow-f4", versions[0].version
                ),
            }
            results["source_inventory"] = [
                {
                    "path": str(p),
                    "sha256": checksum(p),
                    "checked_at": read_json(p)["checked_at"],
                    "window": read_json(p)["window"],
                    "source_url": read_json(p)["source_url"],
                    "objects": len(read_json(p)["objects"]),
                }
                for p in (root / "inventory").glob("*/inventory.json")
            ]
            results["events"] = [
                read_json(p)
                for p in (config.state_dir / "monitoring/events").glob("*.json")
            ]
        results["original_active_pointers_unchanged"] = all(
            Path(p).read_bytes() == value for p, value in pointers.items()
        )
        results["original_active_pointer_count"] = len(pointers)
        results["accuracy"] = (
            "pending_actuals; existing validation metrics remain "
            "approximate retrospective"
        )
        results["production_readiness"] = "NOT VERIFIED; local technical checks only"
        write_json(args.output, results)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "forecast_idempotent": results["forecast_idempotent"],
                    "original_pointers_unchanged": results[
                        "original_active_pointers_unchanged"
                    ],
                    "commands": [
                        {
                            "returncode": r["returncode"],
                            "status": r["result"].get("status"),
                        }
                        for r in results["commands"]
                    ],
                },
                indent=2,
            )
        )
    finally:
        if original_database is None:
            os.environ.pop("PGDATABASE", None)
        else:
            os.environ["PGDATABASE"] = original_database
        with connect(admin) as db:
            db.autocommit = True
            db.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    main()
