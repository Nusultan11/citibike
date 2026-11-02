import importlib
import subprocess
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pytest

from citibike import decision_research as research
from citibike.decision_research import analytics as decision_analytics
from citibike.decision_research import features, models
from citibike.decision_research import verify as oracle
from citibike.decision_research.readiness import readiness
from citibike.features.contracts import TYPES
from citibike.forecasting.artifacts import checked
from citibike.forecasting.feature_research import save_frame
from citibike.forecasting.models import load as load_model
from citibike.forecasting.models import save as save_model
from citibike.storage import read_json, write_json


@pytest.fixture(scope="module")
def protocol_path(forecast_workflow: dict[str, Path]) -> Path:
    return research.freeze(
        forecast_workflow["root"],
        forecast_workflow["dataset"],
        None,
        populations={"cohort64": forecast_workflow["dataset"]},
    )


@pytest.fixture(scope="module")
def outcome_path(forecast_workflow: dict[str, Path], protocol_path: Path) -> Path:
    return research.run(forecast_workflow["root"], protocol_path, isolated=False)


def first_item(path: Path) -> dict[str, Any]:
    plan = checked(path)["plan"]
    return {
        "stage": "S1_features",
        "population": "cohort64",
        "fold": "T0",
        "prediction_type": "departures",
        "name": "seasonal_naive",
        "config": plan["feature_variants"]["seasonal_naive"],
    }


def dense_file(tmp_path: Path, rows: list[tuple[Any, ...]]) -> Path:
    path = tmp_path / "dense.parquet"
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'")
        db.execute("""CREATE TABLE dense(station_id VARCHAR,hour_utc TIMESTAMPTZ,
            month_key VARCHAR,departures INT,arrivals INT,
            label_available_at TIMESTAMPTZ)""")
        db.executemany("INSERT INTO dense VALUES (?,?,?,?,?,?)", rows)
        db.execute("COPY dense TO ? (FORMAT PARQUET)", [str(path)])
    return path


def request_frame(stations: list[str], origin: datetime) -> Any:
    pd = importlib.import_module("pandas")
    target = origin + timedelta(hours=1)
    return pd.DataFrame(
        [
            {
                "station_id": s,
                "prediction_type": "departures",
                "forecast_origin_utc": origin,
                "target_hour_utc": target,
                "horizon_h": 1,
                "weekday": target.weekday(),
                "local_hour": target.hour - 4,
                "month": target.month,
                "holiday": 0,
                "feature_available_at": origin - timedelta(days=1),
                "max_event_at": origin - timedelta(days=2),
            }
            for s in stations
        ]
    ).astype({"forecast_origin_utc": "datetime64[us, UTC]"})


def published_rows() -> list[tuple[Any, ...]]:
    rows = []
    available = {
        "2025-05": datetime(2025, 8, 1, tzinfo=UTC),
        "2025-06": datetime(2025, 9, 1, tzinfo=UTC),
    }
    for month, start in (
        ("2025-05", datetime(2025, 5, 1, 4, tzinfo=UTC)),
        ("2025-06", datetime(2025, 6, 1, 4, tzinfo=UTC)),
    ):
        for hour in range(72):
            when = start + timedelta(hours=hour)
            rows.append(("A", when, month, hour % 3, 1, available[month]))
            rows.append(("B", when, month, 0, 0, available[month]))
    return rows


def test_catalog_records_formula_lineage_availability_and_nulls() -> None:
    for name, entry in features.CATALOG.items():
        assert set(entry) == {
            "group",
            "formula",
            "dtype",
            "lineage",
            "availability",
            "max_availability_lag",
            "null_semantics",
            "pit_check",
        }, name
    assert set(features.GROUPS) == {"A", "C", "P", "S"}
    assert len(features.CATALOG) < 40


