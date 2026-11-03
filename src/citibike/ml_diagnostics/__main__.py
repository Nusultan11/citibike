"""Read-only inputs, immutable diagnostics; audit never publishes forecasts."""

import argparse
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citibike.forecasting.artifacts import checked, create
from citibike.forecasting.feature_research import save_frame
from citibike.ml_diagnostics import baseline
from citibike.ml_diagnostics.events import controls, detect
from citibike.ml_diagnostics.protocol import (
    EVIDENCE,
    VERSION,
    check_files,
    freeze,
    load_plan,
)
from citibike.processing.resources import peak_rss_bytes
from citibike.storage import identity, read_json, write_json


def audit(root: Path, protocol: Path) -> Path:
    plan = load_plan(protocol)
    identifier = identity([VERSION, "result", checked(protocol)["id"]])

    def build(stage: Path) -> dict[str, Any]:
        before = time.monotonic()
        with baseline.connection() as db:
            baseline.tables(db, plan)
            coverage = baseline.coverage(db, plan)
            frame = baseline.calculate(db, plan)
        detected_at = datetime.now(UTC)
        events, raw_signals = detect(
            frame.to_dict("records"),
            plan["rules"],
            detected_at,
            {
                "protocol_id": checked(protocol)["id"],
                "dataset_id": plan["dataset_id"],
                "mode": "retrospective_posthoc",
                "mapping": "unchanged source IDs",
            },
        )
        calculation = baseline.summary(frame, events)
        save_frame(stage / "rows.parquet", frame)
        write_json(stage / "events.json", {"events": events})
        control = controls(plan["rules"])
        if any(c["status"] != "PASS" for c in control["cases"].values()):
            raise ValueError("Controlled detector acceptance failed")
        elapsed, rss = time.monotonic() - before, peak_rss_bytes()
        if (
            elapsed > plan["budget"]["wall_seconds"]
            or rss > plan["budget"]["rss_gate_bytes"]
            or sum(p.stat().st_size for p in stage.iterdir())
            > plan["budget"]["output_bytes"]
        ):
            raise ValueError("Diagnostics resource budget exceeded")
        # Budget failures do not activate a partial result. The frozen protocol
        # and any earlier immutable result remain untouched.
        write_json(
            stage / "summary.json",
            {
                "coverage": coverage,
                "errors": calculation,
                "controls": control,
                "anomalies": {
                    "events": len(events),
                    "raw_signals": raw_signals,
                    "top_cases": sorted(events, key=lambda r: (-r["score"], r["id"]))[
                        : plan["rules"]["top_k"]
                    ],
                    "precision_at_k": None,
                    "real_false_alert_rate": None,
                    "independent_label_status": "NOT VERIFIED: no expert labels",
                    "online_detection": "NOT VERIFIED; retrospective post-hoc only",
                    "retrospective_delay_hours": {
                        "min": min((e["delay_hours"] for e in events), default=None),
                        "max": max((e["delay_hours"] for e in events), default=None),
                    },
                    "type_event_counts": {
                        kind: sum(kind in e["types"] for e in events)
                        for kind in ("spike", "long_zero", "cusum_up", "cusum_down")
                    },
                },
                "classification_policy": {
                    "DATA ERROR": "Only verified source/lineage/count violations; ID "
                    "ambiguity excludes rows, does not license ID merging. Statistical "
                    "rarity alone is not DATA ERROR.",
                    "MODEL ERROR": "SN prediction minus observed label on supported "
                    "source-ID rows; numerical forecast error, not causal attribution.",
                    "INFORMATION LIMITATION": "missing/unknown, thin context, ID "
                    "ambiguity, publication delay and unlabelled event causes",
                },
                "as_of": {
                    "analysis_as_of": plan["analysis_as_of"],
                    "detected_at": detected_at.isoformat(),
                    "input_available_at": plan["input_available_at"],
                    "strict_origin_supported_rows": int(
                        (
                            frame["input_available_at"] <= frame["forecast_origin_utc"]
                        ).sum()
                    ),
                    "rows": len(frame),
                    "historical_first_publication": None,
                    "proxy_origin_supported_rows": int(
                        (frame["proxy_n"] >= plan["rules"]["min_cell_history"]).sum()
                    ),
                    "mode": plan["as_of_policy"],
                },
                "measurements": {
                    "elapsed_seconds": elapsed,
                    "peak_rss_bytes": rss,
                    "scope": "analysis loop, shared host; not cold end-to-end / SLA",
                },
            },
        )
        check_files(plan["protected_files"])
        return {
            "kind": "ml_diagnostics_result",
            "protocol": str(protocol),
            "protocol_id": checked(protocol)["id"],
            "rows": len(frame),
            "final_test_accessed": False,
            "status": "computed; verification pending",
        }

    try:
        return create(
            root / "decision_research", "diagnostics", identifier, build, activate=False
        )
    except Exception as exc:
        failure = {
            "kind": "ml_diagnostics_failed_attempt",
            "protocol": str(protocol),
            "protocol_id": checked(protocol)["id"],
            "status": "failed",
            "error_type": type(exc).__name__,
            "attempt_at": datetime.now(UTC).isoformat(),
            "result_id_not_published": identifier,
        }
        create(
            root / "decision_research",
            "diagnostics",
            identity(failure),
            lambda stage: failure,
            activate=False,
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser("audit")
    scan.add_argument("--data-dir", type=Path, default=Path("data"))
    scan.add_argument("--protocol", type=Path)
    scan.add_argument("--freeze-only", action="store_true")
    scan.add_argument(
        "--evidence", type=Path, default=Path("docs/evidence/f4-decision-research.json")
    )
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--result", required=True, type=Path)
    verify_parser.add_argument("--output", type=Path, default=EVIDENCE)
    args = parser.parse_args()
    if args.command == "verify":
        from citibike.ml_diagnostics.verify import verify

        evidence = verify(args.result)
        write_json(args.output, evidence)
        print(f"VERIFIED {args.result.name}: {evidence['oracle']}")
        return
    if args.freeze_only:
        if args.protocol:
            raise ValueError("Freeze takes pinned evidence, not an existing protocol")
        evidence = read_json(args.evidence)
        outcome = (
            args.data_dir / "decision_research/f4/outcomes" / evidence["outcome_id"]
        )
        old_plan = checked(
            args.data_dir / "decision_research/f4/protocols" / evidence["protocol_id"]
        )["plan"]
        dataset = Path(old_plan["populations"]["cohort64"]["dataset"])
        lineage = (
            args.data_dir
            / "decision_research/f4/lineage"
            / evidence["corrective"]["lineage"]["id"]
        )
        result = freeze(args.data_dir, dataset, outcome, lineage)
        print(f"FROZEN {result}")
    else:
        if not args.protocol:
            raise ValueError("Freeze --freeze-only before audit --protocol")
        print(f"AUDITED {audit(args.data_dir, args.protocol)}")


if __name__ == "__main__":
    main()
