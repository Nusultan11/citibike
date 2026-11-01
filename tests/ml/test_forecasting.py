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


@pytest.fixture(scope="module")
def feature_protocol(forecast_workflow: dict[str, Path]) -> Path:
    from citibike.forecasting.feature_research import protocol

    return protocol(forecast_workflow["root"], forecast_workflow["dataset"])


def feature_request(path: Path) -> dict[str, Any]:
    from citibike.forecasting.feature_research import checked_protocol

    protocol = checked_protocol(path)
    p = protocol["plan"]["populations"]["cohort64"]
    inputs = Path(p["inputs"])
    split = checked(inputs)["splits"][0]
    return {
        "protocol_id": protocol["id"],
        "protocol_path": str(path),
        "population": "cohort64",
        "dataset_id": p["dataset_id"],
        "seed": 42,
        "fold": 0,
        "prediction_type": "departures",
        "name": "seasonal_naive",
        "parameters": protocol["plan"]["variants"]["seasonal_naive"],
        "fit_asof": split["fit_asof"],
        "budgets": protocol["plan"]["budgets"],
        "evaluation_weather": protocol["plan"]["evaluation_weather"],
        "evaluation_rows_hash": split["populations"]["departures"][
            "evaluation_rows_hash"
        ],
        **{
            field: str(inputs / split[field])
            for field in ("train_file", "evaluation_file")
        },
        **{
            field + "_sha256": split[field + "_sha256"]
            for field in ("train_file", "evaluation_file")
        },
    }


def test_feature_calendar_dst_and_numeric_formula() -> None:
    import importlib

    from citibike.features.contracts import calendar_values
    from citibike.features.research import calendar

    pd = importlib.import_module("pandas")
    hours = pd.to_datetime(
        [
            "2025-11-02T05:00Z",
            "2025-11-02T06:00Z",
            "2025-03-09T06:00Z",
            "2025-03-09T07:00Z",
        ]
    )
    frame = pd.DataFrame([{"target_hour_utc": h, **calendar_values(h)} for h in hours])
    result = calendar(frame)
    assert len(result) == 4
    assert result.iloc[0]["local_hour_sin"] == result.iloc[1]["local_hour_sin"]
    assert result["local_hour"].tolist() == [1, 1, 1, 3]
    assert result["local_hour_sin"].between(-1, 1).all()
    assert result["week_of_year"].between(1, 53).all()


def test_feature_published_history_missing_zero_and_fit_cutoff(tmp_path: Path) -> None:
    import importlib

    from citibike.features.contracts import calendar_values
    from citibike.features.research import enrich, gate
    from citibike.forecasting.feature_research import save_frame

    pd = importlib.import_module("pandas")
    dense = pd.DataFrame(
        [
            {
                "station_id": "s",
                "hour_utc": pd.Timestamp(hour),
                "month_key": month,
                "label_available_at": pd.Timestamp(available),
                "departures": value,
                "arrivals": value,
            }
            for hour, month, available, value in (
                ("2025-01-03T12:00Z", "2025-01", "2025-02-15T00:00Z", 0.0),
                ("2025-02-03T12:00Z", "2025-02", "2025-03-15T00:00Z", None),
                ("2025-03-03T12:00Z", "2025-03", "2025-04-15T00:00Z", 99.0),
            )
        ]
    )
    path = tmp_path / "dense.parquet"
    save_frame(path, dense)
    origin, target = (
        pd.Timestamp("2025-05-01T00:00Z"),
        pd.Timestamp("2025-05-01T01:00Z"),
    )
    frame = pd.DataFrame(
        [
            {
                "station_id": "s",
                "forecast_origin_utc": origin,
                "target_hour_utc": target,
                "prediction_type": "departures",
                "horizon_h": 1,
                "feature_available_at": pd.Timestamp("2025-02-15T00:00Z"),
                "max_event_at": pd.Timestamp("2025-01-03T12:00Z"),
                **calendar_values(target),
            }
        ]
    )
    result = enrich(frame, path, "2025-04-01T00:00Z")
    assert result.iloc[0]["published_month_mean"] == 0.0
    assert result.iloc[0]["published_month_n"] == 1
    assert pd.isna(result.iloc[0]["prior_month_mean"])
    assert pd.isna(result.iloc[0]["monthly_trend"])
    dense.loc[2, "departures"] = 1000000.0
    save_frame(tmp_path / "future.parquet", dense)
    pd.testing.assert_frame_equal(
        result, enrich(frame, tmp_path / "future.parquet", "2025-04-01T00:00Z")
    )
    result["b_available_at"] = origin + timedelta(hours=1)
    with pytest.raises(ValueError, match="PIT"):
        gate(result, "2025-04-01T00:00Z")