def test_missing_history_never_becomes_zero(tmp_path: Path) -> None:
    dense = dense_file(tmp_path, published_rows())
    origin = datetime(2025, 7, 15, tzinfo=UTC)
    frame, _ = features.enrich(
        request_frame(["A", "UNKNOWN"], origin), dense, origin.isoformat()
    )
    unknown = frame[frame["station_id"] == "UNKNOWN"].iloc[0]
    assert unknown["hist_mean"] != unknown["hist_mean"]
    assert unknown["hist_zero_share"] != unknown["hist_zero_share"]
    assert unknown["cell_n"] != unknown["cell_n"]
    assert unknown["history_depth"] == "none"
    # Known frozen defect D1, measured by the verifier: empty cell history gives
    # cell_std 0 instead of NULL. This test pins the defect until a new protocol.
    assert unknown["cell_std"] == 0


def test_future_label_is_invisible_until_published(tmp_path: Path) -> None:
    dense = dense_file(tmp_path, published_rows())
    before = datetime(2025, 8, 15, tzinfo=UTC)
    after = datetime(2025, 9, 15, tzinfo=UTC)
    early, _ = features.enrich(request_frame(["A"], before), dense, before.isoformat())
    late, _ = features.enrich(request_frame(["A"], after), dense, after.isoformat())
    assert early["hist_n"].iloc[0] == 72
    assert late["hist_n"].iloc[0] == 144
    capped, _ = features.enrich(request_frame(["A"], after), dense, before.isoformat())
    assert capped["hist_n"].iloc[0] == 72


@pytest.mark.parametrize(
    "column", ["d_available_at", "d_max_event_at", "feature_available_at"]
)
def test_gate_rejects_future_availability_and_events(
    tmp_path: Path, column: str
) -> None:
    dense = dense_file(tmp_path, published_rows())
    origin = datetime(2025, 8, 15, tzinfo=UTC)
    frame, _ = features.enrich(request_frame(["A"], origin), dense, origin.isoformat())
    frame[column] = origin + timedelta(days=30)
    with pytest.raises(ValueError, match="PIT gate"):
        features.gate(frame, origin.isoformat())


def test_preprocessing_flags_and_train_only_filter(tmp_path: Path) -> None:
    pd = importlib.import_module("pandas")
    rows = []
    available = datetime(2025, 8, 1, tzinfo=UTC)
    start = datetime(2025, 5, 1, 4, tzinfo=UTC)
    for hour in range(720):
        when = start + timedelta(hours=hour)
        quiet = 200 <= hour < 300
        count = (0, 0) if quiet else (3, 2)
        rows.append(("1000.01", when, "2025-05", *count, available))
    dense = dense_file(tmp_path, rows)
    origin = datetime(2025, 7, 15, tzinfo=UTC)
    frame = request_frame(["1000.01", "1234.56", "Shop Morgan"], origin)
    enriched, _ = features.enrich(frame, dense, origin.isoformat())
    assert enriched.set_index("station_id")["anomalous_id"].to_dict() == {
        "1000.01": False,
        "1234.56": True,
        "Shop Morgan": True,
    }
    inside = frame.head(1).copy()
    inside["target_hour_utc"] = pd.Timestamp(start + timedelta(hours=250))
    closed, _ = features.enrich(inside, dense, origin.isoformat())
    assert bool(closed["suspect_closure"].iloc[0])
    assert len(features.train_filter(enriched, preprocess=True)) == 1
    assert len(features.train_filter(enriched, preprocess=False)) == 3


def test_anomalous_rule_keeps_real_id_formats() -> None:
    for station in ("6140.05", "6242.1", "JC032"):
        assert not features.anomalous(station)
    for station in ("1234.56", "5308.04_", "5348.06_old", "Shop Morgan"):
        assert features.anomalous(station)


def test_preprocessing_retains_extreme_values() -> None:
    pd = importlib.import_module("pandas")
    frame = pd.DataFrame(
        {
            "actual": [0.0, 221.0],
            "anomalous_id": [False, False],
            "suspect_closure": [False, False],
        }
    )
    assert features.train_filter(frame, preprocess=True)["actual"].max() == 221.0


def test_dst_and_holiday_distance_are_calendar_only() -> None:
    assert features.holiday_distance(datetime(2025, 7, 4).date()) == (0, 0)
    assert features.holiday_distance(datetime(2025, 7, 2).date())[0] == 2
    assert features.holiday_distance(datetime(2025, 7, 6).date())[1] == 2
    assert features.holiday_distance(datetime(2025, 8, 15).date()) == (7, 7)


