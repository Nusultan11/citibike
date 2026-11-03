from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from citibike.features.contracts import NY
from citibike.features.dataset import build
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.ml_diagnostics.__main__ import audit
from citibike.ml_diagnostics.events import controls, detect
from citibike.ml_diagnostics.protocol import RULES, VERSION, freeze, load_plan
from citibike.ml_diagnostics.verify import event_oracle, label_oracle, verify
from citibike.storage import identity, read_json, write_json
from tests.fixtures_forecasting import gold_fixture


def stream() -> list[dict[str, Any]]:
    start = datetime(2025, 11, 2, 0, tzinfo=UTC)
    return [
        {
            "station_id": "A",
            "prediction_type": "departures",
            "fold": "test",
            "target_hour_utc": start + timedelta(hours=i),
            "actual": 4.0,
            "context_mean": 4.0,
            "context_var": 4.0,
            "context_n": 10,
            "input_available_at": start + timedelta(hours=i + 1),
            "input_quality": "observed",
            "row_key": str(i),
        }
        for i in range(48)
    ]


def test_controlled_detection_and_false_alert_accounting() -> None:
    result = controls(RULES)
    assert all(c["status"] == "PASS" for c in result["cases"].values())
    for name, delay in (
        ("spike", 1),
        ("long_zero", 6),
        ("shift_up", 4),
        ("shift_down", 6),
    ):
        assert result["cases"][name]["detected"]
        assert result["cases"][name]["first_detection_delay_hours"] == delay
    assert result["real_precision_at_k"] is None
    assert result["real_false_alert_rate"] is None


@pytest.mark.parametrize(
    "quality",
    [
        "missing_observation",
        "ambiguous_id",
        "station_not_known",
        "insufficient_history",
    ],
)
def test_information_limits_do_not_become_zero_anomalies(quality: str) -> None:
    rows = stream()
    for row in rows[5:20]:
        row["actual"] = None if quality == "missing_observation" else 0
        row["input_quality"] = quality
    assert detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {}) == ([], 0)


def test_publication_lag_no_early_alert_and_delayed_replay() -> None:
    rows = stream()
    available = datetime(2026, 1, 1, tzinfo=UTC)
    for row in rows[5:20]:
        row["actual"] = 0
        row["input_available_at"] = available
    assert not detect(rows, RULES, available - timedelta(seconds=1), {})[0]
    events, _ = detect(rows, RULES, available, {})
    assert len(events) == 1
    assert events[0]["available_at"] == available.isoformat()
    assert events[0]["detected_at"] >= events[0]["available_at"]
    assert events[0]["classification"] == "INFORMATION LIMITATION"


def test_missing_breaks_zero_runs_and_cusum_and_prevents_merging() -> None:
    rows = stream()
    for row in rows[5:24]:
        row["actual"] = 0
    rows[13]["actual"] = None
    rows[13]["input_quality"] = "missing_observation"
    events, raw = detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {})
    assert len(events) == 2 and raw > len(events)
    assert all("13" not in e["evidence_keys"] for e in events)
    assert event_oracle(rows, RULES, events) == raw
    forged = deepcopy(events)
    forged[0]["score"] += 1
    with pytest.raises(ValueError, match="interval/score"):
        event_oracle(rows, RULES, forged)


def test_dst_repeated_local_hour_remains_two_utc_labels() -> None:
    rows = stream()
    folds = [
        r
        for r in rows
        if r["target_hour_utc"].astimezone(NY).hour == 1
        and r["target_hour_utc"].astimezone(NY).day == 2
    ]
    assert len(folds) == 2
    assert {r["target_hour_utc"].astimezone(NY).fold for r in folds} == {0, 1}
    assert not detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {})[0]


def test_low_context_expectation_and_no_history_do_not_alert() -> None:
    rows = stream()
    for row in rows:
        row["actual"] = 0
        row["context_mean"] = 0.01
    assert not detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {})[0]


@pytest.mark.parametrize("injected", [0, 10, 40])
def test_independent_event_oracle_for_both_targets_and_mixed_signals(
    injected: int,
) -> None:
    rows = stream()
    for row in rows[5:20]:
        row["actual"] = injected
    rows += [
        {**r, "prediction_type": "arrivals", "row_key": "arr-" + r["row_key"]}
        for r in rows
    ]
    events, raw = detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {})
    assert len(events) == 2
    assert event_oracle(rows, RULES, events) == raw
    forged = deepcopy(events)
    forged[0]["available_at"] = datetime(2027, 1, 1, tzinfo=UTC).isoformat()
    with pytest.raises(ValueError, match="timestamps"):
        event_oracle(rows, RULES, forged)
    for row in rows:
        row["actual"] = 100
        row["context_n"] = 0
    assert not detect(rows, RULES, datetime(2026, 1, 1, tzinfo=UTC), {})[0]


