"""Controlled CLI protocol assertions; never substitutes real-data evidence."""

import argparse
from pathlib import Path

from citibike.serving.analytics_publish import validate_gold
from citibike.storage import read_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--changed", action="store_true")
    args = parser.parse_args()
    root = args.data_dir
    active = read_json(root / "f3/active.json")
    report = validate_gold(Path(active["path"]))
    assert report["data_mode"] == "controlled_fixture"
    assert sum(p["metrics"]["n"] for p in report["months"]) == (
        25 if args.changed else 24
    )
    runs = sorted((root / "f3/runs").glob("*.json"))
    latest = read_json(runs[-1])
    if args.changed:
        assert report["affected_months"] == ["2026-04"]
        assert latest["mode"] == "incremental" and latest["rebuilt_months"] == 1
    else:
        assert latest["mode"] == "unchanged" and latest["rebuilt_months"] == 0
    print(
        {
            "publication_id": report["id"],
            "latest_run": latest,
            "all_checksums_and_oracles": "passed",
        }
    )


if __name__ == "__main__":
    main()