@pytest.mark.parametrize("algorithm", ["poisson", "lightgbm", "catboost", "hurdle"])
def test_adapters_accept_missing_unknown_and_keep_saved_parity(
    forecast_workflow: dict[str, Path], protocol_path: Path, algorithm: str
) -> None:
    plan = checked(protocol_path)["plan"]
    inputs_path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(inputs_path)["splits"][0]
    train = research.read_frame(inputs_path / split["train_file"])
    train = train[train["prediction_type"] == "departures"]
    scoring = research.read_frame(inputs_path / split["evaluation_file"])
    scoring = scoring[scoring["prediction_type"] == "departures"].copy()
    params = research.bounded(True)[algorithm]
    config = research.config(algorithm, ["A", "C", "P", "S"], params)
    fitted, measure = models.fit(train, config)
    scoring.loc[scoring.index[:4], "station_id"] = "NEVER_SEEN"
    scoring.loc[scoring.index[:4], "hist_mean"] = float("nan")
    values, _ = models.predict(fitted, scoring)
    assert len(values) == len(scoring) and min(values) >= 0
    assert measure["training_rows"] == len(train)
    path = forecast_workflow["root"] / f"{algorithm}.joblib"
    save_model(path, fitted)
    assert models.predict(load_model(path), scoring)[0] == values


def test_hurdle_is_occurrence_times_conditional_intensity(protocol_path: Path) -> None:
    np = importlib.import_module("numpy")
    plan = checked(protocol_path)["plan"]
    inputs_path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(inputs_path)["splits"][0]
    train = research.read_frame(inputs_path / split["train_file"])
    train = train[train["prediction_type"] == "departures"].copy()
    train.loc[train.index[::2], "actual"] = 0
    config = research.config("hurdle", [], research.bounded(True)["hurdle"])
    fitted, _ = models.fit(train, config)
    pd = importlib.import_module("pandas")
    x = train[fitted["feature_names"]].copy()
    x["station_id"] = pd.Categorical(
        x["station_id"], categories=fitted["station_levels"]
    )
    expected = fitted["occurrence"].predict_proba(x)[:, 1] * np.maximum(
        fitted["intensity"].predict(x), 1.0
    )
    assert np.allclose(models.predict(fitted, train)[0], expected)
    calibration = models.occurrence_calibration(fitted, train)
    assert calibration and 0 <= calibration["expected_calibration_error"] <= 1


def test_baseline_rejects_features_and_invalid_outputs(protocol_path: Path) -> None:
    with pytest.raises(ValueError, match="Baseline"):
        models.fit(None, research.config("seasonal_naive", ["A"], {}))
    with pytest.raises(ValueError, match="groups"):
        models.names(["S", "A"])
    plan = checked(protocol_path)["plan"]
    inputs_path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(inputs_path)["splits"][0]
    scoring = research.read_frame(inputs_path / split["evaluation_file"]).copy()
    fitted, _ = models.fit(scoring, research.config("seasonal_naive", [], {}))
    scoring["seasonal_naive"] = -1.0
    with pytest.raises(ValueError, match="Invalid decision research output"):
        models.predict(fitted, scoring)


def test_search_is_seeded_bounded_and_inside_the_space() -> None:
    first = research.search_space(False)
    assert first == research.search_space(False)
    assert {k: len(v) for k, v in first.items()} == {
        "lightgbm": 20,
        "catboost": 6,
        "hurdle": 8,
    }
    for params in first["catboost"]:
        assert all(params[k] in research.SPACE["catboost"][k] for k in params)


