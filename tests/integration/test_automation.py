"""F7 real Prefect/MLflow + disposable PostgreSQL; inputs explicitly controlled."""

import os
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from prefect import flow
from prefect.client.orchestration import get_client
from prefect.settings import PREFECT_API_URL, get_current_settings, temporary_settings

from apps.api.main import create_app
from citibike.automation import flows
from citibike.automation.config import Config
from citibike.automation.gates import GateRejected
from citibike.automation.registry import client, lineage, register, transition
from citibike.automation.worker import forecast, gbfs, pointer, risk, trips
from citibike.features.contracts import SCHEMA
from citibike.forecasting.artifacts import checked, create
from citibike.ingestion.archive import checksum
from citibike.serving.db import DatabaseSettings, connect
from citibike.storage import identity, read_json, write_json
from tests.fixtures_history import change_source, create_history
from tests.integration.test_risk import controlled_gbfs

pytest_plugins = ("tests.integration.test_serving",)


@pytest.fixture(scope="module")
def prefect_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    # A full local server: prefect_test_harness uses an ephemeral server whose
    # scheduler service is disabled, so it cannot prove actual schedule triggers.
    root = tmp_path_factory.mktemp("prefect-full-server")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/api"
    environment = {
        **os.environ,
        "PREFECT_HOME": str(root),
        "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
        "PREFECT_UI_ENABLED": "false",
        "PREFECT_SERVER_SERVICES_SCHEDULER_LOOP_SECONDS": "1",
    }
    with (root / "server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "prefect",
                "server",
                "start",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 90
            with httpx.Client(timeout=1) as api:
                while time.monotonic() < deadline:
                    try:
                        if api.get(url + "/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        pytest.fail(
                            (root / "server.log").read_text(errors="replace")[-3000:]
                        )
                    time.sleep(0.2)
                else:
                    pytest.fail("full Prefect server startup timeout")
            with temporary_settings(updates={PREFECT_API_URL: url}):
                yield
        finally:
            flows.terminate(process)


def active(database: DatabaseSettings, table: str) -> str | None:
    assert table in {"analytics_active", "ml_forecast_active", "risk_active"}
    with connect(database, readonly=True) as db:
        if db.execute("SELECT to_regclass(%s) present", (table,)).fetchone() == {
            "present": None
        }:
            return None
        row = db.execute(f"SELECT publication_id FROM {table}").fetchone()
        return row["publication_id"] if row else None


def test_source_processing_validation_serving_and_partial_recovery(
    database: DatabaseSettings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefect_server: None,
) -> None:
    inventory = create_history(tmp_path)
    listing = read_json(inventory)
    listing["window"] = listing["window"][-2:]
    listing["objects"] = listing["objects"][-2:]
    write_json(inventory, listing)
    config = Config(data_dir=tmp_path, inventory=inventory, mode="controlled_fixture")
    config_path = tmp_path / "config.json"
    write_json(config_path, config.model_dump(mode="json"))
    result = flows.archive_update(str(config_path))
    publication = result["id"]
    assert result["canonical_rows"] == 2
    assert active(database, "analytics_active") == publication
    response = TestClient(create_app()).get(
        "/v2/analytics/kpi",
        params={"start_date": listing["window"][0] + "-01", "end_date": "2026-10-01"},
    )
    assert response.status_code == 200
    # Independent primary-source oracle: exactly one generated ride per month.
    assert response.json()["metrics"]["completed_published_rides"] == 2
    with monkeypatch.context() as scoped:

        def forbidden_prepare(*args: Any, **kwargs: Any) -> list[Path]:
            pytest.fail("unchanged inputs must not reprocess historical data")

        scoped.setattr(
            "citibike.processing.historical.prepare_inventory", forbidden_prepare
        )
        assert trips(config)["status"] == "unchanged"
    for path in ("f2/active.json", "f3/active.json"):
        assert not (tmp_path / path).exists()
    before = pointer(config, "trips").read_bytes()
    # Rejected schema must leave all published pointers unchanged.
    item = listing["objects"][0]
    receipt = read_json(tmp_path / "inventory/objects" / f"{identity(item)}.json")
    source_manifest = Path(receipt["bronze"]) / "manifest.json"
    original = source_manifest.read_bytes()
    changed = read_json(source_manifest)
    changed["csv_files"][0]["columns"].append("drift")
    write_json(source_manifest, changed)
    with pytest.raises(GateRejected, match="source_schema_drift"):
        flows.archive_update(str(config_path))
    assert pointer(config, "trips").read_bytes() == before
    assert active(database, "analytics_active") == publication
    source_manifest.write_bytes(original)
    # A new source prepares a new SQL candidate; failure in dependent geo must
    # not expose partially updated analytics on the map/product pages.
    change_source(tmp_path, listing["window"][-1])
    with monkeypatch.context() as scoped:

        def fail_geo(*args: Any, **kwargs: Any) -> str:
            raise RuntimeError("controlled_partial_failure")

        scoped.setattr("citibike.serving.product_publish.publish_geo", fail_geo)
        with pytest.raises(RuntimeError, match="controlled_partial_failure"):
            trips(config)
    assert active(database, "analytics_active") == publication
    assert pointer(config, "trips").read_bytes() == before
    recovered = trips(config)
    assert recovered["id"] != publication and recovered["canonical_rows"] == 3
    assert active(database, "analytics_active") == recovered["id"]
    previous = read_json(pointer(config, "trips"))
    old_geo = read_json(Path(previous["geo_path"]) / "manifest.json")
    snapshot = controlled_gbfs(tmp_path, ["001", "002"])
    with monkeypatch.context() as scoped:

        def forbidden_scan(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("GBFS refresh must reuse the immutable geo aggregate")

        scoped.setattr("citibike.analytics.station_geo.configured", forbidden_scan)
        refreshed = gbfs(config.model_copy(update={"gbfs_path": snapshot}))
    latest = read_json(pointer(config, "trips"))
    assert refreshed["analytics_publication_version"] == latest["id"]
    assert latest["id"] != previous["id"]
    with connect(database, readonly=True) as db:
        assert db.execute(
            "SELECT count(*) n FROM analytics_availability WHERE publication_id=%s",
            (latest["id"],),
        ).fetchone() == {"n": 2}
    assert (
        read_json(Path(latest["geo_path"]) / "manifest.json")["checksums"]
        == old_geo["checksums"]
    )
    assert active(database, "analytics_active") == latest["id"]


def test_forecast_repeat_fallback_risk_and_outage(
    database: DatabaseSettings,
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prefect_server: None,
) -> None:
    original_pointer = (
        forecast_workflow["root"] / "f4/active_models.json"
    ).read_bytes()
    config = Config(
        data_dir=tmp_path,
        model_bundle=None,
        fallback_bundle=forecast_workflow["model"],
        mode="controlled_fixture",
    )
    first = forecast(config)
    assert first["fallback_reason"] == "primary_model_unavailable"
    assert first["model"] == "seasonal_naive" and first["coverage"] > 0
    assert forecast(config)["status"] == "unchanged"
    assert active(database, "ml_forecast_active") == first["id"]
    before = pointer(config, "forecast").read_bytes()
    config_path = tmp_path / "forecast-config.json"
    write_json(config_path, config.model_dump(mode="json"))
    with connect(database, readonly=True) as db:
        assert db.execute(
            "SELECT count(*) n FROM ml_forecast_publications"
        ).fetchone() == {"n": 1}
    path = controlled_gbfs(tmp_path, checked(Path(first["path"]))["station_ids"])
    risk_config = tmp_path / "risk-config.json"
    write_json(
        risk_config, {"hours": 2, "repeats": 5, "scales": [1.0], "orders": ["uniform"]}
    )
    scenario_config = config.model_copy(
        update={"gbfs_path": path, "risk_config": risk_config}
    )
    scenario = risk(scenario_config)
    assert scenario["quality"] == "ok"
    assert risk(scenario_config)["status"] == "unchanged"
    with connect(database, readonly=True) as db:
        assert db.execute("SELECT count(*) n FROM risk_publications").fetchone() == {
            "n": 1
        }
    assert active(database, "risk_active") == scenario["id"]
    assert not (tmp_path / "f5/active_controlled.json").exists()
    # A stale snapshot and an unavailable DB retain the last valid publication.
    with monkeypatch.context() as scoped:
        scoped.setattr(
            "citibike.automation.gates.gbfs",
            lambda *a, **k: (_ for _ in ()).throw(GateRejected("gbfs_stale_input")),
        )
        with pytest.raises(GateRejected, match="gbfs_stale_input"):
            risk(scenario_config)
    assert active(database, "risk_active") == scenario["id"]
    with monkeypatch.context() as scoped:
        scoped.setenv("PGPORT", "1")
        from citibike.serving.db import DatabaseUnavailable

        with pytest.raises(DatabaseUnavailable):
            forecast(config)
        with pytest.raises(flows.TransientFailure):
            flows.batch_forecast(str(config_path))
        events = [
            read_json(p)
            for p in (tmp_path / "automation/monitoring/events").glob("*.json")
        ]
        assert {r["attempt"] for r in events if r["status"] == "unavailable"} == {
            1,
            2,
            3,
        }
    assert active(database, "ml_forecast_active") == first["id"]
    assert pointer(config, "forecast").read_bytes() == before
    assert flows.batch_forecast(str(config_path))["status"] == "unchanged"
    assert (
        read_json(tmp_path / "automation/monitoring/forecast.json")["recovery"]
        == "recovered"
    )
    assert (
        forecast_workflow["root"] / "f4/active_models.json"
    ).read_bytes() == original_pointer
    assert not (tmp_path / "f4/active_forecasts.json").exists()


def test_ml_failure_uses_only_verified_seasonal_bundle(
    database: DatabaseSettings,
    forecast_workflow: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from citibike.forecasting.inference import predict

    primary = tmp_path / "primary"
    shutil.copytree(forecast_workflow["model"], primary)
    calls = []

    def failing_primary(*args: Any, **kwargs: Any) -> Path:
        calls.append(args[1])
        if args[1] == primary:
            raise RuntimeError("controlled_model_engine_failure")
        return predict(*args, **kwargs)

    monkeypatch.setattr("citibike.forecasting.inference.predict", failing_primary)
    result = forecast(
        Config(
            data_dir=tmp_path,
            model_bundle=primary,
            fallback_bundle=forecast_workflow["model"],
            mode="controlled_fixture",
        )
    )
    assert result["fallback_reason"] == "primary_ml_failure"
    assert calls == [primary, forecast_workflow["model"]]
    assert active(database, "ml_forecast_active") == result["id"]


def test_registry_lineage_promotion_rejection_and_isolated_rollback(
    forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    registry = tmp_path / "registry"
    name = "f7-test-lifecycle"
    model = forecast_workflow["model"]
    original = (forecast_workflow["root"] / "f4/active_models.json").read_bytes()
    first = register(registry, model, name)
    assert register(registry, model, name)["version"] == first["version"]
    api = client(registry)
    assert api.get_registered_model(name).aliases == {}
    history = lineage(registry, name, first["version"])
    assert (
        history["lineage"]["dataset_checksum"]
        == checked(forecast_workflow["dataset"])["checksums"]["examples.parquet"]
    )
    assert history["metrics"]  # Existing immutable validation metrics, not new fits.
    assert history["lineage"]["bundle"] == checked(model)
    evidence, approval = tmp_path / "evidence.json", tmp_path / "approval.json"
    write_json(evidence, {"candidate_sha256": first["artifact_sha256"]})
    write_json(approval, {"approved": False})
    with pytest.raises(ValueError, match="promotion_gate_rejected"):
        transition(registry, name, first["version"], evidence, approval)
    assert api.get_registered_model(name).aliases == {}
    # Second controlled payload/version: copy trusted fixture bundle, no fit and
    # no mutation of any frozen experiment or operational model.
    manifest = checked(model)

    def variant(stage: Path) -> dict[str, Any]:
        for file in manifest["checksums"]:
            shutil.copy2(model / file, stage / file)
        return {**manifest, "controlled_registry_variant": True}

    second_path = create(
        tmp_path,
        "models",
        identity([manifest["id"], "f7-lifecycle-control"]),
        variant,
        activate=False,
    )
    second = register(registry, second_path, name)
    assert second["version"] != first["version"]

    def approve(candidate: dict[str, Any], action: str) -> None:
        now = datetime.now(UTC)
        review = {
            "candidate_sha256": candidate["artifact_sha256"],
            "feature_schema": SCHEMA,
            "data_mode": "controlled_fixture",
            "strict_pit": True,
            "train_serve_parity": True,
            "independent_forward_shadow": True,
            "frozen_validation": True,
            "resources": True,
            "rollback_verified": True,
            "training_cutoff": (now - timedelta(days=3)).isoformat(),
            "forward_start": (now - timedelta(days=2)).isoformat(),
            "forward_end": (now - timedelta(days=1)).isoformat(),
            "evidence_files": {
                str(model / "manifest.json"): checksum(model / "manifest.json")
            },
        }
        write_json(evidence, review)
        write_json(
            approval,
            {
                "approved": True,
                "owner": "CONTROLLED TEST OPERATOR; NOT REAL APPROVAL",
                "action": action,
                "approved_at": now.isoformat(),
                "candidate_sha256": candidate["artifact_sha256"],
                "evidence_sha256": checksum(evidence),
            },
        )
        transition(
            registry,
            name,
            candidate["version"],
            evidence,
            approval,
            rollback=action == "rollback",
        )

    approve(first, "promote")
    approve(second, "promote")
    assert api.get_model_version_by_alias(name, "champion").version == second["version"]
    approve(first, "rollback")
    assert api.get_model_version_by_alias(name, "champion").version == first["version"]
    assert (
        forecast_workflow["root"] / "f4/active_models.json"
    ).read_bytes() == original
    assert not (tmp_path / "f4/active_models.json").exists()


def test_prefect_actual_bounded_retries_and_recovery(
    prefect_server: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.json"
    write_json(config, Config(data_dir=tmp_path).model_dump(mode="json"))
    attempts = []

    def transient(config: Config, command: list[str]) -> dict[str, Any]:
        attempts.append(time.monotonic())
        result = Path(command[-1])
        write_json(
            result,
            {
                "status": "failed" if len(attempts) < 3 else "ok",
                "reason": "controlled_source_unavailable",
            },
        )
        return {"returncode": 75 if len(attempts) < 3 else 0}

    monkeypatch.setattr(flows, "bounded", transient)

    @flow
    def probe() -> dict[str, Any]:
        return flows.operation(str(config), "review")

    assert probe()["status"] == "ok"
    assert len(attempts) == 3
    assert attempts[1] - attempts[0] >= 1 and attempts[2] - attempts[1] >= 2
    final = read_json(tmp_path / "automation/monitoring/review.json")
    assert final["attempt"] == 3 and final["recovery"] == "recovered"


def test_concurrent_writer_is_rejected_before_start(
    prefect_server: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from filelock import FileLock

    config = tmp_path / "config.json"
    write_json(config, Config(data_dir=tmp_path).model_dump(mode="json"))
    state = tmp_path / "automation"
    state.mkdir()

    def forbidden(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("concurrent worker must not start")

    monkeypatch.setattr(flows, "bounded", forbidden)

    @flow
    def probe() -> dict[str, Any]:
        return flows.operation.with_options(retries=0)(str(config), "review")

    with FileLock(str(state / "writer.lock")):
        with pytest.raises(flows.TransientFailure, match="concurrency_limit"):
            probe()
    assert read_json(state / "monitoring/review.json")["reason"] == "concurrency_limit"


def test_real_local_scheduler_trigger_and_repeated_run(
    prefect_server: None, tmp_path: Path
) -> None:
    config_path = tmp_path / "scheduled.json"
    write_json(config_path, Config(data_dir=tmp_path).model_dump(mode="json"))
    assert flows.retraining_review(str(config_path))["training_started"] is False
    environment = {
        **os.environ,
        "PREFECT_API_URL": str(get_current_settings().api.url),
        "PREFECT_RUNNER_POLL_FREQUENCY": "1",
        "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
    }
    logfile = tmp_path / "scheduler.log"
    completed = []
    with logfile.open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "citibike.automation",
                "serve",
                "--config",
                str(config_path),
                "--only",
                "review",
                "--test-interval",
                "2",
            ],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 150
            with get_client(sync_client=True) as api:
                while time.monotonic() < deadline:
                    runs = api.read_flow_runs(limit=100)
                    completed = [
                        r
                        for r in runs
                        if r.deployment_id is not None
                        and r.auto_scheduled
                        and r.state
                        and r.state.is_completed()
                        and r.parameters.get("config_path")
                        == str(config_path.resolve())
                    ]
                    if len(completed) >= 2:
                        break
                    if process.poll() is not None:
                        pytest.fail(
                            "scheduler exited: "
                            + logfile.read_text(errors="replace")[-3000:]
                        )
                    time.sleep(0.5)
            assert len(completed) >= 2, logfile.read_text(errors="replace")[-3000:]
            assert all(r.state and r.state.is_completed() for r in completed)
            assert not list((tmp_path / "automation/published").glob("*.json"))
            assert not list((tmp_path / "f4/models").glob("*"))
            write_json(
                tmp_path / "scheduler-proof.json",
                {
                    "mode": "controlled_scheduler",
                    "scheduled_run_ids": [str(r.id) for r in completed],
                    "automatic_training": False,
                    "publication_changes": 0,
                },
            )
        finally:
            flows.terminate(process)
