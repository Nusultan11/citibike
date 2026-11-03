"""Bounded causal signal rules, followed by offline interval deduplication."""

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from citibike.features.contracts import utc
from citibike.storage import identity


def detect(
    rows: list[dict[str, Any]],
    rules: dict[str, Any],
    as_of: datetime,
    provenance: dict[str, Any],
) -> tuple[list[dict[str, Any]], int]:
    """No future counts are read by a rule. Gaps reset all state. The real
    invocation is retrospective; offline merging may extend an event interval
    after its first detection, with finalisation explicitly timestamped.
    """
    raw: list[dict[str, Any]] = []
    previous: datetime | None = None
    group: tuple[str, str, str] | None = None
    block = 0
    zero: list[dict[str, Any]] = []
    pos: list[dict[str, Any]] = []
    neg: list[dict[str, Any]] = []
    plus = minus = 0.0

    def emit(support: list[dict[str, Any]], kind: str, score: float) -> None:
        first, last = support[0], support[-1]
        available = max(utc(r["input_available_at"]) for r in support)
        end = utc(last["target_hour_utc"]) + timedelta(hours=1)
        raw.append(
            {
                "station_id": first["station_id"],
                "prediction_type": first["prediction_type"],
                "fold": first["fold"],
                "block": block,
                "event_at": utc(first["target_hour_utc"]).isoformat(),
                "event_end": end.isoformat(),
                "available_at": available.isoformat(),
                "detected_at": max(as_of, available, end).isoformat(),
                "type": kind,
                "score": float(score),
                "evidence_keys": [r["row_key"] for r in support],
                "input_quality": (
                    "observed; supported context; operating status UNKNOWN"
                ),
            }
        )

    for row in sorted(
        rows,
        key=lambda r: (
            r["station_id"],
            r["prediction_type"],
            r["fold"],
            utc(r["target_hour_utc"]),
        ),
    ):
        when = utc(row["target_hour_utc"])
        key = (row["station_id"], row["prediction_type"], row["fold"])
        valid = (
            row["input_quality"] == "observed"
            and row["actual"] is not None
            and math.isfinite(float(row["actual"]))
            and row["context_mean"] is not None
            and math.isfinite(float(row["context_mean"]))
            and row["context_n"] >= rules["min_cell_history"]
            and utc(row["input_available_at"]) <= as_of
            and when + timedelta(hours=1) <= as_of
        )
        if (
            key != group
            or previous is None
            or when - previous != timedelta(hours=1)
            or not valid
        ):
            zero, pos, neg = [], [], []
            plus = minus = 0.0
            block += 1
        group, previous = key, when
        if not valid:
            continue
        observed, expected = float(row["actual"]), float(row["context_mean"])
        variance = float(row["context_var"]) if row["context_var"] is not None else 0.0
        if not all(math.isfinite(v) and v >= 0 for v in (observed, expected, variance)):
            raise ValueError("Invalid diagnostic count / variance")
        z = (observed - expected) / math.sqrt(max(1.0, expected, variance))
        if observed >= rules["spike_count"] and z >= rules["spike_z"]:
            emit([row], "spike", z)
        zero = [*zero, row] if observed == 0 else []
        expected_zero = math.fsum(float(r["context_mean"]) for r in zero)
        if len(zero) >= rules["zero_hours"] and expected_zero >= rules["zero_expected"]:
            emit(zero, "long_zero", expected_zero / rules["zero_expected"])
        clipped = max(-rules["z_clip"], min(rules["z_clip"], z))
        plus = max(0.0, plus + clipped - rules["cusum_k"])
        minus = max(0.0, minus - clipped - rules["cusum_k"])
        pos = [*pos, row] if plus > 0 else []
        neg = [*neg, row] if minus > 0 else []
        if plus >= rules["cusum_h"] and len(pos) >= rules["cusum_min_hours"]:
            emit(pos, "cusum_up", plus / rules["cusum_h"])
            plus, pos = 0.0, []
        if minus >= rules["cusum_h"] and len(neg) >= rules["cusum_min_hours"]:
            emit(neg, "cusum_down", minus / rules["cusum_h"])
            minus, neg = 0.0, []
    events: list[dict[str, Any]] = []
    for signal in sorted(
        raw,
        key=lambda r: (
            r["station_id"],
            r["prediction_type"],
            r["fold"],
            r["block"],
            r["event_at"],
            r["type"],
        ),
    ):
        previous_event = events[-1] if events else None
        if (
            previous_event
            and all(
                previous_event[k] == signal[k]
                for k in ("station_id", "prediction_type", "fold", "block")
            )
            and utc(signal["event_at"])
            <= utc(previous_event["event_end"])
            + timedelta(hours=rules["merge_gap_hours"])
        ):
            previous_event["event_end"] = max(
                previous_event["event_end"], signal["event_end"]
            )
            previous_event["types"] = sorted(
                set(previous_event["types"]) | {signal["type"]}
            )
            previous_event["score"] = max(previous_event["score"], signal["score"])
            previous_event["evidence_keys"] = sorted(
                set(previous_event["evidence_keys"]) | set(signal["evidence_keys"])
            )
            previous_event["available_at"] = max(
                previous_event["available_at"], signal["available_at"]
            )
            previous_event["detected_at"] = min(
                previous_event["detected_at"], signal["detected_at"]
            )
            previous_event["finalized_at"] = max(
                previous_event["finalized_at"], signal["detected_at"]
            )
            previous_event["raw_signals"] += 1
        else:
            event = {k: v for k, v in signal.items() if k != "type"}
            event.update(
                {
                    "types": [signal["type"]],
                    "raw_signals": 1,
                    "finalized_at": signal["detected_at"],
                }
            )
            events.append(event)
    for event in events:
        event["id"] = identity(
            [
                event["station_id"],
                event["prediction_type"],
                event["fold"],
                event["event_at"],
                event["event_end"],
                event["types"],
                provenance,
            ]
        )
        event["provenance"] = provenance
        event["classification"] = "INFORMATION LIMITATION"
        event["reason"] = (
            "statistical signal; no independent cause/operating-state label"
        )
        event["delay_hours"] = (
            utc(event["detected_at"]) - utc(event["event_at"])
        ).total_seconds() / 3600
    return events, len(raw)


