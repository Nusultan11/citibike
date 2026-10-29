"""F3 batch CLI: build, publish, verify and benchmark analytical aggregates."""

import argparse
from pathlib import Path

from citibike.analytics.build import build


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["build", "publish", "verify", "benchmark", "activate"]
    )
    parser.add_argument("--publication-id")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--gold", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument(
        "--output", type=Path, default=Path("docs/evidence/f3-analytics.json")
    )
    args = parser.parse_args()
    if args.command == "build":
        print(build(args.data_dir, args.snapshot, prepare_only=args.prepare_only))
    elif args.command == "publish":
        from citibike.serving.analytics_publish import active_path, publish

        print(publish(args.gold or active_path(args.data_dir)))
    elif args.command == "activate":
        from citibike.serving.analytics_publish import activate
        from citibike.serving.db import connect

        if not args.publication_id:
            parser.error("activate requires --publication-id of a published version")
        with connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(7061643)")
            activate(db, args.publication_id)
        print(args.publication_id)
    else:
        from citibike.analytics.verify import verify
        from citibike.serving.analytics_publish import active_path

        print(
            verify(
                args.gold or active_path(args.data_dir),
                args.output,
                benchmark=args.command == "benchmark",
            )
        )


if __name__ == "__main__":
    main()