def test_selection_rules_prefer_fewer_columns_and_reject_failures() -> None:
    variants = {
        "lightgbm__base": research.config("lightgbm", [], {}),
        "lightgbm__A": research.config("lightgbm", ["A"], {}),
        "catboost__base": research.config("catboost", [], {}),
    }

    def rows(name: str, value: float, status: str = "success") -> list[dict[str, Any]]:
        return [
            {"name": name, "prediction_type": kind, "status": status, "mae": value}
            for kind in TYPES
        ]

    results = [
        *rows("lightgbm__base", 1.0),
        *rows("lightgbm__A", 0.999),
        *rows("catboost__base", 1.0, "failed"),
    ]
    chosen = research.select_features(results, variants)
    assert chosen["lightgbm"]["groups"] == []
    assert "catboost" not in chosen
    searched = {
        "lightgbm__bounded": variants["lightgbm__base"],
        "lightgbm__search00": variants["lightgbm__A"],
    }
    kept = research.select_params(
        [*rows("lightgbm__bounded", 1.0), *rows("lightgbm__search00", 0.999)], searched
    )
    assert kept["lightgbm:departures"]["name"] == "lightgbm__bounded"
    better = research.select_params(
        [*rows("lightgbm__bounded", 1.0), *rows("lightgbm__search00", 0.9)], searched
    )
    assert better["lightgbm:departures"]["name"] == "lightgbm__search00"


def test_protocol_binds_code_budgets_and_test_lock(
    forecast_workflow: dict[str, Path], protocol_path: Path
) -> None:
    report = research.checked_protocol(protocol_path)
    plan = report["plan"]
    assert plan["final_test_accessed"] is False
    assert plan["hypotheses"] and plan["decision_rule"] and plan["stopping"]
    assert plan["trial_upper_bound"] > 0
    assert all(
        f["end"] <= plan["final_window_not_used"][0] for f in plan["holdout_folds"]
    )
    assert not set(research.recipe()) & {
        "decision_research/verify.py",
        "decision_research/analytics.py",
    }
    data = checked(forecast_workflow["dataset"])
    forged = deepcopy(data)
    forged["config"]["final"] = [plan["holdout_folds"][0]["start"], "2030-01-01"]
    with pytest.raises(ValueError, match="final test"):
        research.folds(forged, tuning=False)


def test_run_requires_remote_proof_for_real_data(
    forecast_workflow: dict[str, Path], protocol_path: Path, tmp_path: Path
) -> None:
    source = protocol_path / "manifest.json"
    report = read_json(source)
    report["plan"]["data_mode"] = "real_official"
    copy = tmp_path / "protocol"
    copy.mkdir()
    write_json(copy / "manifest.json", report)
    with pytest.raises(ValueError, match="mismatch"):
        research.run(forecast_workflow["root"], copy)


def test_remote_proof_refuses_absent_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Result:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout

    def fake(args: list[str], **_: Any) -> Result:
        if args[1] == "rev-parse":
            return Result("aaa\n")
        if args[1] == "ls-remote":
            return Result("bbb\trefs/heads/main\n")
        return Result("")

    monkeypatch.setattr(subprocess, "run", fake)
    with pytest.raises(ValueError, match="not on origin"):
        research.remote_proof("id")


def test_worker_failures_are_durable_and_substitution_is_rejected(
    forecast_workflow: dict[str, Path], protocol_path: Path, tmp_path: Path
) -> None:
    root = forecast_workflow["root"]
    work = research.research_root(root)
    report = research.checked_protocol(protocol_path)
    item = first_item(protocol_path)
    request = research.request_for(item, report, protocol_path)
    research.validate_request(request, report)
    for field, value in (
        ("dataset_id", "other"),
        ("fit_asof", "2030-01-01T00:00:00+00:00"),
        ("evaluation_rows_hash", "changed"),
        ("train_file_sha256", "changed"),
    ):
        with pytest.raises(ValueError):
            research.validate_request({**request, field: value}, report)
    with pytest.raises(ValueError):
        research.validate_request(
            {**request, "config": {**request["config"], "seed": 7}}, report
        )
    crashed = research.trial(work, {**request, "evaluation_rows_hash": "changed"})
    outcome = checked(crashed)
    assert outcome["status"] == "failed"
    bad = research.trial(
        work,
        {**request, "train_file_sha256": "0" * 64},
        isolated=False,
    )
    assert checked(bad)["error"]["type"] == "ValueError"


def test_corrupted_artifact_and_trial_are_detected(outcome_path: Path) -> None:
    row = checked(outcome_path)["stages"]["S1_features"][0]
    with pytest.raises(ValueError, match="Protected input changed"):
        research.assert_hashes({row["path"] + "/manifest.json": "0" * 64})


