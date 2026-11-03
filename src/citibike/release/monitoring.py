"""Periodic read-only staging probes, durable metrics and F7 state transitions."""

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import psycopg

from citibike.automation.monitoring import event
from citibike.release.benchmark import distribution
from citibike.release.database import readiness
from citibike.release.recovery import verify
from citibike.serving.db import DatabaseSettings, DatabaseUnavailable, connect
from citibike.storage import read_json


def check(
    settings: DatabaseSettings,
    root: Path,
    backup: Path | None,
    *,
    api_url: str = "http://127.0.0.1:18000",
    local_webhook: str | None = None,
    latency_budget_ms: float = 1000,
    log_command: list[str] | None = None,
) -> dict[str, Any]:
    parsed = urlsplit(api_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("monitor_requires_loopback_api")
    if latency_budget_ms <= 0:
        raise ValueError("invalid_latency_budget")
    now = datetime.now(UTC)
    directory = root / "automation/monitoring"
    previous_path = directory / "release.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    observations = previous.get("signals", {}).get("http", {}).get("observations", [])
    with httpx.Client(timeout=5, trust_env=False, follow_redirects=False) as client:
        for probe_path in ("/ready", "/v1/kpi"):
            started = time.perf_counter()
            try:
                http_status = client.get(api_url.rstrip("/") + probe_path).status_code
            except httpx.HTTPError:
                http_status = 0
            observations.append(
                {
                    "path": probe_path,
                    "status": http_status,
                    "latency_ms": (time.perf_counter() - started) * 1000,
                }
            )
    observations = observations[-120:]
    signals: dict[str, Any] = {
        "database_ready": readiness(settings),
        "disk_free_bytes": shutil.disk_usage(root).free,
        "backup_age_seconds": None,
        "backup_integrity": False,
        "workflow_failures": [],
        "forecast": None,
        "ingestion": {},
        "http": {
            "observations": observations,
            "window_samples": len(observations),
            "errors": sum(r["status"] != 200 for r in observations),
            "error_rate": sum(r["status"] != 200 for r in observations)
            / len(observations),
            "latency_ms": distribution([r["latency_ms"] for r in observations]),
            "scope": "bounded synthetic probes; not production traffic telemetry",
        },
    }
    # Existing F8 middleware emits only status/latency, without URL or secrets.
    # This bounded window supplements probes with real HTTP traffic. Windows
    # overlap; metrics are snapshots and must not be summed as request totals.
    traffic: list[dict[str, Any]] = []
    if log_command:
        try:
            logs = subprocess.run(
                [
                    *log_command,
                    "logs",
                    "--no-color",
                    "--since",
                    "60s",
                    "--tail",
                    "10000",
                    "api",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            for line in logs.stdout.splitlines():
                if '{"event":"http"' not in line:
                    continue
                try:
                    sample = json.loads(line[line.index('{"event":"http"') :])
                    traffic.append(
                        {
                            "status": int(sample["status"]),
                            "latency_ms": float(sample["latency_ms"]),
                        }
                    )
                except (ValueError, KeyError, TypeError):
                    continue
            signals["traffic"] = {
                "scope": (
                    "last 60s, at most 10000 log lines; "
                    "overlapping windows; current container"
                ),
                "samples": len(traffic),
                "errors": sum(r["status"] >= 500 for r in traffic),
                "error_rate": sum(r["status"] >= 500 for r in traffic) / len(traffic)
                if traffic
                else None,
                "latency_ms": distribution([r["latency_ms"] for r in traffic])
                if traffic
                else None,
                "throughput_rps": len(traffic) / 60,
                "collector_ok": True,
            }
        except (OSError, subprocess.SubprocessError):
            signals["traffic"] = {"collector_ok": False}
    if backup:
        try:
            manifest = verify(backup)
            signals["backup_integrity"] = True
            signals["backup_age_seconds"] = (
                now - datetime.fromisoformat(manifest["created_at"])
            ).total_seconds()
        except (OSError, ValueError, KeyError, TypeError):
            pass
    stale = []
    cadence = {"trips": 21600, "gbfs": 60, "forecast": 86400, "risk": 300}
    for operation in (*cadence, "review"):
        path = directory / f"{operation}.json"
        if not path.exists():
            continue
        try:
            record = read_json(path)
            signals["ingestion"][operation] = {
                "status": record["status"],
                "created_at": record["created_at"],
                "age_seconds": (
                    now - datetime.fromisoformat(record["created_at"])
                ).total_seconds(),
            }
            if record.get("last_terminal_status") in {"failed", "unavailable"}:
                signals["workflow_failures"].append(operation)
        except (OSError, ValueError, KeyError, TypeError):
            signals["workflow_failures"].append(operation)
    for operation, seconds in cadence.items():
        state = signals["ingestion"].get(operation)
        if not state or not 0 <= state["age_seconds"] <= seconds * 2:
            stale.append(operation)
    signals["stale_workflows"] = stale
    if signals["database_ready"]:
        try:
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
                    signals["forecast"] = {
                        k: row["manifest"].get(k)
                        for k in (
                            "publication_id",
                            "status",
                            "valid_until",
                            "data_as_of",
                        )
                    }
        except (DatabaseUnavailable, psycopg.Error):
            signals["database_ready"] = False
    forecast = signals["forecast"]
    try:
        signals["forecast_valid"] = bool(
            forecast
            and forecast["status"] == "ok"
            and datetime.fromisoformat(forecast["valid_until"]) > now
        )
    except (TypeError, ValueError):
        signals["forecast_valid"] = False
    checks = {
        "api": all(r["status"] == 200 for r in observations[-2:]),
        "latency": all(r["latency_ms"] <= latency_budget_ms for r in observations[-2:]),
        "database": signals["database_ready"],
        "disk": signals["disk_free_bytes"] >= 512 * 1024**2,
        "backup": (
            signals["backup_integrity"]
            and signals["backup_age_seconds"] is not None
            and 0 <= signals["backup_age_seconds"] <= 86400
        ),
        "workflows": not signals["workflow_failures"] and not stale,
        "forecast": signals["forecast_valid"],
    }
    if log_command:
        checks["traffic"] = bool(
            signals["traffic"]["collector_ok"]
            and all(r["status"] < 500 for r in traffic)
            and (
                not traffic
                or distribution([r["latency_ms"] for r in traffic])["p95"]
                <= latency_budget_ms
            )
        )
    changes = []
    for name, healthy in checks.items():
        status = "ok" if healthy else "failed"
        path = directory / f"release.{name}.json"
        prior = read_json(path) if path.exists() else {}
        # Per-signal recovery remains visible while another gate is failed.
        if prior.get("status") != status or prior.get("webhook") == "delivery_failed":
            changes.append(
                event(
                    root,
                    f"release.{name}",
                    status,
                    local_webhook=local_webhook,
                    check=name,
                )
            )
    record = event(
        root,
        "release",
        "ok" if all(checks.values()) else "failed",
        notify=False,
        signals=signals,
        checks=checks,
        latency_budget_ms=latency_budget_ms,
        transitions=[r["event_id"] for r in changes],
    )
    with (directory / "release-metrics.jsonl").open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def watch(
    settings: DatabaseSettings,
    root: Path,
    backup: Path | None,
    *,
    interval: float = 60,
    samples: int | None = None,
    api_url: str = "http://127.0.0.1:18000",
    local_webhook: str | None = None,
    latency_budget_ms: float = 1000,
    log_command: list[str] | None = None,
) -> None:
    if not 1 <= interval <= 3600 or (samples is not None and samples < 1):
        raise ValueError("invalid_monitor_budget")
    count = 0
    while samples is None or count < samples:
        started = time.monotonic()
        record = check(
            settings,
            root,
            backup,
            api_url=api_url,
            local_webhook=local_webhook,
            latency_budget_ms=latency_budget_ms,
            log_command=log_command,
        )
        print(
            json.dumps(
                {
                    "event_id": record["event_id"],
                    "status": record["status"],
                    "checks": record["checks"],
                }
            ),
            flush=True,
        )
        count += 1
        if samples is None or count < samples:
            time.sleep(max(0, interval - (time.monotonic() - started)))
