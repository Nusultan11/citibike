"""Read-only operator snapshot and durable alert/recovery through F7 events."""

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citibike.automation.monitoring import event
from citibike.release.database import readiness
from citibike.release.recovery import verify
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import read_json


def check(
    settings: DatabaseSettings, root: Path, backup: Path | None
) -> dict[str, Any]:
    now = datetime.now(UTC)
    signals: dict[str, Any] = {
        "database_ready": readiness(settings),
        "disk_free_bytes": shutil.disk_usage(root).free,
        "backup_age_seconds": None,
        "backup_integrity": False,
        "workflow_failures": [],
        "forecast": None,
        "ingestion": {},
    }
    if backup:
        try:
            manifest = verify(backup)
            signals["backup_integrity"] = True
            signals["backup_age_seconds"] = (
                now - datetime.fromisoformat(manifest["created_at"])
            ).total_seconds()
        except (OSError, ValueError, KeyError):
            pass
    stale = []
    cadence = {"trips": 21600, "gbfs": 60, "forecast": 86400, "risk": 300}
    for path in (root / "automation/monitoring").glob("*.json"):
        record = read_json(path)
        if record["operation"] == "release":
            continue
        signals["ingestion"][record["operation"]] = {
            "status": record["status"],
            "created_at": record["created_at"],
            "age_seconds": (
                now - datetime.fromisoformat(record["created_at"])
            ).total_seconds(),
        }
        if record.get("last_terminal_status") in {"failed", "unavailable"}:
            signals["workflow_failures"].append(record["operation"])
    for operation, seconds in cadence.items():
        state = signals["ingestion"].get(operation)
        if not state or state["age_seconds"] > seconds * 2:
            stale.append(operation)
    signals["stale_workflows"] = stale
    if signals["database_ready"]:
        with connect(settings, readonly=True) as db:
            signals["connections"] = db.execute(
                "SELECT count(*) total FROM pg_stat_activity"
            ).fetchone()
            signals["database_bytes"] = db.execute(
                "SELECT pg_database_size(current_database()) bytes"
            ).fetchone()
            row = db.execute(
                "SELECT p.manifest FROM ml_forecast_publications p "
                "JOIN ml_forecast_active a ON a.publication_id=p.publication_id"
            ).fetchone()
            if row:
                published = row["manifest"]
                signals["forecast"] = {
                    k: published.get(k)
                    for k in ("publication_id", "status", "valid_until", "data_as_of")
                }
    forecast = signals["forecast"]
    signals["forecast_valid"] = bool(
        forecast
        and forecast["status"] == "ok"
        and datetime.fromisoformat(forecast["valid_until"]) > now
    )
    unhealthy = (
        not signals["database_ready"]
        or not signals["backup_integrity"]
        or signals["disk_free_bytes"] < 512 * 1024**2
        or signals["backup_age_seconds"] is None
        or signals["backup_age_seconds"] > 86400
        or bool(signals["workflow_failures"])
        or bool(stale)
        or not signals["forecast_valid"]
    )
    record = event(root, "release", "failed" if unhealthy else "ok", signals=signals)
    return record
