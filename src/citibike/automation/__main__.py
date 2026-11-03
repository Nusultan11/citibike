"""F7 manual runs, explicit scheduler and local Registry inspection."""

import argparse
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

from citibike.automation.config import Config, load
from citibike.storage import read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "init",
            "versions",
            "run",
            "serve",
            "status",
            "registry",
            "lineage",
            "registry-ui",
        ),
    )
    parser.add_argument(
        "--config", type=Path, default=Path("data/automation/config.json")
    )
    parser.add_argument(
        "--operation",
        choices=("trips", "gbfs", "forecast", "risk", "review"),
        default="review",
    )
    parser.add_argument("--test-interval", type=float)
    parser.add_argument(
        "--only", choices=("trips", "gbfs", "forecast", "risk", "review")
    )
    parser.add_argument("--name", default="bikeflow-f4")
    parser.add_argument("--version")
    args = parser.parse_args()
    if args.command == "init":
        if args.config.exists():
            parser.error("config already exists; explicit edits required")
        write_json(args.config, Config().model_dump(mode="json"))
        print(args.config)
        return
    if args.command == "versions":
        print(
            json.dumps(
                {
                    p: importlib.metadata.version(p)
                    for p in ("prefect", "mlflow", "psutil", "filelock")
                },
                indent=2,
            )
        )
        return
    config = load(args.config)
    if args.command in {"run", "serve"}:
        from citibike.automation.flows import FLOWS, scheduler

        if args.command == "serve":
            scheduler(args.config, test_interval=args.test_interval, only=args.only)
            return
        try:
            print(
                json.dumps(
                    FLOWS[args.operation](str(args.config.resolve())),
                    indent=2,
                    default=str,
                )
            )
        except Exception:
            path = config.state_dir / "monitoring" / f"{args.operation}.json"
            print(
                json.dumps(
                    read_json(path)
                    if path.exists()
                    else {"status": "failed", "reason": "prefect_unavailable"},
                    indent=2,
                )
            )
            raise SystemExit(2) from None
    elif args.command == "status":
        print(
            json.dumps(
                [
                    read_json(p)
                    for p in sorted((config.state_dir / "monitoring").glob("*.json"))
                ],
                indent=2,
            )
        )
    elif args.command == "registry-ui":
        from citibike.automation.registry import uri

        root = config.state_dir / "registry"
        # Local-only inspection, no public server or account credentials.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "mlflow",
                "server",
                "--backend-store-uri",
                uri(root),
                "--default-artifact-root",
                (root.resolve() / "artifacts").as_uri(),
                "--host",
                "127.0.0.1",
                "--port",
                os.environ.get("BIKEFLOW_MLFLOW_PORT", "5000"),
            ],
            check=True,
        )
    else:
        from citibike.automation.registry import lineage, register

        root = config.state_dir / "registry"
        if args.command == "registry":
            if config.model_bundle is None:
                parser.error("model_bundle required in config")
            print(json.dumps(register(root, config.model_bundle, args.name), indent=2))
        else:
            if args.version is None:
                parser.error("--version required")
            print(
                json.dumps(
                    lineage(root, args.name, args.version), indent=2, default=str
                )
            )


if __name__ == "__main__":
    main()