@pytest.mark.parametrize(
    "algorithm", ["lightgbm_bounded", "catboost_bounded", "poisson_bounded"]
)
@pytest.mark.parametrize("groups", [[], ["A"], ["B"], ["A", "B"]])
def test_feature_adapters_numeric_ablation_saved_compatibility(
    feature_protocol: Path, tmp_path: Path, algorithm: str, groups: list[str]
) -> None:
    from citibike.features.research import A, B
    from citibike.forecasting.feature_research import models as feature_models
    from citibike.forecasting.feature_research import read_frame

    request = feature_request(feature_protocol)
    frame = read_frame(Path(request["train_file"]))
    frame = frame[frame["prediction_type"] == "departures"].head(256)
    config = {**models.configurations(True)[algorithm], "research_groups": groups}
    fitted, _ = feature_models.fit(frame, config)
    values, _ = feature_models.predict(fitted, frame)
    expected = set(A if "A" in groups else {}) | set(B if "B" in groups else {})
    assert expected <= set(fitted["feature_names"])
    excluded = (set(A) | set(B)) - expected
    assert not excluded & set(fitted["feature_names"])
    changed = frame.copy()
    for column in excluded:
        changed[column] = 99999.0
    assert feature_models.predict(fitted, changed)[0] == values
    path = tmp_path / "research.joblib"
    models.save(path, fitted)
    assert feature_models.predict(models.load(path), frame)[0] == values
    if algorithm == "poisson_bounded" and groups:
        transform = fitted["model"].steps[0][1]
        numeric = transform.transformers_[1][2]
        assert expected <= set(numeric)


