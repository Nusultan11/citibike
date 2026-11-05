"""Controlled tests validate research mechanics, never real model quality."""

import importlib
import runpy
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from citibike.decision_research.features import GROUPS
from citibike.features.contracts import calendar_values
from citibike.forecasting.feature_research import row_hash
from citibike.ingestion.archive import checksum
from citibike.kaggle_research import models, study
from citibike.kaggle_research.verify import metric, quantiles
from citibike.storage import identity, read_json, write_json


def test_modal_remote_import_never_resolves_client_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RemoteApp:
        def __init__(self, _name: str) -> None:
            pass

        def function(self, **_kwargs: Any) -> Any:
            return lambda function: function

        def local_entrypoint(self) -> Any:
            return lambda function: function

    monkeypatch.setitem(
        sys.modules, "modal", SimpleNamespace(App=RemoteApp, is_local=lambda: False)
    )
    source = Path("research/modal/verify.py").read_text(encoding="utf-8")
    namespace: dict[str, Any] = {
        "__file__": "/root/verify.py",
        "__name__": "modal_remote_import",
    }
    exec(compile(source, "/root/verify.py", "exec"), namespace)
    assert callable(namespace["verify_remote"])
    assert "ROOT" not in namespace
    assert "PAYLOAD" not in namespace


def frame(origin: str) -> Any:
    pd = importlib.import_module("pandas")
    when = datetime.fromisoformat(origin.replace("Z", "+00:00"))
    rows = []
    for target in ("departures", "arrivals"):
        for station in ("7111.1", "7111.10"):
            for horizon in range(1, 25):
                hour = when + timedelta(hours=horizon)
                row = {
                    "station_id": station,
                    "prediction_type": target,
                    "forecast_origin_utc": when,
                    "target_hour_utc": hour,
                    "horizon_h": horizon,
                    "actual": horizon % 6,
                    "seasonal_naive": 2.0,
                    "profile_mean": 2.0,
                    "station_mean": 2.0,
                    "profile_count": 10.0,
                    "known_at": pd.Timestamp("2025-01-01Z".replace("Z", "T00:00Z")),
                    "feature_available_at": when - timedelta(days=1),
                    "max_event_at": when - timedelta(days=2),
                    "d_available_at": when - timedelta(days=1),
                    "d_max_event_at": when - timedelta(days=2),
                    "research_cutoff": when,
                    "label_available_at": when + timedelta(days=45),
                    "eligibility": "eligible",
                    "activity": "middle",
                    "new_known": False,
                    "anomalous_id": False,
                    "suspect_closure": False,
                    "history_depth": "deep",
                    **calendar_values(hour),
                }
                row.update({c: 1.0 for fields in GROUPS.values() for c in fields})
                rows.append(row)
    return pd.DataFrame(rows)


def data() -> tuple[Any, Any, dict[str, Any]]:
    train = frame("2025-08-01T00:00Z")
    evaluation = frame("2025-11-02T04:00Z")
    split = {
        "fit_asof": "2025-11-02T04:00Z",
        "origin_end": "2025-11-03T04:00Z",
        "populations": {
            target: {
                "evaluation_rows_hash": row_hash(
                    evaluation[evaluation["prediction_type"] == target]
                )
            }
            for target in ("departures", "arrivals")
        },
    }
    return train, evaluation, split


def test_pit_dst_and_station_ids_remain_distinct() -> None:
    train, evaluation, split = data()
    study.validate_frames(train, evaluation, split)
    assert evaluation["station_id"].nunique() == 2
    # Fall-back contains two local 01:00 hours with distinct UTC keys.
    repeated = evaluation[
        (evaluation["station_id"] == "7111.1")
        & (evaluation["prediction_type"] == "departures")
        & (evaluation["local_hour"] == 1)
    ]
    assert len(repeated) == 2 and repeated["target_hour_utc"].nunique() == 2


@pytest.mark.parametrize(
    "field",
    [
        "actual",
        "feature_available_at",
        "max_event_at",
        "known_at",
        "label_available_at",
        "eligibility",
        "horizon_h",
    ],
)
def test_rejects_leakage_missing_and_population_changes(field: str) -> None:
    pd = importlib.import_module("pandas")
    train, evaluation, split = data()
    if field == "actual":
        evaluation.loc[0, field] = float("nan")
    elif field == "eligibility":
        evaluation.loc[0, field] = "missing_target"
    elif field == "horizon_h":
        evaluation.loc[0, field] = 25
    elif field == "label_available_at":
        train.loc[0, field] = pd.Timestamp("2026-01-01T00:00Z")
    else:
        evaluation.loc[0, field] = pd.Timestamp("2026-01-01T00:00Z")
    with pytest.raises(ValueError):
        study.validate_frames(train, evaluation, split)


