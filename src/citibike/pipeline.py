"""One-shot F1 pipeline. Publication only follows every local quality gate."""

import argparse
import json
from pathlib import Path

from citibike.ingestion.archive import download, ingest_fixture
from citibike.processing.trips import process, verify_artifacts
from citibike.serving.publish import publish


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument(
        "--fixture",
        type=Path,
        help="Controlled tests only; metadata labels it as fixture",
    )
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--process-only", action="store_true")
    args = parser.parse_args()
    bronze = (
        ingest_fixture(args.fixture, args.period, args.data_dir)
        if args.fixture
        else download(args.url, args.period, args.data_dir)
    )
    result = process(bronze, args.data_dir)
    if not args.process_only:
        publish(result)
    report = verify_artifacts(result)
    print(
        json.dumps(
            {
                "dataset_id": report["dataset_id"],
                "metrics": report["metrics"],
                "dispositions": report["dispositions"],
                "result": str(result),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
