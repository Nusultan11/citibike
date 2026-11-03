"""Database-backed local MLflow lineage; candidate logging cannot promote models."""

import importlib
import importlib.metadata
import math
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import FileLock

from citibike.features.contracts import SCHEMA, TYPES, code_hash, utc
from citibike.forecasting.artifacts import checked
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json


def uri(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    return "sqlite:///" + (root.resolve() / "metadata.sqlite").as_posix()


def client(root: Path) -> Any:
    return importlib.import_module("mlflow").MlflowClient(
        tracking_uri=uri(root), registry_uri=uri(root)
    )


def _versions(api: Any, name: str) -> list[Any]:
    result = []
    token = None
    while True:
        page = api.search_model_versions(
            f"name='{name}'", max_results=100, page_token=token
        )
        result.extend(page)
        token = page.token
        if not token:
            return result


def _name(name: str) -> None:
    if not name or len(name) > 100 or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError("unsafe_registry_name")


def register(
    root: Path, bundle_path: Path, name: str = "bikeflow-f4"
) -> dict[str, Any]:
    _name(name)
    bundle = checked(bundle_path)
    if bundle.get("kind") != "model_bundle" or bundle.get("feature_schema") != SCHEMA:
        raise ValueError("invalid_registry_model_bundle")
    data_path = Path(bundle["dataset_path"])
    data = checked(data_path)
    if (
        data["id"] != bundle["dataset_id"]
        or data["feature_schema"] != bundle["feature_schema"]
    ):
        raise ValueError("registry_dataset_lineage_mismatch")
    artifact_sha = checksum(bundle_path / "manifest.json")
    lineage = {
        "bundle": bundle,
        "dataset": data,
        "bundle_manifest_sha256": artifact_sha,
        "dataset_manifest_sha256": checksum(data_path / "manifest.json"),
        "dataset_checksum": data["checksums"]["examples.parquet"],
        "f7_code_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "f7_recipe_sha256": code_hash(Path(__file__).parent),
        "f7_working_tree_dirty": subprocess.run(
            ["git", "diff", "--quiet"], check=False
        ).returncode
        != 0,
        "automation_environment": {
            p: importlib.metadata.version(p)
            for p in ("prefect", "mlflow", "fastapi", "sqlalchemy")
        },
    }
    metrics: dict[str, float] = {}
    # Existing immutable selection/validation manifests are optional for externally
    # supplied bundles; absence is recorded, never replaced with synthetic metrics.
    selection_path = bundle_path.parents[1] / "selections" / bundle["selection_id"]
    if selection_path.exists():
        selection = checked(selection_path)
        experiment = checked(Path(selection["experiment_path"]))
        if (
            selection["dataset_id"] != data["id"]
            or experiment["dataset_id"] != data["id"]
        ):
            raise ValueError("registry_experiment_lineage_mismatch")
        lineage.update(selection=selection, experiment=experiment)
        for index, row in enumerate(experiment["results"]):
            if row["model_name"] == "seasonal_naive":
                for key in ("mae", "wape", "bias", "rmse"):
                    value = row["metrics"]["all"].get(key)
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        metrics[
                            f"validation.{index}.{row['prediction_type']}.{key}"
                        ] = float(value)
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(str(root / "registry.lock"), timeout=30):
        api = client(root)
        for version in _versions(api, name):
            if version.tags.get("bundle_manifest_sha256") == artifact_sha:
                run = api.get_run(version.run_id)
                if run.info.status != "FINISHED":
                    raise ValueError("incomplete_registry_run")
                return {
                    "name": name,
                    "version": version.version,
                    "run_id": version.run_id,
                    "experiment_id": run.info.experiment_id,
                    "artifact_sha256": artifact_sha,
                    "status": "unchanged",
                }
        if not api.search_registered_models(filter_string=f"name='{name}'"):
            api.create_registered_model(
                name,
                tags={"purpose": "F4 immutable candidates; no automatic promotion"},
            )
        experiment = api.get_experiment_by_name("bikeflow-f7")
        experiment_id = (
            experiment.experiment_id
            if experiment
            else api.create_experiment(
                "bikeflow-f7", artifact_location=(root.resolve() / "artifacts").as_uri()
            )
        )
        run = api.create_run(
            experiment_id,
            tags={
                "bundle_manifest_sha256": artifact_sha,
                "dataset_checksum": lineage["dataset_checksum"],
                "feature_schema": bundle["feature_schema"],
                "bundle_id": bundle["id"],
                "source_snapshot_id": bundle["source_snapshot_id"],
                "code_sha": lineage["f7_code_sha"],
                "accuracy_mode": bundle["historical_accuracy_mode"],
                "promotion": "not_approved",
            },
        )
        run_id = run.info.run_id
        try:
            api.log_param(run_id, "feature_schema", bundle["feature_schema"])
            for kind in TYPES:
                api.log_param(run_id, f"model.{kind}", bundle["models"][kind]["name"])
            for key, value in metrics.items():
                api.log_metric(run_id, key, value)
            api.log_artifacts(run_id, str(bundle_path.resolve()), "bundle")
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                report_path = Path(temporary) / "lineage.json"
                write_json(report_path, lineage)
                api.log_artifact(run_id, str(report_path))
            api.set_terminated(run_id, "FINISHED")
            version = api.create_model_version(
                name,
                f"runs:/{run_id}/bundle",
                run_id=run_id,
                tags={
                    "bundle_manifest_sha256": artifact_sha,
                    "feature_schema": bundle["feature_schema"],
                    "approval": "pending",
                },
            )
        except Exception:
            api.set_terminated(run_id, "FAILED")
            raise
        return {
            "name": name,
            "version": version.version,
            "run_id": run_id,
            "experiment_id": experiment_id,
            "artifact_sha256": artifact_sha,
            "status": "ok",
        }


def lineage(root: Path, name: str, version: str) -> dict[str, Any]:
    _name(name)
    api = client(root)
    model = api.get_model_version(name, version)
    run = api.get_run(model.run_id)
    with tempfile.TemporaryDirectory(dir=root) as temporary:
        saved = Path(api.download_artifacts(model.run_id, "lineage.json", temporary))
        result = read_json(saved)
        bundle_path = Path(api.download_artifacts(model.run_id, "bundle", temporary))
        bundle = checked(bundle_path)
        if (
            checksum(bundle_path / "manifest.json")
            != model.tags["bundle_manifest_sha256"]
            or bundle != result["bundle"]
        ):
            raise ValueError("registry_artifact_checksum_mismatch")
    return {
        "name": name,
        "version": version,
        "run_id": model.run_id,
        "experiment_id": run.info.experiment_id,
        "tags": model.tags,
        "params": run.data.params,
        "metrics": run.data.metrics,
        "lineage": result,
    }


def transition(
    root: Path,
    name: str,
    version: str,
    evidence: Path,
    approval: Path,
    *,
    rollback: bool = False,
) -> dict[str, Any]:
    """Explicit operator-only alias transition. Never called by flows/schedules.

    Evidence/approval are separately authored immutable review records bound to
    candidate content. Controlled lifecycle exercises use an isolated registry
    and f7-test-* namespace. No F4/F5 or serving pointer is changed here.
    """
    _name(name)
    review, owner = read_json(evidence), read_json(approval)
    with FileLock(str(root / "registry.lock"), timeout=30):
        api = client(root)
        candidate = api.get_model_version(name, version)
        fingerprint = candidate.tags["bundle_manifest_sha256"]
        required = (
            "strict_pit",
            "train_serve_parity",
            "independent_forward_shadow",
            "frozen_validation",
            "resources",
            "rollback_verified",
        )
        valid = (
            review.get("candidate_sha256") == fingerprint
            and review.get("feature_schema") == SCHEMA
            and all(review.get(k) is True for k in required)
            and owner.get("approved") is True
            and bool(owner.get("owner"))
            and owner.get("evidence_sha256") == checksum(evidence)
            and owner.get("candidate_sha256") == fingerprint
            and owner.get("action") == ("rollback" if rollback else "promote")
            and utc(review["forward_start"])
            < utc(review["forward_end"])
            <= utc(owner["approved_at"])
            <= datetime.now(UTC)
            and utc(review["training_cutoff"]) < utc(review["forward_start"])
            and (
                review.get("data_mode") == "real_official"
                or (
                    name.startswith("f7-test-")
                    and review.get("data_mode") == "controlled_fixture"
                )
            )
        )
        files = review.get("evidence_files", {})
        if (
            not valid
            or not files
            or any(checksum(Path(p)) != sha for p, sha in files.items())
        ):
            raise ValueError("promotion_gate_rejected")
        if rollback and candidate.tags.get("approval") != "approved":
            raise ValueError("rollback_target_not_previously_approved")
        # Verify the stored payload, not just user-supplied booleans, before change.
        lineage(root, name, version)
        api.set_model_version_tag(name, version, "approval", "approved")
        api.set_model_version_tag(name, version, "approval_sha256", checksum(approval))
        api.set_registered_model_alias(name, "champion", version)
        record = {
            "name": name,
            "version": version,
            "action": owner["action"],
            "approval_sha256": checksum(approval),
            "at": datetime.now(UTC).isoformat(),
        }
        write_json(root / "reviews" / f"{identity(record)}.json", record)
        return record
