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
