"""Operator commands for fixed isolated local staging; no public deploy path."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from citibike.release import staging
from citibike.release.benchmark import measure
from citibike.release.monitoring import check, watch
from citibike.release.recovery import backup, model_parity, restore
from citibike.serving.db import DatabaseSettings
from citibike.storage import read_json, write_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    deploy = sub.add_parser("deploy")
    deploy.add_argument("--release", required=True)
    deploy.add_argument("--approval", required=True, type=Path)
    deploy.add_argument("--no-build", action="store_true")
    sub.add_parser("rollback")
    seed = sub.add_parser("seed")
    seed.add_argument("--registry", required=True, type=Path)
    parity = sub.add_parser("model-parity")
    parity.add_argument("--restored", required=True, type=Path)
    parity.add_argument("--work", required=True, type=Path)
    save = sub.add_parser("backup")
    save.add_argument("--target", required=True, type=Path)
    recover = sub.add_parser("restore")
    recover.add_argument("--source", required=True, type=Path)
    recover.add_argument("--store", required=True, type=Path)
    monitor = sub.add_parser("monitor")
    monitor.add_argument("--backup", type=Path)
    monitor.add_argument("--watch", action="store_true")
    monitor.add_argument("--interval", type=float, default=60)
    monitor.add_argument("--samples", type=int)
    monitor.add_argument("--latency-budget-ms", type=float, default=1000)
    monitor.add_argument("--test-webhook", help="Isolated loopback HTTP test receiver")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--url", default="http://127.0.0.1:18000/v1/kpi")
    bench.add_argument("--requests", type=int, default=200)
    bench.add_argument("--concurrency", type=int, default=8)
    bench.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "init":
        staging.init()
        result: object = {"status": "initialized", "scope": "local_staging"}
    elif args.command == "deploy":
        result = staging.deploy(args.release, args.approval, build=not args.no_build)
    elif args.command == "rollback":
        result = staging.rollback()
    elif args.command == "seed":
        # Development DB from local .env, opened read-only; F4 active artifacts.
        models = read_json(Path("data/f4/active_models.json"))
        datasets = read_json(Path("data/f4/active_datasets.json"))
        result = staging.seed(
            DatabaseSettings(),
            {
                f"f4/models/{models['id']}": Path(models["path"]),
                f"f4/datasets/{datasets['id']}": Path(datasets["path"]),
                "registry": args.registry,
            },
        )
    elif args.command == "model-parity":
        models = read_json(Path("data/f4/active_models.json"))
        datasets = read_json(Path("data/f4/active_datasets.json"))
        result = model_parity(
            staging.ROOT / "store",
            args.restored,
            models["id"],
            datasets["id"],
            datetime.now(UTC),
            args.work,
        )
    elif args.command == "backup":
        result = {
            "backup": str(
                backup(
                    staging.compose(),
                    staging.owner(),
                    staging.ROOT / "store",
                    args.target,
                )
            )
        }
    elif args.command == "restore":
        _, result = restore(staging.compose(), staging.owner(), args.source, args.store)
    elif args.command == "monitor":
        if args.watch:
            watch(
                staging.owner(),
                staging.ROOT / "store",
                args.backup,
                interval=args.interval,
                samples=args.samples,
                local_webhook=args.test_webhook,
                latency_budget_ms=args.latency_budget_ms,
                log_command=staging.compose(),
            )
            return
        result = check(
            staging.owner(),
            staging.ROOT / "store",
            args.backup,
            local_webhook=args.test_webhook,
            latency_budget_ms=args.latency_budget_ms,
            log_command=staging.compose(),
        )
    else:
        result = measure(args.url, args.requests, args.concurrency)
        write_json(args.output, result)
        result = {k: v for k, v in result.items() if k != "observations_ms"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
