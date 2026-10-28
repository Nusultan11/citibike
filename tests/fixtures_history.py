"""Explicitly synthetic 24-month history; no official data claims or network."""

import argparse
from pathlib import Path

from citibike.ingestion.archive import ingest_fixture
from citibike.ingestion.inventory import shift_month
from citibike.storage import identity, read_json, write_json
from tests.fixtures import ride, write_zip


def create_history(root: Path) -> Path:
    existing = root / "inventory/controlled.json"
    if existing.exists():
        if read_json(existing).get("data_mode") != "controlled_fixture":
            raise ValueError("Only controlled fixtures can be reused")
        return existing
    window = [shift_month("2026-09", offset) for offset in range(-23, 1)]
    objects = []
    for number, period in enumerate(window, 1):
        archive = write_zip(
            root / "fixture-input" / f"{period}.zip",
            {
                "controlled.csv": [
                    ride(
                        number,
                        started_at=f"{period}-01 00:00:00",
                        ended_at=f"{period}-01 00:10:00",
                    )
                ]
            },
        )
        bronze = ingest_fixture(archive, period, root)
        manifest = read_json(bronze / "manifest.json")
        item = {
            "period": period,
            "scope": "NYC",
            "key": f"synthetic-{period}.zip",
            "url": "fixture://controlled-ci",
            "bytes": manifest["bytes"],
            "etag": "fixture",
            "last_modified": None,
            "packaging": "monthly_zip",
            "availability": "listed",
            "year": None,
            "checksum": None,
            "zip_entries": None,
            "csv_files": None,
        }
        objects.append(item)
        write_json(
            root / "inventory/objects" / f"{identity(item)}.json",
            {
                **item,
                **manifest,
                "bronze": str(bronze),
                "availability": "verified",
                "source_version": manifest["checksum"],
            },
        )
    inventory = root / "inventory/controlled.json"
    write_json(
        inventory,
        {
            "data_mode": "controlled_fixture",
            "window": window,
            "objects": objects,
            "checked_at": "2026-10-28T00:00:00+00:00",
            "source_url": "fixture://controlled-ci",
        },
    )
    return inventory


def change_source(root: Path, period: str) -> Path:
    path = root / "inventory/controlled.json"
    inventory = read_json(path)
    if (
        inventory.get("data_mode") != "controlled_fixture"
        or period not in inventory["window"]
    ):
        raise ValueError("Change must target one controlled fixture month")
    number = inventory["window"].index(period) + 1
    archive = write_zip(
        root / "fixture-input" / f"changed-{period}.zip",
        {
            "controlled.csv": [
                ride(
                    number,
                    started_at=f"{period}-01 00:00:00",
                    ended_at=f"{period}-01 00:10:00",
                ),
                ride(
                    1000 + number,
                    started_at=f"{period}-02 00:00:00",
                    ended_at=f"{period}-02 00:10:00",
                ),
            ],
        },
    )
    bronze = ingest_fixture(archive, period, root)
    manifest = read_json(bronze / "manifest.json")
    index = number - 1
    item = {
        **inventory["objects"][index],
        "bytes": manifest["bytes"],
        "etag": "changed-fixture",
    }
    inventory["objects"][index] = item
    write_json(
        root / "inventory/objects" / f"{identity(item)}.json",
        {
            **item,
            **manifest,
            "bronze": str(bronze),
            "availability": "verified",
            "source_version": manifest["checksum"],
        },
    )
    write_json(path, inventory)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--change-period")
    args = parser.parse_args()
    print(
        change_source(args.data_dir, args.change_period)
        if args.change_period
        else create_history(args.data_dir)
    )


if __name__ == "__main__":
    main()