def test_feature_protocol_population_test_isolation_and_durable_failures(
    feature_protocol: Path, forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    from citibike.forecasting.feature_research import (
        checked_protocol,
        protocol,
        trial,
        validate_request,
    )

    p = checked_protocol(feature_protocol)
    assert (
        protocol(forecast_workflow["root"], forecast_workflow["dataset"])
        == feature_protocol
    )
    assert p["plan"]["expanded"]["status"] == "available"
    assert set(p["plan"]["populations"]["cohort64"]["stations"]) <= set(
        p["plan"]["populations"]["cohort128"]["stations"]
    )
    assert {"poisson_bounded__base", "poisson_bounded__A"} <= set(p["plan"]["variants"])
    assert p["plan"]["superseded"] is None
    request = feature_request(feature_protocol)
    bad = {**request, "evaluation_file": str(tmp_path / "final.parquet")}
    with pytest.raises(ValueError, match="isolation"):
        validate_request(bad, p)
    swapped = {**request, "evaluation_weather": {"daily_file": "other.json"}}
    with pytest.raises(ValueError, match="deviated"):
        validate_request(swapped, p)
    failed = trial(forecast_workflow["root"], bad, isolated=False)
    assert checked(failed)["status"] == "failed"
    assert trial(forecast_workflow["root"], bad, isolated=False) == failed
    wrong = {**request, "evaluation_rows_hash": "wrong"}
    assert (
        checked(trial(forecast_workflow["root"], wrong, isolated=False))["status"]
        == "failed"
    )
    timeout = {
        **request,
        "budgets": {**request["budgets"], "worker_wall_seconds": 0.00001},
    }
    result = checked(trial(forecast_workflow["root"], timeout))
    assert result["error"]["type"] == "TimeoutExpired"
    success = checked(trial(forecast_workflow["root"], request))
    assert success["status"] == "success"
    assert p["plan"]["final_test_accessed"] is False


def test_feature_independent_oracle_and_crossed_cluster_comparisons(
    feature_protocol: Path, tmp_path: Path
) -> None:
    from citibike.forecasting.feature_research import read_frame, save_frame
    from citibike.forecasting.feature_research.verify import feature_oracle, paired

    request = feature_request(feature_protocol)
    p = checked(feature_protocol)["plan"]["populations"]["cohort64"]
    path = Path(request["evaluation_file"])
    audit = feature_oracle(
        path, Path(p["dataset"]) / "dense.parquet", request["fit_asof"]
    )
    assert audit["mismatches"] == 0
    frame = read_frame(path)
    frame["seasonal_month_mean"] = 99999.0
    wrong = tmp_path / "wrong.parquet"
    save_frame(wrong, frame)
    with pytest.raises(ValueError, match="Missing source|published feature"):
        feature_oracle(wrong, Path(p["dataset"]) / "dense.parquet", request["fit_asof"])
    frame["predicted"] = frame["actual"] + 1.0
    frame["fold"] = 0
    other = frame.copy()
    other["predicted"] = other["actual"]
    comparison = paired(frame, other, replicates=50)
    assert comparison["macro_MAE_difference_left_minus_right"] == 1.0
    assert comparison["percentile_95"] == [1.0, 1.0]
    with pytest.raises(ValueError, match="populations"):
        paired(frame, other.iloc[1:], replicates=5)


def test_feature_recipe_binds_fit_code_and_excludes_weather_inputs() -> None:
    from citibike.forecasting.feature_research import models as feature_models
    from citibike.forecasting.feature_research import recipe

    files = recipe()
    assert {
        "features/research/__init__.py",
        "forecasting/feature_research/__init__.py",
        "forecasting/feature_research/models.py",
        "forecasting/verify.py",
    } <= set(files)
    assert "forecasting/feature_research/verify.py" not in files
    assert not [n for n in feature_models.names(["A", "B"]) if "weather" in n]


def test_feature_rebuild_is_deterministic_and_batch_independent(
    feature_protocol: Path, tmp_path: Path
) -> None:
    from citibike.forecasting.feature_research import read_frame, save_frame
    from citibike.forecasting.feature_research.verify import reproduce_features

    request = feature_request(feature_protocol)
    p = checked(feature_protocol)["plan"]["populations"]["cohort64"]
    dense = Path(p["dataset"]) / "dense.parquet"
    path = Path(request["evaluation_file"])
    report = reproduce_features(path, dense, request["fit_asof"])
    assert report["full_batch"]["different_columns"] == 0
    assert 0 < report["one_origin_request"]["rows"] < report["rows"]
    frame = read_frame(path)
    frame["week_of_year"] = frame["week_of_year"] + 1
    wrong = tmp_path / "wrong.parquet"
    save_frame(wrong, frame)
    with pytest.raises(ValueError, match="deterministic"):
        reproduce_features(wrong, dense, request["fit_asof"])


def test_feature_decision_needs_both_populations_and_group_needs_agreement() -> None:
    from citibike.forecasting.feature_research.verify import decide, group_verdict

    def score(population: str, name: str, mae: float, wins: int) -> dict[str, Any]:
        gate = {
            "wins": wins,
            "windows": 4,
            "majority": wins > 2,
            "degraded_segments": [],
            "resources_and_fit_ok": True,
        }
        return {
            "population": population,
            "target": "departures",
            "variant": name,
            "macro_mae": mae,
            "gate": gate,
        }

    scores = [
        score("cohort64", "seasonal_naive", 2.0, 0),
        score("cohort128", "seasonal_naive", 2.0, 0),
        score("cohort64", "fragile", 1.0, 4),
        score("cohort128", "fragile", 1.5, 2),
        score("cohort64", "robust", 1.2, 3),
        score("cohort128", "robust", 1.6, 3),
    ]
    result = decide(scores, "departures")
    assert result["research_candidate"] == "robust"
    assert list(result["blockers"]) == ["fragile"]
    assert result["lowest_macro_mae"]["cohort64"] == "fragile"
    alone = [s for s in scores if s["variant"] != "robust"]
    assert decide(alone, "departures")["research_candidate"] == "seasonal_naive"
    folds = [-0.1, -0.1, -0.1, 0.1]
    assert (
        group_verdict(
            {
                "percentile_95": [-0.2, -0.01],
                "fold_differences": folds,
                "folds_left_better": 3,
            }
        )
        == "improves"
    )
    assert (
        group_verdict(
            {
                "percentile_95": [-0.2, 0.01],
                "fold_differences": folds,
                "folds_left_better": 3,
            }
        )
        == "inconclusive"
    )
    assert (
        group_verdict(
            {
                "percentile_95": [-0.2, -0.01],
                "fold_differences": folds,
                "folds_left_better": 2,
            }
        )
        == "inconclusive"
    )
    assert (
        group_verdict(
            {
                "percentile_95": [0.01, 0.2],
                "fold_differences": folds,
                "folds_left_better": 1,
            }
        )
        == "degrades"
    )


def test_feature_complete_research_oracle_and_immutable_repeat(
    feature_protocol: Path, forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    from citibike.forecasting.feature_research import run
    from citibike.forecasting.feature_research.verify import verify as feature_verify
    from citibike.ingestion.archive import checksum
    from citibike.storage import read_json

    root = forecast_workflow["root"]
    result = run(root, feature_protocol, isolated=False)
    assert checked(result)["status"] == "completed"
    before = checksum(result / "manifest.json")
    assert run(root, feature_protocol, isolated=False) == result
    assert checksum(result / "manifest.json") == before
    output = tmp_path / "evidence.json"
    feature_verify(result, output)
    evidence = read_json(output)
    assert evidence["independent_audit"]["metric_mismatches"] == 0
    assert evidence["independent_audit"]["final_window_input_rows"] == 0
    assert len(evidence["scores"]) == 44
    assert len(evidence["decisions"]) == 4
    assert len(evidence["target_decisions"]) == 2
    # Smoke configuration: 2 populations x 3 folds x 2 targets x 11 variants.
    assert evidence["outcome_counts"]["success"] == 132
    assert evidence["superseded_replay"] == {"status": "not_applicable"}
    # Same protocol code path must reproduce every frozen base prediction.
    assert len(evidence["frozen_replay"]) == 8
    assert all(r["max_absolute_difference"] == 0 for r in evidence["frozen_replay"])
    assert {(e["left"], e["right"]) for e in evidence["group_effects"]} >= {
        ("poisson_bounded__A", "poisson_bounded__base"),
        ("catboost_bounded__A+B", "catboost_bounded__A"),
        ("catboost_bounded__A+B", "catboost_bounded__B"),
        ("lightgbm_bounded__B", "lightgbm_bounded__base"),
    }
    assert evidence["root_cause"]["rows"]
    assert all(
        row["concentration"]["stations"] > 0 for row in evidence["root_cause"]["rows"]
    )
    audit = evidence["independent_audit"]
    assert all(
        r["one_origin_request"]["different_columns"] == 0
        for r in audit["feature_reproduction"]
    )
    assert evidence["final_test_accessed"] is False
    assert evidence["operational_promotion"] is False


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
