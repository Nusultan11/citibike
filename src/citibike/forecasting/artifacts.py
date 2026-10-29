"""Checksum-verified immutable F4 directories; small atomic pointers only."""

import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citibike.analytics.build import publish_directory
from citibike.features.contracts import VERSION
from citibike.ingestion.archive import checksum
from citibike.storage import read_json, write_json


def checked(path: Path) -> dict[str, Any]:
    report = read_json(path / "manifest.json")
    if report["version"] != VERSION:
        raise ValueError("Incompatible F4 artifact version")
    for name, expected in report["checksums"].items():
        if checksum(path / name) != expected:
            raise ValueError("F4 artifact checksum mismatch")
    return report


def create(
    root: Path,
    kind: str,
    identifier: str,
    builder: Callable[[Path], dict[str, Any]],
) -> Path:
    target = root / "f4" / kind / identifier
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        checked(target)
    else:
        with tempfile.TemporaryDirectory(dir=target.parent) as temporary:
            stage = Path(temporary) / "result"
            stage.mkdir()
            report = builder(stage)
            report.update(
                {
                    "id": identifier,
                    "version": VERSION,
                    "created_at": datetime.now(UTC).isoformat(),
                    "checksums": {
                        str(p.relative_to(stage)): checksum(p)
                        for p in sorted(stage.rglob("*"))
                        if p.is_file()
                    },
                }
            )
            write_json(stage / "manifest.json", report)
            publish_directory(stage, target)
    report = checked(target)
    if kind != "forecasts" or report["status"] == "ok":
        write_json(
            root / "f4" / f"active_{kind}.json", {"id": identifier, "path": str(target)}
        )
    return target


def active(root: Path, kind: str) -> Path:
    return Path(read_json(root / "f4" / f"active_{kind}.json")["path"])
