"""Durable structured events, latest status, optional webhook and delayed scores."""

import math
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from citibike.features.contracts import utc
from citibike.storage import read_json, write_json


def event(
    root: Path,
    operation: str,
    status: str,
    *,
    local_webhook: str | None = None,
    **details: Any,
) -> dict[str, Any]:
    notify = details.pop("notify", True)
    now = datetime.now(UTC).isoformat()
    directory = root / "automation/monitoring"
    previous_path = directory / f"{operation}.json"
    previous = read_json(previous_path) if previous_path.exists() else {}
    prior_status = previous.get("last_terminal_status", previous.get("status"))
    recovered = status in {"ok", "unchanged"} and prior_status in {
        "failed",
        "unavailable",
    }
    record = {
        "event_id": uuid4().hex,
        "created_at": now,
        "operation": operation,
        "status": status,
        "last_terminal_status": prior_status if status == "running" else status,
        "recovery": "recovered"
        if recovered
        else "required"
        if status in {"failed", "unavailable"}
        else "healthy",
        "accuracy": {
            "status": "pending_actuals",
            "mae": None,
            "wape": None,
            "bias": None,
        },
        **details,
    }
    alert = status in {"failed", "unavailable"} or recovered
    record["alert"] = alert
    # Never log raw exception messages, credentials or webhook URL.
    url = local_webhook or os.environ.get("BIKEFLOW_ALERT_WEBHOOK")
    if alert and notify and url:
        parsed = urlsplit(url)
        if (
            not (
                (local_webhook is None and parsed.scheme == "https")
                or (
                    local_webhook is not None
                    and parsed.scheme == "http"
                    and parsed.hostname in {"127.0.0.1", "::1"}
                )
            )
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            record["webhook"] = "invalid_configuration"
        else:
            try:
                with httpx.Client(
                    timeout=5, follow_redirects=False, trust_env=False
                ) as client:
                    client.post(url, json=record).raise_for_status()
                record["webhook"] = "delivered"
            except httpx.HTTPError:
                record["webhook"] = "delivery_failed"
    write_json(directory / "events" / f"{record['event_id']}.json", record)
    write_json(previous_path, record)
    return record


def delayed_scores(
    predictions: list[dict[str, Any]], actuals: list[dict[str, Any]], as_of: datetime
) -> dict[str, Any]:
    """Exact station/hour/type join; actuals must be observed and published by as_of."""

    def key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            row["station_id"],
            utc(row["target_hour_utc"]).isoformat(),
            row["prediction_type"],
        )

    observed = {}
    for row in actuals:
        k = key(row)
        if k in observed:
            raise ValueError("duplicate_actual_grain")
        observed[k] = row
    pairs = []
    seen = set()
    for prediction in predictions:
        k = key(prediction)
        if k in seen:
            raise ValueError("duplicate_prediction_grain")
        seen.add(k)
        actual = observed.get(k)
        if actual is None or prediction.get("status") != "ok":
            continue
        if actual.get("label_status") not in {"observed_zero", "observed_nonzero"}:
            continue
        hour_end = utc(actual["target_hour_utc"]) + timedelta(hours=1)
        if not hour_end <= utc(actual["available_at"]) <= as_of:
            continue
        value, estimate = float(actual["actual"]), float(prediction["predicted_value"])
        if value < 0 or estimate < 0 or not math.isfinite(value + estimate):
            raise ValueError("invalid_delayed_observation")
        pairs.append((value, estimate))
    if not pairs:
        return {
            "status": "pending_actuals",
            "rows": 0,
            "mae": None,
            "wape": None,
            "bias": None,
        }
    error = sum(abs(a - p) for a, p in pairs)
    total = sum(a for a, _ in pairs)
    return {
        "status": "observed",
        "rows": len(pairs),
        "mae": error / len(pairs),
        "wape": error / total if total else None,
        "bias": sum(p - a for a, p in pairs) / len(pairs),
    }