def test_complete_run_repeat_oracle_and_replay(
    forecast_workflow: dict[str, Path],
    protocol_path: Path,
    outcome_path: Path,
    tmp_path: Path,
) -> None:
    root = forecast_workflow["root"]
    outcome = checked(outcome_path)
    # Fixture IDs are all non-canonical, so preprocess=true leaves no training rows:
    # those trials must fail durably and must not reach any selection.
    every = [row for rows in outcome["stages"].values() for row in rows]
    failed = {r["name"] for r in every if r["status"] == "failed"}
    assert failed == {"lightgbm__ACPS__pre", "catboost__ACPS__pre"}
    assert outcome["status"] == "partial"
    assert outcome["outcome_counts"]["not_run"] == 0
    assert not any(v["preprocess"] for v in outcome["feature_selection"].values())
    assert research.run(root, protocol_path, isolated=False) == outcome_path
    stage = outcome["stages"]["S1_features"]
    assert {r["provenance"] for r in stage} >= {
        "repeat_of_F4_baseline",
        "new_fit_on_reused_window",
    }
    assert any(
        r["provenance"] == "new_fit_on_never_scored_window"
        for r in outcome["stages"]["S3_holdout"]
    )
    replay = oracle.frozen_replay(root, stage)
    assert replay and max(r["max_absolute_difference"] for r in replay) == 0
    plan = checked(protocol_path)["plan"]
    path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(path)["splits"][0]
    source = oracle.Source(forecast_workflow["dataset"] / "dense.parquet")
    audit = oracle.feature_oracle(
        path / split["train_file"], source, split["fit_asof"], split["band_thresholds"]
    )
    assert audit["mismatches"] == 0 and audit["feature_values"] > 0
    report, rows = oracle.trial_check(stage[0], plan, checked(protocol_path))
    assert rows == report["metrics"]["all"]["n"]
    assert oracle.selection_chain(outcome, plan)["parameter_selection"] == "equal"
    forged = deepcopy(outcome)
    forged["feature_selection"]["lightgbm"]["groups"] = ["P"]
    with pytest.raises(ValueError, match="frozen rule"):
        oracle.selection_chain(forged, plan)


def test_feature_oracle_detects_missing_as_zero(
    forecast_workflow: dict[str, Path], protocol_path: Path, tmp_path: Path
) -> None:
    plan = checked(protocol_path)["plan"]
    path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(path)["splits"][1]
    frame = research.read_frame(path / split["evaluation_file"])
    frame["season_index"] = frame["season_index"].fillna(0.0)
    frame["hist_cv"] = frame["hist_cv"].fillna(0.0)
    frame.loc[frame.index[:3], "hist_mean"] = float("nan")
    broken = tmp_path / "broken.parquet"
    save_frame(broken, frame)
    source = oracle.Source(forecast_workflow["dataset"] / "dense.parquet")
    with pytest.raises(ValueError, match="Missing history|mismatch"):
        oracle.feature_oracle(
            broken, source, split["fit_asof"], split["band_thresholds"]
        )


def test_feature_oracle_detects_shifted_value(
    forecast_workflow: dict[str, Path], protocol_path: Path, tmp_path: Path
) -> None:
    plan = checked(protocol_path)["plan"]
    path = Path(plan["populations"]["cohort64"]["inputs"])
    split = checked(path)["splits"][1]
    frame = research.read_frame(path / split["evaluation_file"])
    frame["shrunk_cell_mean"] = frame["shrunk_cell_mean"] * 1.01
    broken = tmp_path / "shifted.parquet"
    save_frame(broken, frame)
    source = oracle.Source(forecast_workflow["dataset"] / "dense.parquet")
    with pytest.raises(ValueError, match="mismatch"):
        oracle.feature_oracle(
            broken, source, split["fit_asof"], split["band_thresholds"]
        )


