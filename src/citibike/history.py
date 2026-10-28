"""F2 commands: discovery, ingestion, historical build, profiling and verification."""

import argparse
from pathlib import Path

from citibike.ingestion.inventory import discover, ingest_window
from citibike.processing.historical import prepare_inventory
from citibike.processing.history_publish import publish_history
from citibike.quality.history_report import analytical_markdown, evidence
from citibike.storage import read_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["inventory", "ingest", "build", "profile", "verify"]
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--ranged", action="store_true")
    parser.add_argument("--period")
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/f2-history.json")
    )
    parser.add_argument(
        "--report", type=Path, default=Path("docs/evidence/f2-analysis.md")
    )
    args = parser.parse_args()
    if args.command == "inventory":
        print(discover(args.data_dir))
        return
    if args.inventory is None:
        parser.error("--inventory is required")
    if args.command == "ingest":
        path = ingest_window(args.inventory, args.data_dir, 1, args.ranged, args.period)
        print(path)
        if not read_json(path)["complete"] and args.period is None:
            raise SystemExit("Historical ingestion incomplete")
    elif args.command == "build":
        states = prepare_inventory(args.inventory, args.data_dir, args.wait_seconds)
        print(
            publish_history(states, args.data_dir, read_json(args.inventory)["window"])
        )
    else:
        snapshot = args.snapshot or Path(
            read_json(args.data_dir / "f2/active.json")["path"]
        )
        manifest = read_json(snapshot / "manifest.json")
        base_version = manifest.get(
            "base_pipeline_version", manifest["pipeline_version"]
        )
        if args.command == "verify":
            states = [
                args.data_dir
                / "f2/states"
                / f"{p['source_object_id']}-{base_version}.json"
                for p in manifest["partitions"]
            ]
            verified = publish_history(
                states,
                args.data_dir,
                read_json(args.inventory)["window"],
                activate=False,
            )
            if verified != snapshot:
                raise ValueError("Snapshot identity mismatch")
        report = evidence(args.data_dir, args.inventory, snapshot, args.output)
        analytical_markdown(report, args.report)
        print(
            f"Verified {len(report['window'])} months; "
            f"input={report['input_rows']}; canonical={report['canonical_rows']}"
        )


if __name__ == "__main__":
    main()