@pytest.mark.parametrize(
    "name",
    [
        "quantile_lightgbm",
        "quantile_empirical",
        "quantile_poisson_sn",
        "spike_lightgbm",
        "lightgbm_l1",
    ],
)
def test_new_adapters_never_fit_evaluation_labels(name: str) -> None:
    train, evaluation, _ = data()
    config = deepcopy(models.configurations()[name])
    if "n_estimators" in config["params"]:
        config["params"].update(n_estimators=3, min_child_samples=2)
    first, _ = models.predictions(train, evaluation, config)
    altered = evaluation.copy()
    altered["actual"] = 1000
    second, _ = models.predictions(train, altered, config)
    columns = [
        c
        for c in first
        if c in ("predicted", "probability", "baseline_probability")
        or c.startswith("q0.")
    ]
    assert first[columns].equals(second[columns])
    if name.startswith("quantile"):
        a, b = study.probabilistic_metrics(first), quantiles(first)
        for key in ("mean_pinball", "coverage_80", "mean_width"):
            assert a[key] == pytest.approx(b[key])


def test_probability_oracle_known_values_and_corruption() -> None:
    pd = importlib.import_module("pandas")
    controlled = pd.DataFrame(
        {"actual": [0, 2, 4], "q0.1": [0, 0, 0], "q0.5": [2, 2, 2], "q0.9": [3, 3, 3]}
    )
    assert quantiles(controlled)["coverage_80"] == pytest.approx(2 / 3)
    assert quantiles(controlled)["pinball"]["0.5"] == pytest.approx(2 / 3)
    controlled.loc[0, "q0.1"] = 4
    with pytest.raises(ValueError):
        quantiles(controlled)
    assert metric([0.0, 0.0], [1.0, 1.0])["wape"] is None


def test_bundle_corruption_and_search_drift_fail_before_fit(tmp_path: Path) -> None:
    file = tmp_path / "payload.parquet"
    file.write_bytes(b"original")
    plan = {
        "checksums": {file.name: checksum(file)},
        "configurations": models.configurations(),
        "environment": study.environment(),
    }
    plan["id"] = identity(plan)
    write_json(tmp_path / "protocol.json", plan)
    assert study.check_bundle(tmp_path)["id"] == plan["id"]
    file.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        study.check_bundle(tmp_path)


@pytest.mark.parametrize("failure", ["timeout", "exit", "budget"])
def test_kaggle_failure_attempts_are_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    plan = {
        "id": "controlled",
        "configurations": {"seasonal_naive": {}},
        "splits": [{"fold": "T0"}],
        "budgets": {
            "cumulative_wall_seconds": 0 if failure == "budget" else 100,
            "worker_wall_seconds": 1,
        },
    }
    monkeypatch.setattr(study, "check_bundle", lambda _: plan)

    def fail(*args: Any, **kwargs: Any) -> Any:
        if failure == "timeout":
            raise subprocess.TimeoutExpired("controlled", 1)
        return subprocess.CompletedProcess("controlled", 2)

    monkeypatch.setattr(subprocess, "run", fail)
    output = tmp_path / "result"
    study.run(tmp_path, output)
    trials = read_json(output / "result.json")["trials"]
    assert len(trials) == 2
    assert {t["status"] for t in trials} == {
        "not_run"
        if failure == "budget"
        else "failed"
        if failure == "exit"
        else "timeout"
    }
    assert len(list(output.glob("*/trial.json"))) == 2
    with pytest.raises(ValueError, match="immutable"):
        study.run(tmp_path, output)


def test_kaggle_image_python_does_not_select_fit_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = runpy.run_path("research/kaggle/kernel.py")["prepare_runtime"]
    calls = []

    def capture(command: list[str], **kwargs: Any) -> Any:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="controlled freeze\n")

    monkeypatch.setattr(subprocess, "run", capture)
    python = runtime(tmp_path / "requirements.txt", "3.12.13", tmp_path)
    assert python == tmp_path / "venv/bin/python"
    probe = next(c for c, _ in calls if c[0] == str(python))
    assert "platform.python_version()=='3.12.13'" in probe[-1]
    assert all("--system-site-packages" not in c for c, _ in calls)


def test_kaggle_rejects_unsupported_python_before_install(tmp_path: Path) -> None:
    runtime = runpy.run_path("research/kaggle/kernel.py")["prepare_runtime"]
    with pytest.raises(ValueError, match="Unsupported project Python"):
        runtime(tmp_path / "requirements.txt", "3.13.0", tmp_path)
