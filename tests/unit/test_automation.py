"""Controlled F7 safety checks, independent of real source/accuracy evidence."""

import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from citibike.automation.config import Config
from citibike.automation.flows import bounded
from citibike.automation.gates import GateRejected, bronze, gbfs, model
from citibike.automation.monitoring import delayed_scores, event
from citibike.forecasting.artifacts import checked
from citibike.ingestion.archive import ingest_fixture
from citibike.ingestion.gbfs import collect
from citibike.storage import read_json, write_json
from tests.fixtures import ride, write_zip
from tests.integration.test_risk import controlled_gbfs
from tests.unit.test_gbfs import feeds


def test_config_budget_and_unknown_fields() -> None:
    for fields in (
        {"timeout_seconds": 0},
        {"max_changed_months": 25},
        {"password": "forbidden"},
    ):
        with pytest.raises(ValidationError):
            Config(**fields)


def test_source_schema_drift_before_processing(tmp_path: Path) -> None:
    archive = write_zip(tmp_path / "schema.zip", {"bad.csv": [ride(1)]})
    path = ingest_fixture(archive, "2026-04", tmp_path)
    report = read_json(path / "manifest.json")
    report["csv_files"][0]["columns"].append("unexpected_field")
    write_json(path / "manifest.json", report)
    with pytest.raises(GateRejected, match="source_schema_drift"):
        bronze(path)
    assert not (tmp_path / "f2/active.json").exists()


def test_gbfs_parity_freshness_and_no_activation(tmp_path: Path) -> None:
    discovery, information, status = feeds()
    replies = {
        "gbfs": discovery,
        "station_information": information,
        "station_status": status,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=replies[request.url.path.rsplit("/", 1)[-1].removesuffix(".json")]
        )

    # Reuse version-aware normalization through an explicit fixture transport.
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        path = collect(tmp_path, client=client, activate=False)
    assert not (tmp_path / "gbfs/active.json").exists()
    assert not (tmp_path / "gbfs/station_history/active.json").exists()
    result = gbfs(path, datetime.now(UTC))
    assert result["coverage"] > 0
    with pytest.raises(GateRejected, match="gbfs_stale_input"):
        gbfs(path, datetime.now(UTC) + timedelta(minutes=5))
    original = path.parents[2] / "bronze" / path.name / "station_status.json"
    original.write_bytes(b"{}")
    with pytest.raises(GateRejected, match="gbfs_bronze_checksum"):
        gbfs(path, datetime.now(UTC))


@pytest.mark.parametrize(
    "field,reason",
    [
        ("feature_schema", "feature_schema_drift"),
        ("feature_recipe", "train_serve_parity"),
        ("created_at", "pit_unavailable_at_origin"),
    ],
)
def test_model_gates_fail_closed(
    forecast_workflow: dict[str, Path], tmp_path: Path, field: str, reason: str
) -> None:
    source = forecast_workflow["model"]
    path = tmp_path / "candidate"
    shutil.copytree(source, path)
    manifest = checked(path)
    manifest[field] = (
        (datetime.now(UTC) + timedelta(days=1)).isoformat()
        if field == "created_at"
        else "changed"
    )
    write_json(path / "manifest.json", manifest)
    with pytest.raises(GateRejected, match=reason):
        model(path, datetime.now(UTC))


def test_monitoring_alert_recovery_and_webhook_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIKEFLOW_ALERT_WEBHOOK", "http://invalid-local-config/secret")
    failed = event(
        tmp_path, "forecast", "unavailable", reason="source_event_window_stale"
    )
    assert failed["alert"] and failed["webhook"] == "invalid_configuration"
    assert failed["accuracy"]["mae"] is None
    event(tmp_path, "forecast", "running")
    recovered = event(tmp_path, "forecast", "ok", publication_version="controlled-id")
    assert recovered["recovery"] == "recovered" and recovered["alert"]
    unchanged = event(tmp_path, "forecast", "unchanged")
    assert not unchanged["alert"]
    assert "secret" not in str(
        list((tmp_path / "automation/monitoring").rglob("*.json"))
    )
    for p in (tmp_path / "automation/monitoring").rglob("*.json"):
        assert "invalid-local-config" not in p.read_text()