def paired_frames(shift: float) -> tuple[Any, Any]:
    pd = importlib.import_module("pandas")
    rows = []
    start = datetime(2025, 6, 1, 4, tzinfo=UTC)
    for fold in ("H0", "H1", "H2", "H3"):
        for day in range(6):
            for station in range(6):
                hour = start + timedelta(days=day + 30 * int(fold[1]))
                rows.append(
                    {
                        "fold": fold,
                        "station_id": str(station),
                        "forecast_origin_utc": hour - timedelta(hours=1),
                        "target_hour_utc": hour,
                        "horizon_h": 1,
                        "actual": 4.0,
                        "predicted": 1.0 + 0.1 * station,
                    }
                )
    left = pd.DataFrame(rows)
    right = left.copy()
    right["predicted"] = right["predicted"] + shift
    return left, right


def test_paired_bootstrap_rejects_mismatched_rows_and_finds_a_real_gap() -> None:
    left, right = paired_frames(1.0)
    result = oracle.bootstrap(right, left, replicates=200, alpha=0.01)
    assert result["macro_mae_difference_left_minus_right"] == pytest.approx(-1.0)
    assert result["familywise_interval"][1] < 0
    assert result["folds_left_better"] == 4
    with pytest.raises(ValueError, match="Paired populations"):
        oracle.bootstrap(left.iloc[1:], right.iloc[:-1], replicates=10)


def test_sign_flip_and_holm_are_exact() -> None:
    assert oracle.sign_flip([-1.0, -1.0, -1.0, -1.0]) == 0.125
    assert oracle.sign_flip([-1.0, 1.0, -1.0, 1.0]) == 1.0
    items = [{"bootstrap_sign_p": p} for p in (0.01, 0.02, 0.5)]
    oracle.holm(items)
    assert [i["holm_p"] for i in items] == [0.03, 0.04, 0.5]


def test_decision_needs_gate_and_familywise_interval() -> None:
    good = {
        "majority": True,
        "degraded_segments": [],
        "resources_and_fit_ok": True,
        "wins": 3,
        "windows": 4,
        "macro_mae": 1.0,
    }
    gates = {
        (population, "departures", "catboost_selected"): good
        for population in ("cohort64", "cohort128")
    }

    def comparison(population: str, upper: float) -> dict[str, Any]:
        return {
            "population": population,
            "target": "departures",
            "left": "catboost_selected",
            "right": "seasonal_naive",
            "primary": True,
            "familywise_interval": [-1.0, upper],
        }

    passed = oracle.decide(
        gates,
        [comparison("cohort64", -0.1), comparison("cohort128", -0.1)],
        "departures",
    )
    assert passed["research_candidate"] == "catboost_selected"
    wide = oracle.decide(
        gates,
        [comparison("cohort64", -0.1), comparison("cohort128", 0.1)],
        "departures",
    )
    assert wide["research_candidate"] == "seasonal_naive"
    gates[("cohort128", "departures", "catboost_selected")] = {
        **good,
        "degraded_segments": ["H0:activity:new"],
    }
    blocked = oracle.decide(
        gates,
        [comparison("cohort64", -0.1), comparison("cohort128", -0.1)],
        "departures",
    )
    assert blocked["research_candidate"] == "seasonal_naive"


def test_rank_correlation_matches_sql() -> None:
    left, right = [1.0, 2.0, 2.0, 5.0, 9.0], [2.0, 1.0, 4.0, 3.0, 7.0]
    with duckdb.connect() as db:
        db.execute("CREATE TABLE t(a DOUBLE,b DOUBLE)")
        db.executemany(
            "INSERT INTO t VALUES (?,?)", list(zip(left, right, strict=True))
        )
        rho = db.execute(
            """SELECT corr(x,y) FROM (SELECT rank() OVER(ORDER BY a)
                +(count(*) OVER(PARTITION BY a)-1)/2.0 x,rank() OVER(ORDER BY b)
                +(count(*) OVER(PARTITION BY b)-1)/2.0 y FROM t)"""
        ).fetchone()
    assert rho is not None
    assert oracle.stdlib_rank_correlation(left, right) == pytest.approx(rho[0])


