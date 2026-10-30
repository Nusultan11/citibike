from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from citibike.features.contracts import SCHEMA, utc
from citibike.features.dataset import build
from citibike.forecasting import inference, models
from citibike.forecasting.artifacts import active, checked
from citibike.forecasting.experiment import final_test, promotion, select, split_frames
from citibike.forecasting.verify import oracle_file, provenance, verify
from citibike.storage import write_json


@pytest.mark.parametrize(
    "algorithm", ["poisson_bounded", "lightgbm_bounded", "catboost_bounded"]
)
@pytest.mark.parametrize("group", list(models.GROUPS))
def test_group_ablation_removes_inputs_and_retains_saved_parity(
    forecast_workflow: dict[str, Path], tmp_path: Path, algorithm: str, group: str
) -> None:
    with duckdb.connect() as db:
        frame = db.execute(
            """SELECT * FROM read_parquet(?) WHERE eligibility='eligible'
            AND prediction_type='departures' ORDER BY station_id,forecast_origin_utc
            LIMIT 128""",
            [str(forecast_workflow["dataset"] / "examples.parquet")],
        ).df()
    config = {**models.configurations(True)[algorithm], "drop_groups": [group]}
    fitted, info = models.fit(frame, config)
    assert "horizon_h" in fitted["feature_names"]
    assert not set(models.GROUPS[group]) & set(fitted["feature_names"])
    values, _ = models.predict(fitted, frame)
    changed = frame.copy()
    for name in models.GROUPS[group]:
        changed[name] = "unseen" if name == "station_id" else 99999
    assert models.predict(fitted, changed)[0] == values
    path = tmp_path / "ablated.joblib"
    models.save(path, fitted)
    assert models.predict(models.load(path), frame)[0] == values
    assert info["training_rows"] == 128


def test_baseline_group_removal_and_unknown_schema_are_rejected() -> None:
    with pytest.raises(ValueError, match="semantics"):
        models.feature_names(
            {"algorithm": "seasonal_naive", "drop_groups": ["calendar"]}
        )
    with pytest.raises(ValueError, match="Unknown ablation"):
        models.feature_names({"algorithm": "poisson", "drop_groups": ["weather"]})