@pytest.fixture(scope="module")
def diagnostic_result(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, Path, Path]:
    root = tmp_path_factory.mktemp("diagnostics")
    gold = gold_fixture(root)
    gold_report = read_json(gold / "manifest.json")
    snapshot = Path(gold_report["source_snapshot"])
    old_snapshot = read_json(snapshot / "manifest.json")
    part = old_snapshot["partitions"][0]
    source_id = "controlled-source"
    part.update(
        {
            "period": "2024-10",
            "source_object_id": source_id,
            "checksums": {
                "silver.parquet": checksum(Path(part["path"]) / "silver.parquet")
            },
        }
    )
    old_snapshot["published_at"] = datetime.now(UTC).isoformat()
    write_json(snapshot / "manifest.json", old_snapshot)
    gold_report["source_manifest_checksum"] = checksum(snapshot / "manifest.json")
    write_json(gold / "manifest.json", gold_report)
    write_json(
        root / "bronze/NYC" / source_id / "manifest.json",
        {
            "source_url": "controlled_fixture",
            "checksum": source_id,
            "retrieved_at": datetime.now(UTC).isoformat(),
        },
    )
    dataset = build(root, gold, station_count=4, smoke=True)
    data = checked(dataset)
    items = []
    for i, (a, b) in enumerate(data["config"]["validation"]):
        for kind in ("departures", "arrivals"):

            def trial(
                stage: Path, a: str = a, b: str = b, kind: str = kind
            ) -> dict[str, Any]:
                with duckdb.connect() as db:
                    db.execute(
                        """CREATE TABLE predictions AS
                        SELECT *,seasonal_naive::DOUBLE predicted
                        FROM read_parquet(?) WHERE forecast_origin_utc>=?::TIMESTAMPTZ
                        AND forecast_origin_utc<?::TIMESTAMPTZ AND prediction_type=?
                        AND eligibility='eligible'""",
                        [
                            str(dataset / "examples.parquet"),
                            a,
                            b,
                            kind,
                        ],
                    )
                    db.execute(
                        "COPY predictions TO ? (FORMAT PARQUET)",
                        [str(stage / "predictions.parquet")],
                    )
                return {"request": {"dataset_id": data["id"]}}

            path = create(
                root, "diagnostic_fixture_trials", f"{i}-{kind}", trial, activate=False
            )
            items.append(
                {
                    "population": "cohort64",
                    "name": "seasonal_naive",
                    "fold": f"T{i}",
                    "prediction_type": kind,
                    "status": "success",
                    "path": str(path),
                }
            )
    outcome = create(
        root,
        "diagnostic_fixture_outcome",
        "controlled",
        lambda stage: {"stages": {"S1_features": items}},
        activate=False,
    )
    lineage = create(
        root,
        "diagnostic_fixture_lineage",
        "controlled",
        lambda stage: {"pairs": []},
        activate=False,
    )
    protocol = freeze(root, dataset, outcome, lineage)
    result = audit(root, protocol)
    return root, protocol, result


def test_full_audit_oracle_repeat_and_no_active_pointer(
    diagnostic_result: tuple[Path, Path, Path],
) -> None:
    root, protocol, result = diagnostic_result
    before = {p: checksum(p) for p in result.glob("*")}
    evidence = verify(result)
    assert evidence["oracle"]["silver_labels"] > 0
    assert evidence["oracle"]["mismatches"] == 0
    assert evidence["results"]["as_of"]["strict_origin_supported_rows"] == 0
    assert audit(root, protocol) == result
    assert {p: checksum(p) for p in result.glob("*")} == before
    assert not (root / "decision_research/f4/active_diagnostics.json").exists()


def test_tampered_summary_detected_by_independent_oracle(
    diagnostic_result: tuple[Path, Path, Path],
) -> None:
    _, _, result = diagnostic_result
    summary_path = result / "summary.json"
    original, manifest = (
        summary_path.read_bytes(),
        (result / "manifest.json").read_bytes(),
    )
    try:
        summary = read_json(summary_path)
        summary["errors"]["segments"][0]["context"]["mae"] += 1
        write_json(summary_path, summary)
        report = read_json(result / "manifest.json")
        report["checksums"]["summary.json"] = checksum(summary_path)
        write_json(result / "manifest.json", report)
        with pytest.raises(ValueError, match="segmented metric"):
            verify(result)
    finally:
        summary_path.write_bytes(original)
        (result / "manifest.json").write_bytes(manifest)


def test_protocol_protected_tamper_rejected(
    diagnostic_result: tuple[Path, Path, Path],
) -> None:
    _, protocol, _ = diagnostic_result
    plan = load_plan(protocol)
    path = Path(plan["sources"][0]["path"])
    original = path.read_bytes()
    try:
        path.write_bytes(original + b" ")
        with pytest.raises(ValueError, match="Protected checksum"):
            load_plan(protocol)
    finally:
        path.write_bytes(original)


