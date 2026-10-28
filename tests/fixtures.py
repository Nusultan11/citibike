"""Explicitly synthetic, tiny multi-CSV archives for repeatable CI."""

import argparse
import csv
import io
import zipfile
from pathlib import Path

from citibike.contracts import FIELDS


def ride(number: int, **changes: str) -> dict[str, str]:
    row = dict(
        zip(
            FIELDS,
            [
                f"{number:016X}",
                "classic_bike",
                "2026-04-01 00:00:00",
                "2026-04-01 00:10:00",
                "Test station A",
                "001",
                "Test station B",
                "002",
                "40.7",
                "-74.0",
                "40.71",
                "-74.01",
                "member",
            ],
            strict=True,
        )
    )
    row.update(changes)
    return row


def write_zip(path: Path, files: dict[str, list[dict[str, str]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, rows in files.items():
            text = io.StringIO(newline="")
            writer = csv.DictWriter(text, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            archive.writestr(name, text.getvalue())
    return path


def fixture_archive(path: Path) -> Path:
    a = ride(1)
    return write_zip(
        path,
        {
            "part-a.csv": [
                a,
                ride(2, start_station_id=""),
                ride(3),
                ride(4, ride_id=""),
                ride(5, ended_at="2026-03-31 23:50:00"),
            ],
            "nested/part-b.csv": [
                a,
                ride(3, member_casual="casual"),
                ride(
                    6, started_at="2026-03-31 23:59:00", ended_at="2026-04-01 00:01:00"
                ),
                ride(
                    7, started_at="2026-04-30 23:55:00", ended_at="2026-05-01 00:05:00"
                ),
                ride(8, ended_at="2026-04-01 00:00:00"),
            ],
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    fixture_archive(parser.parse_args().output)