def controls(rules: dict[str, Any]) -> dict[str, Any]:
    """Declared synthetic streams only; no estimate of real anomaly precision."""
    start = datetime(2025, 11, 1, tzinfo=UTC)
    as_of = start + timedelta(days=30)
    base = [
        {
            "station_id": "CONTROL",
            "prediction_type": "departures",
            "fold": "synthetic",
            "target_hour_utc": start + timedelta(hours=i),
            "actual": 4.0,
            "context_mean": 4.0,
            "context_var": 4.0,
            "context_n": 16,
            "input_quality": "observed",
            "input_available_at": start + timedelta(hours=i + 1),
            "row_key": str(i),
        }
        for i in range(240)
    ]
    outcomes: dict[str, Any] = {}
    rng = random.Random(42)
    variants = {
        "constant_null": (None, None),
        "poisson_null": (None, None),
        "spike": (48, 49),
        "long_zero": (48, 60),
        "shift_up": (48, 72),
        "shift_down": (48, 72),
        "missing": (48, 72),
        "publication_lag": (48, 72),
        "ambiguous_id": (48, 72),
        "cold_start": (48, 72),
        "absent_history": (48, 72),
    }
    for name, (left, right) in variants.items():
        stream = [dict(r) for r in base]
        if name == "poisson_null":
            for row in stream:
                product, count = 1.0, -1
                while product > math.exp(-4):
                    product *= rng.random()
                    count += 1
                row["actual"] = float(count)
        if left is not None and right is not None:
            for row in stream[left:right]:
                row["actual"] = (
                    40.0 if name == "spike" else 10.0 if name == "shift_up" else 0.0
                )
                if name in ("missing", "ambiguous_id", "cold_start", "absent_history"):
                    row["input_quality"] = name
                if name == "missing":
                    row["actual"] = None
                if name in ("cold_start", "absent_history"):
                    row["context_n"] = 0
                if name == "publication_lag":
                    row["input_available_at"] = as_of + timedelta(days=1)
        events, raw_count = detect(
            stream, rules, as_of, {"mode": "controlled_injection"}
        )
        injected = name in ("spike", "long_zero", "shift_up", "shift_down")
        matching = [
            e
            for e in events
            if left is not None
            and right is not None
            and utc(e["event_at"]) < start + timedelta(hours=right)
            and utc(e["event_end"]) > start + timedelta(hours=left)
        ]
        # Re-run each prefix to measure true first threshold crossing, rather
        # than using the retrospective execution timestamp as an online delay.
        delay = None
        if injected and left is not None and right is not None:
            for i in range(left + 1, right + 1):
                found, _ = detect(
                    stream[:i],
                    rules,
                    start + timedelta(hours=i),
                    {"mode": "controlled_stream"},
                )
                if found:
                    delay = i - left
                    break
        false = len(events) - len(matching) if injected else len(events)
        outcomes[name] = {
            "events": len(events),
            "raw_signals": raw_count,
            "injected": injected,
            "detected": bool(matching) if injected else None,
            "first_detection_delay_hours": delay,
            "false_alert_events": false,
            "exposure_hours": len(stream),
            "false_alerts_per_1000_hours": false / len(stream) * 1000,
            "status": "PASS"
            if (bool(matching) if injected else not events)
            else "FAIL",
        }
    # UTC continuity across a repeated NY local hour is retained (fold 0/1).
    dst_stream = [dict(r) for r in base[:48]]
    dst_events, _ = detect(dst_stream, rules, as_of, {"mode": "DST_negative"})
    outcomes["dst_fall_back"] = {
        "status": "PASS" if not dst_events else "FAIL",
        "utc_hours": len(dst_stream),
        "events": len(dst_events),
    }
    return {
        "mode": "synthetic_only",
        "seed": 42,
        "cases": outcomes,
        "real_precision_at_k": None,
        "real_false_alert_rate": None,
        "limitation": "Known controlled null/injections only; one seeded null "
        "stream is not a real-demand precision estimate",
    }