def test_failed_attempt_is_retained_without_replacing_success(
    diagnostic_result: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from citibike.ml_diagnostics import baseline

    root, protocol, result = diagnostic_result
    plan = deepcopy(load_plan(protocol))
    plan["analysis_as_of"] = datetime.now(UTC).isoformat()
    different = create(
        root / "decision_research",
        "diagnostics",
        identity([VERSION, plan]),
        lambda stage: {"kind": "ml_diagnostics_protocol", "plan": plan},
        activate=False,
    )

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("controlled failure")

    monkeypatch.setattr(baseline, "calculate", fail)
    original = checksum(result / "manifest.json")
    with pytest.raises(RuntimeError, match="controlled failure"):
        audit(root, different)
    failures = [
        read_json(p)
        for p in (root / "decision_research/f4/diagnostics").glob("*/manifest.json")
        if read_json(p)["kind"] == "ml_diagnostics_failed_attempt"
    ]
    assert failures and failures[-1]["status"] == "failed"
    assert checksum(result / "manifest.json") == original


def test_label_oracle_keeps_ny_month_anchor_after_utc_cutoff(tmp_path: Path) -> None:
    """The anchor at July 1 01UTC is still June 30 in NY; it is not scored."""
    gold = tmp_path / "gold"
    gold.mkdir()
    write_json(
        gold / "manifest.json",
        {"months": [{"month": "2025-06", "silver": [{"source_period": "2025-06"}]}]},
    )
    silver = tmp_path / "silver.parquet"
    cutoff = datetime(2025, 7, 1, tzinfo=UTC)
    available = datetime(2025, 7, 1, 4, tzinfo=UTC) + timedelta(days=45)
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        db.execute("""CREATE TABLE silver(start_station_id VARCHAR,
            end_station_id VARCHAR,
            started_at_utc TIMESTAMPTZ,ended_at_utc TIMESTAMPTZ)""")
        db.executemany(
            "INSERT INTO silver VALUES(?,?,?,?)",
            [
                (
                    station,
                    station,
                    cutoff + timedelta(hours=h, minutes=10),
                    cutoff + timedelta(hours=h, minutes=20),
                )
                for station, h in (("A", -3), ("B", -2), ("B", -1), ("A", 1))
            ],
        )
        db.execute("COPY silver TO ? (FORMAT PARQUET)", [str(silver)])
        db.execute("CREATE TABLE stations AS SELECT 'A' station_id")
        db.execute(
            """CREATE TABLE dense AS SELECT 'A' station_id,h hour_utc,
            '2025-06' month_key,
            (h=?::TIMESTAMPTZ-INTERVAL '3 hours')::BIGINT departures,
            (h=?::TIMESTAMPTZ-INTERVAL '3 hours')::BIGINT arrivals,
            ?::TIMESTAMPTZ label_available_at FROM generate_series(
            ?::TIMESTAMPTZ-INTERVAL '3 hours',?::TIMESTAMPTZ-INTERVAL '1 hour',
            INTERVAL '1 hour') t(h)""",
            [cutoff, cutoff, available, cutoff, cutoff],
        )

        def dataset_builder(stage: Path) -> dict[str, Any]:
            db.execute(
                "COPY dense TO ? (FORMAT PARQUET)", [str(stage / "dense.parquet")]
            )
            return {"gold_path": str(gold), "config": {"final": ["2025-07-01"]}}

        dataset = create(
            tmp_path, "datasets", "boundary", dataset_builder, activate=False
        )
        plan = {
            "dataset": str(dataset),
            "sources": [{"silver": str(silver)}],
            "excluded_ids": [],
        }
        count = {
            "station_hours": 3,
            "observed": 3,
            "missing": 0,
            "observed_zero": 2,
            "published_events": 1,
            "excluded_hours": 0,
            "excluded_observed": 0,
            "excluded_events": 0,
        }
        coverage: dict[str, Any] = {
            "summary": [],
            "by_month": [],
            "by_station": [],
            "city_source_gaps": [],
        }
        for kind in ("departures", "arrivals"):
            coverage["summary"].append(
                {
                    **count,
                    "prediction_type": kind,
                    "segment": "all",
                    "excluded_observed_share": 0.0,
                    "excluded_event_share": 0.0,
                }
            )
            coverage["by_month"].append(
                {**count, "prediction_type": kind, "month_key": "2025-06"}
            )
            coverage["by_station"].append(
                {**count, "prediction_type": kind, "station_id": "A"}
            )
            coverage["city_source_gaps"].append(
                {"prediction_type": kind, "city_gap_hours": 0}
            )
        assert label_oracle(db, plan, coverage) == 6
        missing_anchor = tmp_path / "without-anchor.parquet"
        db.execute(
            "CREATE TABLE truncated AS SELECT * FROM silver WHERE started_at_utc<?",
            [cutoff],
        )
        db.execute("COPY truncated TO ? (FORMAT PARQUET)", [str(missing_anchor)])
        plan["sources"] = [{"silver": str(missing_anchor)}]
        with pytest.raises(ValueError, match="Silver label mismatch"):
            label_oracle(db, plan, coverage)
