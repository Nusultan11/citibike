"""Small atomic metadata writes; large datasets remain immutable Parquet/ZIP."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def publish_directory(stage: Path, target: Path) -> None:
    """Bounded retry for transient Windows scanner handles; never overwrite."""
    for attempt in range(6):
        try:
            stage.rename(target)
            return
        except PermissionError:
            if attempt == 5 or target.exists():
                raise
            time.sleep(0.2 * (attempt + 1))


def identity(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, ensure_ascii=False, default=str)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