def test_analytics_keep_denominators_and_unknown_separate(
    forecast_workflow: dict[str, Path],
) -> None:
    db = decision_analytics.connect(forecast_workflow["gold"])
    try:
        trend = decision_analytics.trends(db)
        category = decision_analytics.categories(db)
        pattern = decision_analytics.patterns(db)
        share = decision_analytics.concentration(db)
    finally:
        db.close()
    assert trend["kind"] == "fact" and trend["result"]["months"]
    assert all(row["observed_source_hours"] > 0 for row in trend["result"]["months"])
    for block in category["result"]["shares"].values():
        assert "unknown" not in block["shares"]
        assert sum(block["shares"].values()) == pytest.approx(1.0)
    assert pattern["result"]["hour_of_week"]
    assert all(0 <= row["top10pct_share"] <= 1 for row in share["result"]["monthly"])
    assert {
        "user_decision",
        "definition",
        "time_domain",
        "grain",
        "sql",
        "limits",
    } <= set(trend)


def test_full_analytics_sections_on_fixture_outcome(
    forecast_workflow: dict[str, Path], outcome_path: Path, tmp_path: Path
) -> None:
    scenario = tmp_path / "scenario"
    scenario.mkdir()
    write_json(
        scenario / "manifest.json",
        {
            "id": "stub",
            "data_as_of": "2026-10-30T06:36:54+00:00",
            "gate": {"eligible": ["A"]},
            "comparisons": [],
            "assumptions": ["controlled test stub"],
        },
    )
    write_json(tmp_path / "f5" / "active.json", {"path": str(scenario)})
    path = decision_analytics.analytics(tmp_path, outcome_path)
    report = checked(path)
    assert set(report["sections"]) == {
        "1_trends",
        "2_patterns",
        "3_concentration",
        "4_imbalance",
        "5_categories",
        "6_coverage_quality",
        "7_errors_vs_flows",
        "8_f5_policies",
    }
    kinds = {k: v["kind"] for k, v in report["sections"].items()}
    assert kinds["1_trends"] == "fact"
    assert kinds["6_coverage_quality"] == "prediction"
    assert kinds["8_f5_policies"] == "simulation"
    for section in report["sections"].values():
        assert section["user_decision"] and section["limits"]
    assert oracle.analytics_oracle(report)["mismatches"] == 0
    board = decision_analytics.dashboard(report["sections"], [])
    assert board["forecast_quality"]["kind"] == "prediction"
    assert board["scenario_policies"]["kind"] == "simulation"


def test_f5_oracle_recomputes_policy_means(tmp_path: Path) -> None:
    line = {
        "scale": 1,
        "order": "uniform",
        "policy": "no_action",
        "outcome": {"rejected": {"A": {"departures": 1, "arrivals": 2}}},
    }
    write_json(tmp_path / "row.json", line)
    (tmp_path / "outcomes.jsonl").write_text(
        (tmp_path / "row.json").read_text(encoding="utf-8").replace("\n", "") + "\n",
        encoding="utf-8",
    )
    cell: dict[str, Any] = {
        "scale": 1,
        "order": "uniform",
        "policies": {"no_action": {"unserved_total_mean": 3.0}},
    }
    section = {"result": {"comparisons": [cell]}}
    assert oracle.f5_oracle(tmp_path, section)["mismatches"] == 0
    cell["policies"]["no_action"]["unserved_total_mean"] = 2.0
    assert oracle.f5_oracle(tmp_path, section)["mismatches"] == 1


def test_readiness_never_summarises_holdout_actuals_and_finds_no_leak(
    forecast_workflow: dict[str, Path],
) -> None:
    path = readiness(forecast_workflow["root"], forecast_workflow["dataset"])
    report = checked(path)
    cohort = report["cohorts"]["cohort64"]
    assert cohort["leakage"]["future_feature_rows"] == 0
    assert cohort["grain_duplicates"] == 0
    assert cohort["labels"][0]["label_type"] == "departures"
    folds = report["folds"]["cohort64"]
    assert {f["fold"] for f in folds} >= {"T0", "H0"}
    for fold in folds:
        assert fold["train_rows_after_availability_or_inside_embargo"] == 0
        assert fold["train_evaluation_target_overlap"] == 0
        assert "actual_mean" not in str(fold)
    assert "cohort64" in report["composition_bias"]
