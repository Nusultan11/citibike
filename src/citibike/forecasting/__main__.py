"""F4 explicit batch workflow; optional ML group required for experiments."""

import argparse
import os
from pathlib import Path

from citibike.features.contracts import utc
from citibike.forecasting.artifacts import active
from citibike.storage import read_json


def main() -> None:
    # Bound native libraries before their first import.
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ[name] = "2"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=[
            "build",
            "weather",
            "backtest",
            "select",
            "train",
            "predict",
            "publish",
            "activate",
            "verify",
            "protocol",
            "research",
        ],
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--weather-json", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--model", type=Path)
    parser.add_argument("--forecast", type=Path)
    parser.add_argument("--origin")
    parser.add_argument("--station", action="append")
    parser.add_argument("--stations", type=int, default=64)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--final", action="store_true")
    parser.add_argument("--serving", action="store_true")
    parser.add_argument("--publication-id")
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/f4-forecasting.json")
    )
    args = parser.parse_args()
    root = args.data_dir
    result: object
    if args.command in ("protocol", "research"):
        from citibike.forecasting.experiment import research, research_protocol

        if args.final:
            parser.error("Corrective research is validation-only; --final forbidden")
        if args.command == "protocol":
            result = research_protocol(root, args.dataset or active(root, "datasets"))
        else:
            if args.protocol is None:
                parser.error("research requires a frozen --protocol path")
            result = research(root, args.protocol)
            from citibike.forecasting.artifacts import checked

            if checked(result)["status"] != "completed":
                raise SystemExit(f"PARTIAL research; immutable outcomes: {result}")
    elif args.command == "build":
        from citibike.features.dataset import build

        gold = args.gold or Path(read_json(root / "f3/active.json")["path"])
        result = build(root, gold, station_count=args.stations, smoke=args.smoke)
    elif args.command == "weather":
        from citibike.forecasting.evaluation_weather import prepare

        result = prepare(
            root, args.dataset or active(root, "datasets"), args.weather_json
        )
    elif args.command == "backtest":
        from citibike.forecasting.experiment import backtest, final_test

        result = (
            final_test(root, args.selection or active(root, "selections"))
            if args.final
            else backtest(root, args.dataset or active(root, "datasets"))
        )
    elif args.command == "select":
        from citibike.forecasting.experiment import select

        result = select(root, args.experiment or active(root, "experiments"))
    elif args.command == "train":
        from citibike.forecasting.inference import train

        result = train(root, args.selection or active(root, "selections"))
    elif args.command == "predict":
        from citibike.forecasting.inference import predict

        result = predict(
            root,
            args.model or active(root, "models"),
            utc(args.origin) if args.origin else None,
            args.station,
        )
    elif args.command == "publish":
        from citibike.serving.forecast_publish import publish

        result = publish(args.forecast or active(root, "forecasts"))
    elif args.command == "activate":
        from citibike.serving.db import connect
        from citibike.serving.forecast_publish import activate as activate_forecast

        if not args.publication_id:
            parser.error("--publication-id required")
        with connect() as db:
            activate_forecast(db, args.publication_id)
        result = args.publication_id
    else:
        from citibike.forecasting.verify import verify

        result = verify(root, args.output, serving=args.serving)
    print(result)


if __name__ == "__main__":
    main()
