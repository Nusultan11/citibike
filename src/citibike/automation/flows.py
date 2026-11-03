"""Prefect dependencies, bounded retries, process budgets and local schedules."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import psutil  # type: ignore[import-untyped]
from filelock import FileLock, Timeout
from prefect import flow, get_run_logger, serve, task
from prefect.context import TaskRunContext, get_run_context
from prefect.deployments.runner import RunnerDeployment
from prefect.flows import Flow
from prefect.tasks import Task

from citibike.automation.config import Config, load
from citibike.automation.gates import GateRejected
from citibike.automation.monitoring import event
from citibike.storage import read_json


class TransientFailure(RuntimeError):
    """Only transient source/DB failures are retried."""


def retryable(task: Task[Any, Any], task_run: Any, state: Any) -> bool:
    try:
        state.result()
    except TransientFailure:
        return True
    except Exception:
        return False
    return False


def terminate(process: subprocess.Popen[bytes]) -> None:
    """Kill the entire isolated worker tree; no timed-out writer may publish later."""
    try:
        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for child in reversed(descendants):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        if process.poll() is None:
            parent.kill()
        psutil.wait_procs([parent, *descendants], timeout=5)
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=5)


def bounded(config: Config, command: list[str]) -> dict[str, Any]:
    """Hard wall/RSS limits around a process tree, unlike thread cancellation."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(config.state_dir).free < config.min_free_mb * 1024**2:
        raise GateRejected("disk_budget_exceeded")
    before = time.monotonic()
    environment = {
        **os.environ,
        "OMP_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
    }
    measured = 0
    process = subprocess.Popen(
        command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        parent = psutil.Process(process.pid)
        while process.poll() is None:
            if time.monotonic() - before > config.timeout_seconds:
                raise GateRejected("worker_timeout")
            try:
                processes = [parent, *parent.children(recursive=True)]
                rss = sum(p.memory_info().rss for p in processes if p.is_running())
                measured = max(measured, rss)
                if rss > config.max_rss_mb * 1024**2:
                    raise GateRejected("worker_rss_budget_exceeded")
            except psutil.NoSuchProcess:
                pass
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            terminate(process)
    return {
        "returncode": process.returncode,
        "elapsed_seconds": time.monotonic() - before,
        "peak_tree_rss_bytes": measured,
        "rss_sampling_seconds": 0.05,
    }


@task(
    retries=2,
    retry_delay_seconds=[1, 2],
    retry_condition_fn=retryable,
    persist_result=False,
)
def operation(config_path: str, name: str) -> dict[str, Any]:
    config = load(Path(config_path))
    root = config.data_dir
    context = get_run_context()
    assert isinstance(context, TaskRunContext)
    run_id = str(context.task_run.flow_run_id)
    attempt = context.task_run.run_count
    config.state_dir.mkdir(parents=True, exist_ok=True)
    result_path = config.state_dir / "attempts" / f"{uuid4().hex}.json"
    event(
        root,
        name,
        "running",
        flow_run_id=run_id,
        attempt=attempt,
        timeout_seconds=config.timeout_seconds,
        max_rss_mb=config.max_rss_mb,
    )
    try:
        # One writer for the shared local warehouse across ALL deployments/processes.
        # The lock is OS-backed, automatically released after a crashed scheduler.
        with FileLock(str(config.state_dir / "writer.lock"), timeout=1):
            metrics = bounded(
                config,
                [
                    sys.executable,
                    "-m",
                    "citibike.automation.worker",
                    name,
                    "--config",
                    config_path,
                    "--result",
                    str(result_path),
                ],
            )
        result = (
            read_json(result_path)
            if result_path.exists()
            else {"status": "failed", "reason": "worker_crashed_without_result"}
        )
        event(
            root,
            name,
            result["status"],
            flow_run_id=run_id,
            attempt=attempt,
            resources=metrics,
            result=result,
            **({"accuracy": result["accuracy"]} if "accuracy" in result else {}),
        )
        if metrics["returncode"] == 75:
            raise TransientFailure(result["reason"])
        if metrics["returncode"] != 0:
            raise GateRejected(result["reason"])
        get_run_logger().info("%s: %s", name, result["status"])
        return result
    except Timeout:
        event(
            root,
            name,
            "failed",
            flow_run_id=run_id,
            attempt=attempt,
            reason="concurrency_limit",
        )
        raise TransientFailure("concurrency_limit") from None
    except GateRejected as exc:
        event(
            root,
            name,
            "unavailable",
            flow_run_id=run_id,
            attempt=attempt,
            reason=str(exc),
            last_valid_retained=True,
        )
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        event(
            root,
            name,
            "failed",
            flow_run_id=run_id,
            attempt=attempt,
            reason="worker_runtime_failure",
            error_type=type(exc).__name__,
        )
        raise TransientFailure("worker_runtime_failure") from None


@flow(name="bikeflow-archive-update", persist_result=False)
def archive_update(config_path: str) -> dict[str, Any]:
    return operation(config_path, "trips")


@flow(name="bikeflow-gbfs-update", persist_result=False)
def gbfs_update(config_path: str) -> dict[str, Any]:
    result = operation(config_path, "gbfs")
    # Risk gate is executed after a validated snapshot. If forecast is absent,
    # the GBFS success is retained and the risk task is explicitly unavailable.
    try:
        result["risk"] = operation(config_path, "risk")
    except (GateRejected, TransientFailure) as exc:
        result["risk"] = {"status": "unavailable", "reason": str(exc)}
    return result


@flow(name="bikeflow-daily-forecast", persist_result=False)
def batch_forecast(config_path: str) -> dict[str, Any]:
    result = operation(config_path, "forecast")
    try:
        result["risk"] = operation(config_path, "risk")
    except (GateRejected, TransientFailure) as exc:
        result["risk"] = {"status": "unavailable", "reason": str(exc)}
    return result


@flow(name="bikeflow-risk-refresh", persist_result=False)
def risk_refresh(config_path: str) -> dict[str, Any]:
    return operation(config_path, "risk")


@flow(name="bikeflow-retraining-review", persist_result=False)
def retraining_review(config_path: str) -> dict[str, Any]:
    return operation(config_path, "review")


FLOWS: dict[str, Flow[..., Any]] = {
    "trips": archive_update,
    "gbfs": gbfs_update,
    "forecast": batch_forecast,
    "risk": risk_refresh,
    "review": retraining_review,
}


def scheduler(
    config_path: Path, *, test_interval: float | None = None, only: str | None = None
) -> None:
    """Explicit local Prefect runner. The API process never imports or starts it."""
    parsed = urlsplit(os.environ.get("PREFECT_API_URL", ""))
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("scheduler_requires_explicit_loopback_prefect_api")
    if test_interval is not None and not 1 <= test_interval <= 60:
        raise ValueError("test_interval_must_be_1_to_60_seconds")
    intervals = {"trips": 21600, "gbfs": 60, "risk": 300, "review": 604800}
    deployments = []
    for name, function in FLOWS.items():
        if only and only != name:
            continue
        deployment = function.to_deployment(
            name=f"local-{name}",
            parameters={"config_path": str(config_path.resolve())},
            concurrency_limit=1,
            interval=test_interval or intervals.get(name, 86400),
        )
        deployments.append(cast(RunnerDeployment, deployment))
    serve(*deployments, limit=1, pause_on_shutdown=True)