def test_late_accuracy_only_observed_and_available() -> None:
    now = datetime.now(UTC)
    prediction = {
        "station_id": "001",
        "prediction_type": "arrivals",
        "target_hour_utc": (now - timedelta(days=1)).isoformat(),
        "predicted_value": 4,
        "status": "ok",
    }
    actual = {
        **prediction,
        "actual": 2,
        "available_at": (now + timedelta(days=1)).isoformat(),
        "label_status": "observed_nonzero",
    }
    assert delayed_scores([prediction], [actual], now)["status"] == "pending_actuals"
    actual["available_at"] = now.isoformat()
    result = delayed_scores([prediction], [actual], now)
    assert result == {
        "status": "observed",
        "rows": 1,
        "mae": 2.0,
        "wape": 1.0,
        "bias": 2.0,
    }
    half_hour = {**actual, "target_hour_utc": (now - timedelta(minutes=30)).isoformat()}
    assert (
        delayed_scores(
            [{**prediction, "target_hour_utc": half_hour["target_hour_utc"]}],
            [half_hour],
            now,
        )["status"]
        == "pending_actuals"
    )
    actual.update(actual=0, label_status="missing_observation")
    assert delayed_scores([prediction], [actual], now)["mae"] is None
    with pytest.raises(ValueError, match="duplicate_actual_grain"):
        delayed_scores([prediction], [actual, actual], now)


def test_timeout_kills_child_tree_before_late_publication(tmp_path: Path) -> None:
    output = tmp_path / "late.json"
    # The child would publish after the timeout if thread cancellation left it alive.
    child_code = (
        "import time;from pathlib import Path;time.sleep(2);"
        f"Path({str(output)!r}).write_text('late')"
    )
    parent_code = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}]);time.sleep(10)"
    )
    config = Config(data_dir=tmp_path, timeout_seconds=0.5, min_free_mb=0)
    with pytest.raises(GateRejected, match="worker_timeout"):
        bounded(config, [sys.executable, "-c", parent_code])
    time.sleep(2)
    assert not output.exists()


def test_rss_limit_and_disk_limit(tmp_path: Path) -> None:
    with pytest.raises(GateRejected, match="worker_rss_budget_exceeded"):
        bounded(
            Config(data_dir=tmp_path, max_rss_mb=64, timeout_seconds=10, min_free_mb=0),
            [
                sys.executable,
                "-c",
                "import time;x=bytearray(128*1024**2);time.sleep(5)",
            ],
        )
    with pytest.raises(GateRejected, match="disk_budget_exceeded"):
        bounded(
            Config(data_dir=tmp_path, min_free_mb=10**15),
            [sys.executable, "-c", "pass"],
        )


def test_gbfs_ttl_suppresses_early_network_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from citibike.automation.worker import gbfs as collect_operation

    path = controlled_gbfs(tmp_path, ["001"])
    config = Config(data_dir=tmp_path, gbfs_path=path, mode="controlled_fixture")
    result = collect_operation(config)
    assert result["status"] == "ok"
    monkeypatch.setattr(
        "citibike.ingestion.gbfs.collect",
        lambda *a, **k: pytest.fail("must respect TTL"),
    )
    assert collect_operation(config)["status"] == "unchanged"
    assert read_json(tmp_path / "automation/published/gbfs.json")["id"] == path.name


def test_invalid_primary_never_hides_schema_drift(
    forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    from citibike.automation.worker import forecast

    path = tmp_path / "invalid-model"
    shutil.copytree(forecast_workflow["model"], path)
    manifest = checked(path)
    manifest["feature_schema"] = "drift"
    write_json(path / "manifest.json", manifest)
    with pytest.raises(GateRejected, match="feature_schema_drift"):
        forecast(
            Config(
                data_dir=tmp_path,
                model_bundle=path,
                fallback_bundle=forecast_workflow["model"],
            )
        )
    assert not (tmp_path / "automation/published/forecast.json").exists()
