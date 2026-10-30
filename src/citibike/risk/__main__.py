"""Run/verify bounded scenario comparisons; never train or publish F4 forecasts."""

import argparse
import json
from pathlib import Path

from citibike.features.contracts import utc
from citibike.forecasting.artifacts import active
from citibike.risk import Config
from citibike.risk.inputs import load
from citibike.risk.run import checked, execute
from citibike.storage import read_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "verify"))
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--forecast", type=Path)
    parser.add_argument("--gbfs", type=Path)
    parser.add_argument("--as-of")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--stations", nargs="+")
    parser.add_argument("--result", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        from citibike.risk.verify import verify

        if args.result is None:
            parser.error("verify requires --result")
        result = verify(args.result, args.output)
        print(json.dumps(result, default=str))
    else:
        if not args.as_of:
            parser.error("run requires explicit timezone-aware --as-of")
        config = Config(**(read_json(args.config) if args.config else {}))
        forecast = args.forecast or (
            active(args.data_dir, "forecasts")
            if (args.data_dir / "f4/active_forecasts.json").exists()
            else args.data_dir / "f4/missing_forecast"
        )
        gbfs = args.gbfs or (
            Path(read_json(args.data_dir / "gbfs/active.json")["path"])
            if (args.data_dir / "gbfs/active.json").exists()
            else args.data_dir / "gbfs/missing_collection"
        )
        scenario, gate = load(
            forecast, gbfs, utc(args.as_of), config, requested=args.stations
        )
        destination = execute(args.data_dir, scenario, gate, config)
        report = checked(destination)
        print(
            json.dumps(
                {
                    "path": str(destination),
                    "id": report["id"],
                    "status": report["status"],
                    "gate": gate,
                },
                default=str,
            )
        )
        if report["status"] != "ok":
            raise SystemExit(2)


if __name__ == "__main__":
    main()