def test_research_protocol_trial_failure_durability_and_population(
    forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    from citibike.forecasting.experiment import (
        checked_protocol,
        research_inputs,
        research_protocol,
        research_trial,
    )
    from citibike.ingestion.archive import checksum
    from citibike.storage import read_json

    p = forecast_workflow
    frozen = {str(f): f.read_bytes() for f in (p["root"] / "f4").glob("active_*.json")}
    protocol_path = research_protocol(p["root"], p["dataset"])
    protocol = checked_protocol(protocol_path)
    assert research_protocol(p["root"], p["dataset"]) == protocol_path
    assert not list((p["root"] / "f4").glob("active_research_protocols.json"))
    inputs = research_inputs(p["root"], protocol)
    split = checked(inputs)["splits"][0]
    train, score = inputs / split["train_file"], inputs / split["validation_file"]
    request = {
        "protocol_id": protocol["id"],
        "dataset_id": protocol["plan"]["dataset_id"],
        "fold": 0,
        "prediction_type": "departures",
        "name": "invalid_controlled_trial",
        "parameters": {"algorithm": "invalid"},
        "fit_asof": split["fit_asof"],
        "train_file": str(train),
        "train_file_sha256": checksum(train),
        "evaluation_file": str(score),
        "evaluation_file_sha256": checksum(score),
        "evaluation_rows_hash": split["populations"]["departures"][
            "evaluation_rows_hash"
        ],
        "budgets": protocol["plan"]["budgets"],
        "seed": 42,
        "purpose": "controlled_negative",
    }
    failed = research_trial(p["root"], request, isolated=False)
    failure = checked(failed)
    assert failure["status"] == "failed" and failure["error"]["type"] == "ValueError"
    assert research_trial(p["root"], request, isolated=False) == failed
    assert (failed / "manifest.json").exists()
    good = {
        **request,
        "name": "controlled_success",
        "parameters": {"algorithm": "seasonal_naive"},
    }
    success = research_trial(p["root"], good, isolated=True)
    result = checked(success)
    assert result["status"] == "success", result.get("error")
    assert (
        result["parity_rows"] == split["populations"]["departures"]["evaluation_rows"]
    )
    from citibike.forecasting.verify import oracle_file

    assert (
        oracle_file(success / result["predictions"], result["metrics"])
        == result["parity_rows"]
    )
    bad_population = {**good, "name": "wrong_rows", "evaluation_rows_hash": "bad"}
    assert (
        checked(research_trial(p["root"], bad_population, isolated=False))["error"][
            "message"
        ]
        == "Trial population changed"
    )
    unavailable = {
        **good,
        "name": "unavailable_label",
        "fit_asof": "2024-10-01T00:00:00+00:00",
    }
    assert (
        checked(research_trial(p["root"], unavailable, isolated=False))["error"][
            "message"
        ]
        == "Unavailable training label"
    )
    source_mismatch = {
        **good,
        "name": "source_hash_mismatch",
        "train_file_sha256": "bad",
    }
    assert (
        checked(research_trial(p["root"], source_mismatch, isolated=False))["error"][
            "message"
        ]
        == "Trial input checksum mismatch"
    )
    budget = {
        **good,
        "name": "timeout",
        "budgets": {**good["budgets"], "worker_wall_seconds": 0.001},
    }
    timeout = checked(research_trial(p["root"], budget, isolated=True))
    assert (
        timeout["status"] == "failed" and timeout["error"]["type"] == "TimeoutExpired"
    )
    for file, content in frozen.items():
        assert Path(file).read_bytes() == content
    tampered = read_json(protocol_path / "manifest.json")
    tampered["plan"]["windows"] = []
    other = tmp_path / "tampered"
    write_json(other / "manifest.json", tampered)
    with pytest.raises(ValueError, match="protocol/code"):
        checked_protocol(other)
    from citibike.forecasting.experiment import RESEARCH_VERSION
    from citibike.storage import identity

    forged = read_json(protocol_path / "manifest.json")
    forged["plan"]["windows"] = [checked(p["dataset"])["config"]["final"]]
    forged["id"] = identity([RESEARCH_VERSION, forged["plan"]])
    write_json(other / "manifest.json", forged)
    with pytest.raises(ValueError, match="declared validation"):
        checked_protocol(other)


def test_research_complete_repeat_preserves_frozen_selection(
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
) -> None:
    from citibike.forecasting.experiment import research, research_protocol

    p = forecast_workflow
    pointers = {
        str(f): f.read_bytes() for f in (p["root"] / "f4").glob("active_*.json")
    }
    protocol = research_protocol(p["root"], p["dataset"])
    run = research(p["root"], protocol, isolated=False)
    result = checked(run)
    assert result["status"] == "completed", result["outcome_counts"]
    assert not result["final_test_accessed"]
    assert result["outcome_counts"]["failed"] == 0
    population: dict[tuple[int, str], set[str]] = {}
    repeat: dict[tuple[str, str], str] = {}
    for item in result["trials"]:
        r = checked(Path(item["path"]))
        key = (r["fold"], r["prediction_type"])
        population.setdefault(key, set()).add(r["evaluation_rows_hash"])
        if r["fold"] == 0:
            repeat[r["prediction_type"], r["model_name"]] = r["prediction_values_hash"]
    assert all(len(hashes) == 1 for hashes in population.values())
    for kind in ("departures", "arrivals"):
        assert (
            repeat[kind, "lightgbm_bounded"] == repeat[kind, "lightgbm_bounded__repeat"]
        )
    assert research(p["root"], protocol, isolated=False) == run
    from citibike.forecasting.verify import research_evidence

    independent = research_evidence(p["root"])
    assert independent["independent_audit"]["metric_mismatches"] == 0
    assert independent["independent_audit"]["prediction_rows"] > 0
    from citibike.forecasting.verify import research_markdown

    markdown = tmp_path / "f4-forecasting.md"
    markdown.write_text("# Frozen history\n", encoding="utf-8")
    research_markdown(independent, markdown)
    research_markdown(independent, markdown)
    text = markdown.read_text(encoding="utf-8")
    assert text.startswith("# Frozen history\n")
    assert text.count("## Корректирующий validation-only protocol") == 1
    for file, content in pointers.items():
        assert Path(file).read_bytes() == content


def test_independent_diagnostic_oracle_detects_wrong_groups(
    forecast_workflow: dict[str, Path],
) -> None:
    from citibike.forecasting.metrics import error_analysis
    from citibike.forecasting.verify import diagnostic_oracle

    report = checked(forecast_workflow["experiment"])
    file = forecast_workflow["experiment"] / report["results"][0]["predictions"]
    with duckdb.connect() as db:
        frame = db.execute("SELECT * FROM read_parquet(?)", [str(file)]).df()
    expected = error_analysis(frame, frame["predicted"].tolist())
    assert diagnostic_oracle(file, expected) > 0
    expected["station_id"][0]["mae"] += 1
    with pytest.raises(ValueError, match="station/hour"):
        diagnostic_oracle(file, expected)


def test_real_transform_fixture_repeat_and_independent_audit(
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
) -> None:
    p = forecast_workflow
    assert build(p["root"], p["gold"], station_count=4, smoke=True) == p["dataset"]
    assert select(p["root"], p["experiment"]) == p["selection"]
    output = verify(p["root"], tmp_path / "verified.json")
    from citibike.storage import read_json

    r = read_json(output)
    assert r["data_mode"] == "controlled_fixture"
    assert r["label_oracle"]["mismatches"] == 0
    assert r["validation_independent_audit"]["metric_mismatches"] == 0
    assert r["strict_historical_unavailable_rows"] > 0
    data = checked(p["dataset"])
    assert data["zero_labels"] > 0 and data["missing_labels"] > 0
    for kind, choice in r["selection"]["decisions"].items():
        assert kind in ("departures", "arrivals")
        assert choice["champion"] == "seasonal_naive"


@pytest.mark.parametrize(
    "name", ["poisson_default", "lightgbm_default", "catboost_default"]
)
def test_candidates_repeat_and_train_only_encoding(
    forecast_workflow: dict[str, Path],
    name: str,
) -> None:
    with duckdb.connect() as db:
        frame = db.execute(
            """SELECT * FROM read_parquet(?) WHERE eligibility='eligible'
            AND prediction_type='departures'
            ORDER BY station_id,forecast_origin_utc LIMIT 96""",
            [str(forecast_workflow["dataset"] / "examples.parquet")],
        ).df()
    parameters = models.configurations(True)[name]
    first, info = models.fit(frame, parameters)
    second, _ = models.fit(frame, parameters)
    a, _ = models.predict(first, frame)
    b, _ = models.predict(second, frame)
    assert a == pytest.approx(b, rel=1e-10, abs=1e-10)
    outcomes = frame.copy()
    outcomes["actual"] = 1e9
    outcomes["eval_weather_cold"] = True
    unchanged, _ = models.predict(first, outcomes)
    assert unchanged == pytest.approx(a)
    assert info["training_station_levels"] == sorted(
        frame["station_id"].unique().tolist()
    )
    unknown = frame.iloc[:1].copy()
    unknown["station_id"] = "unseen_station"
    result, _ = models.predict(first, unknown)
    assert result[0] >= 0


def test_validation_future_feature_is_rejected(
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
) -> None:
    with duckdb.connect() as db:
        db.execute(
            """COPY(SELECT * REPLACE(CASE
            WHEN forecast_origin_utc>=TIMESTAMPTZ '2025-04-01 00:00:00+00'
                THEN forecast_origin_utc+INTERVAL '1 day'
                ELSE feature_available_at END AS feature_available_at)
            FROM read_parquet($input)) TO $output (FORMAT PARQUET)""",
            {
                "input": str(forecast_workflow["dataset"] / "examples.parquet"),
                "output": str(tmp_path / "examples.parquet"),
            },
        )
        with pytest.raises(ValueError, match="Future"):
            split_frames(
                db,
                tmp_path,
                utc("2025-04-01T00:00:00+00:00"),
                utc("2025-04-03T00:00:00+00:00"),
                1024,
            )


def test_independent_metric_oracle_detects_wrong_report(
    forecast_workflow: dict[str, Path],
) -> None:
    path = forecast_workflow["experiment"]
    result = checked(path)["results"][0]
    wrong = deepcopy(result["metrics"])
    wrong["all"]["mae"] += 1
    with pytest.raises(ValueError, match="metric mismatch"):
        oracle_file(path / result["predictions"], wrong)


def test_mixed_dataset_version_is_rejected(forecast_workflow: dict[str, Path]) -> None:
    p = forecast_workflow
    reports = [
        checked(p[k])
        for k in ("dataset", "experiment", "selection", "final", "model", "forecast")
    ]
    provenance(reports)
    reports[4]["dataset_id"] = "wrong"
    with pytest.raises(ValueError, match="Mixed F4 dataset"):
        provenance(reports)


def test_promotion_gate_and_untouched_test_lock(
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
) -> None:
    baseline = [
        {
            "fold": n,
            "metrics": {
                "all": {"mae": 2, "n": 200},
                "activity:high": {"mae": 2, "n": 200},
            },
        }
        for n in range(3)
    ]
    candidate: list[dict[str, Any]] = [
        {
            **r,
            "metrics": {
                "all": {"mae": 1, "n": 200},
                "activity:high": {"mae": 1, "n": 200},
            },
            "measurements": {
                "fit_seconds": 1,
                "inference_seconds": 1,
                "peak_rss_bytes": 1024,
                "warnings": [],
            },
        }
        for r in baseline
    ]
    assert promotion(candidate, baseline, strict_validation=True)["promote"]
    assert not promotion(candidate, baseline, strict_validation=False)["promote"]
    candidate[0]["metrics"]["activity:high"]["mae"] = 10
    assert not promotion(candidate, baseline, strict_validation=True)["promote"]
    p = forecast_workflow
    selection = checked(p["selection"])
    selection["id"] = "f" * 64
    path = tmp_path / "selection"
    path.mkdir()
    write_json(path / "manifest.json", selection)
    with pytest.raises(ValueError, match="already locked"):
        final_test(p["root"], path)


def test_strict_inference_repeat_unknown_stale_and_schema(
    forecast_workflow: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = forecast_workflow
    forecast = checked(p["forecast"])
    origin = utc(forecast["forecast_origin_utc"])
    assert utc(forecast["issued_at"]) >= origin
    assert inference.predict(p["root"], p["model"], origin) == p["forecast"]
    assert forecast["rows"] == 4 * 24 * 2 and forecast["valid_rows"] == forecast["rows"]
    unknown = inference.predict(p["root"], p["model"], stations=["unknown"])
    assert checked(unknown)["status"] == "unavailable"
    assert active(p["root"], "forecasts") == p["forecast"]
    with pytest.raises(ValueError, match="unavailable at origin"):
        inference.predict(p["root"], p["model"], origin - timedelta(days=1))
    monkeypatch.setattr(inference, "MAX_AGE_DAYS", 0)
    stale = inference.predict(p["root"], p["model"])
    assert checked(stale)["valid_rows"] == 0
    assert active(p["root"], "forecasts") == p["forecast"]
    monkeypatch.setattr(inference, "SCHEMA", "incompatible")
    with pytest.raises(ValueError, match="schema mismatch"):
        inference.predict(p["root"], p["model"])
    assert SCHEMA != "incompatible"
