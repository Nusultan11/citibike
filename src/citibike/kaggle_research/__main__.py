"""Explicit CLI for portable Kaggle research, with no publish command."""

import argparse
from pathlib import Path

from citibike.kaggle_research.study import prepare, run, worker
from citibike.kaggle_research.verify import verify


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker", "verify"))
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.output))
    elif args.bundle is None:
        parser.error("--bundle required")
    elif args.command == "run":
        print(run(args.bundle, args.output))
    elif args.command == "worker" and args.request is not None:
        worker(args.bundle, args.request, args.output)
    elif args.command == "verify" and args.result is not None:
        print(verify(args.bundle, args.result, args.output))
    else:
        parser.error("Missing --request or --result")


if __name__ == "__main__":
    main()
