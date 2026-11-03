"""One bounded subprocess operation. Heavy work reuses existing F1–F6 functions."""

import argparse
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg

from citibike.automation import gates
from citibike.automation.config import Config, load
from citibike.features.contracts import utc
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json

OPERATIONS = ("trips", "gbfs", "forecast", "risk", "review")


def pointer(config: Config, operation: str) -> Path:
    return config.state_dir / "published" / f"{operation}.json"


def saved(config: Config, operation: str) -> dict[str, Any]:
    path = pointer(config, operation)
    return read_json(path) if path.exists() else {}


def commit(config: Config, operation: str, report: dict[str, Any]) -> dict[str, Any]:
    before = saved(config, operation)
    status = "unchanged" if before.get("id") == report["id"] else "ok"
    if status != "unchanged":
        write_json(
            pointer(config, operation),
            {**report, "created_at": datetime.now(UTC).isoformat()},
        )
    return {**report, "status": status}


def publish_analytics(
    config: Config,
    gold: Path,
    previous: dict[str, Any],
    *,
    fresh_gbfs: Path | None = None,
) -> dict[str, Any]:
    from citibike.analytics.station_geo import build
    from citibike.serving.analytics_publish import activate, publish
    from citibike.serving.db import connect
    from citibike.serving.product_publish import publish_geo

    geo = build(
        config.data_dir,
        gold,
        reuse_from=Path(previous["geo_path"]) if previous.get("geo_path") else None,
    )
    publication = publish(gold, activate_publication=False)
    geo_id = publish_geo(geo)
    with connect() as db:
        db.execute("SELECT pg_advisory_xact_lock(7061643)")
        if fresh_gbfs:
            gates.gbfs(fresh_gbfs, datetime.now(UTC))
        activate(db, publication)
    return {
        "id": publication,
        "publication_version": publication,
        "path": str(gold),
        "geo_id": geo_id,
        "geo_path": str(geo),
    }


def trips(config: Config) -> dict[str, Any]:
    from citibike.analytics.build import build
    from citibike.ingestion.inventory import discover, ingest_window
    from citibike.processing.historical import prepare_inventory
    from citibike.processing.history_publish import publish_history
    from citibike.serving.analytics_publish import validate_gold

    root = config.data_dir
    inventory = config.inventory or discover(root)
    report = read_json(inventory)
    gates.require(bool(report["window"]), "empty_inventory")
    gates.require(
        config.mode == "controlled_fixture" or len(report["window"]) == 24,
        "incomplete_history_window",
    )
    selected = [
        o
        for o in report["objects"]
        if o["scope"] == "NYC"
        and o["period"] in report["window"]
        and o["packaging"] == "monthly_zip"
    ]
    gates.require(
        sorted(o["period"] for o in selected) == sorted(report["window"]),
        "ambiguous_inventory",
    )
    # Discovery receipts contain changing checked_at fields; match actual source
    # version, not listing identity, to avoid re-downloading 24 months each poll.
    receipts = [read_json(p) for p in (root / "inventory/objects").glob("*.json")]
    pending = []
    for item in selected:
        receipt_path = root / "inventory/objects" / f"{identity(item)}.json"
        matching = [
            r
            for r in receipts
            if r.get("availability") == "verified"
            and all(r.get(k) == item.get(k) for k in ("key", "url", "etag", "bytes"))
        ]
        if receipt_path.exists():
            exact = read_json(receipt_path)
            if exact.get("availability") == "verified":
                matching = [exact]
        if matching:
            match = matching[-1]
            gates.bronze(Path(match["bronze"]))
            if not receipt_path.exists():
                write_json(
                    receipt_path,
                    {
                        **match,
                        **item,
                        "availability": "verified",
                        "bronze": match["bronze"],
                        "source_version": match["source_version"],
                    },
                )
        else:
            pending.append(item)
    gates.require(
        len(pending) <= config.max_changed_months, "archive_change_budget_exceeded"
    )
    gates.require(
        sum(o["bytes"] for o in pending) <= config.max_download_bytes,
        "archive_byte_budget_exceeded",
    )
    for item in pending:
        gates.require(
            config.mode != "controlled_fixture", "controlled_network_ingest_forbidden"
        )
        receipt = read_json(ingest_window(inventory, root, period=item["period"]))
        if not any(
            o["period"] == item["period"] and o["availability"] == "verified"
            for o in receipt["objects"]
        ):
            raise ConnectionError("archive_source_unavailable")
    for item in selected:
        receipt = read_json(root / "inventory/objects" / f"{identity(item)}.json")
        gates.bronze(Path(receipt["bronze"]))
    sources = [
        [
            item["period"],
            read_json(root / "inventory/objects" / f"{identity(item)}.json")[
                "source_version"
            ],
        ]
        for item in sorted(selected, key=lambda value: value["period"])
    ]
    fingerprint = identity(sources)
    previous = saved(config, "trips")
    if previous.get("inventory_fingerprint") == fingerprint:
        # Recheck immutable quality/SQL availability, without running historical
        # preparation, profiling or transforms again on every scheduled poll.
        gold = Path(previous["path"])
        quality = validate_gold(gold)
        gates.require(quality["data_mode"] == config.mode, "source_mode_mismatch")
        publish_analytics(config, gold, previous)
        return {**previous, "status": "unchanged", "changed_sources": 0}
    # Existing processing caches by checksum; changed partitions only are rebuilt.
    states = prepare_inventory(inventory, root)
    snapshot = publish_history(
        states,
        root,
        report["window"],
        required_months=len(report["window"]),
        activate=False,
    )
    gold = build(root, snapshot, activate_publication=False)
    quality = validate_gold(gold)
    gates.require(quality["data_mode"] == config.mode, "source_mode_mismatch")
    # Publication's transaction validates COPY counts before switching SQL active.
    published = publish_analytics(config, gold, previous)
    return commit(
        config,
        "trips",
        {
            **published,
            "snapshot": str(snapshot),
            "inventory_fingerprint": fingerprint,
            "mode": config.mode,
            "quality": quality["quality_status"],
            "canonical_rows": quality["accepted_silver_rows"],
            "changed_sources": len(pending),
            "coverage": len(selected) / len(report["window"]),
            "source_checked_at": report["checked_at"],
        },
    )


def gbfs(config: Config) -> dict[str, Any]:
    from citibike.ingestion.gbfs import collect

    previous = saved(config, "gbfs")
    if previous:
        last_feed = previous["feeds"]["station_status"]
        due = utc(last_feed["received_at_utc"]) + timedelta(
            seconds=max(1, last_feed["ttl"])
        )
        if datetime.now(UTC) < due:
            # Do not poll before TTL, but never label a stale cached feed fresh.
            checked = gates.gbfs(Path(previous["path"]), datetime.now(UTC))
            return {
                **previous,
                **checked,
                "status": "unchanged",
                "next_poll_at": due.isoformat(),
            }
    gates.require(
        config.mode == "real_official" or config.gbfs_path is not None,
        "controlled_gbfs_path_required",
    )
    path = config.gbfs_path or collect(config.data_dir, activate=False)
    checked = gates.gbfs(path, datetime.now(UTC))
    previous_trips = saved(config, "trips")
    if previous_trips:
        from citibike.analytics.build import refresh_availability

        gold = refresh_availability(config.data_dir, Path(previous_trips["path"]))
        # Refresh only availability; trip partitions and geo values are reused.
        published = publish_analytics(config, gold, previous_trips, fresh_gbfs=path)
        commit(config, "trips", {**previous_trips, **published})
        checked["analytics_publication_version"] = published["id"]
    return commit(
        config,
        "gbfs",
        {
            "id": checked["collection_id"],
            "path": str(path),
            "mode": config.mode,
            **checked,
        },
    )


def decision_origin(config: Config) -> datetime:
    if config.controlled_origin is not None:
        gates.require(
            config.mode == "controlled_fixture", "real_origin_override_forbidden"
        )
        return utc(config.controlled_origin)
    signature = identity(
        [str(config.model_bundle), str(config.fallback_bundle), config.stations]
    )
    now = datetime.now(UTC)
    path = config.state_dir / "decisions" / f"{now.date()}-{signature}.json"
    if not path.exists():
        write_json(path, {"origin": now.isoformat()})
    return utc(read_json(path)["origin"])


def forecast(config: Config) -> dict[str, Any]:
    from citibike.forecasting.artifacts import checked
    from citibike.forecasting.inference import predict
    from citibike.serving.forecast_publish import publish, validate

    origin = decision_origin(config)
    path = config.model_bundle
    gates.require(
        path is not None or config.fallback_bundle is not None,
        "model_bundle_unavailable",
    )
    fallback_reason = None
    if path is not None:
        try:
            gates.model(path, origin)
        except gates.GateRejected as exc:
            # Source/schema/PIT failures must never be hidden by a model fallback.
            if str(exc) != "operational_model_not_approved":
                raise
            fallback_reason = str(exc)
            path = config.fallback_bundle
    else:
        path = config.fallback_bundle
        fallback_reason = "primary_model_unavailable"
    gates.require(path is not None, "valid_seasonal_fallback_unavailable")
    assert path is not None
    bundle = gates.model(path, origin)
    gates.require(
        checked(Path(bundle["dataset_path"]))["data_mode"] == config.mode,
        "model_source_mode_mismatch",
    )
    try:
        output = predict(config.data_dir, path, origin, config.stations, activate=False)
    except (RuntimeError, ImportError) as exc:
        if config.fallback_bundle is None or config.fallback_bundle == path:
            raise gates.GateRejected("ml_failure_without_valid_fallback") from exc
        path = config.fallback_bundle
        bundle = gates.model(path, origin)
        gates.require(
            checked(Path(bundle["dataset_path"]))["data_mode"] == config.mode,
            "model_source_mode_mismatch",
        )
        output = predict(config.data_dir, path, origin, config.stations, activate=False)
        fallback_reason = "primary_ml_failure"
    manifest, _ = validate(output)
    gates.require(manifest["availability_mode"] == "strict", "forecast_not_strict")
    gates.require(manifest["status"] == "ok", "forecast_unavailable")
    publication = publish(output)
    return commit(
        config,
        "forecast",
        {
            "id": publication,
            "publication_version": publication,
            "path": str(output),
            "model_version": bundle["id"],
            "model": "seasonal_naive",
            "fallback_reason": fallback_reason,
            "quality": manifest["quality_status"],
            "coverage": manifest["valid_rows"] / manifest["rows"],
            "forecast_origin_utc": manifest["forecast_origin_utc"],
            "data_as_of": manifest["data_as_of"],
            "valid_until": manifest["valid_until"],
            "source_age_days": manifest["freshness"]["source_age_days"],
            "accuracy_evidence_mode": checked(output)["accuracy_evidence_mode"],
        },
    )


def risk(config: Config) -> dict[str, Any]:
    from citibike.risk import Config as RiskConfig
    from citibike.risk.inputs import load as load_inputs
    from citibike.risk.run import execute
    from citibike.serving.product_publish import publish_risk

    forecast_path = config.forecast_path or (
        Path(saved(config, "forecast")["path"]) if saved(config, "forecast") else None
    )
    gbfs_path = config.gbfs_path or (
        Path(saved(config, "gbfs")["path"]) if saved(config, "gbfs") else None
    )
    gates.require(
        forecast_path is not None and gbfs_path is not None, "risk_inputs_unavailable"
    )
    assert forecast_path is not None and gbfs_path is not None
    settings = (
        RiskConfig(**read_json(config.risk_config))
        if config.risk_config
        else RiskConfig()
    )
    now = datetime.now(UTC)
    gates.gbfs(gbfs_path, now)
    scenario, gate = load_inputs(
        forecast_path, gbfs_path, now, settings, requested=config.stations
    )
    gates.require(
        scenario is not None,
        "risk_input_gate_rejected:" + ";".join(gate.get("reasons", [])),
    )
    fingerprint = identity(
        [
            checksum(forecast_path / "manifest.json"),
            checksum(gbfs_path / "quality.json"),
            asdict(settings),
            config.stations,
            gate["start"],
        ]
    )
    previous = saved(config, "risk")
    if previous.get("input_fingerprint") == fingerprint:
        publish_risk(
            Path(previous["path"]),
            pre_activate=lambda: gates.gbfs(gbfs_path, datetime.now(UTC)),
        )
        return {**previous, "status": "unchanged"}
    result = execute(config.data_dir, scenario, gate, settings, activate=False)
    publication = publish_risk(
        result, pre_activate=lambda: gates.gbfs(gbfs_path, datetime.now(UTC))
    )
    return commit(
        config,
        "risk",
        {
            "id": publication,
            "input_fingerprint": fingerprint,
            "publication_version": publication,
            "path": str(result),
            "coverage": gate["coverage"],
            "quality": gate["status"],
            "mode": config.mode,
            "model_version": scenario.provenance["model_bundle_id"]
            if scenario
            else None,
        },
    )


def review(config: Config) -> dict[str, Any]:
    from citibike.automation.monitoring import delayed_scores
    from citibike.automation.registry import register

    record: dict[str, Any] = {
        "status": "review_required",
        "training_started": False,
        "promotion": "blocked_pending_forward_shadow_pit_parity_owner_approval",
        "actuals": "not_supplied",
        "automatic_training": False,
    }
    if config.model_bundle:
        record["candidate"] = register(
            config.state_dir / "registry", config.model_bundle
        )
    record["accuracy"] = {
        "status": "pending_actuals",
        "mae": None,
        "wape": None,
        "bias": None,
    }
    prediction = saved(config, "forecast")
    if config.actuals_dataset and prediction:
        import duckdb

        from citibike.forecasting.artifacts import checked
        from citibike.risk.inputs import parquet_rows

        data = checked(config.actuals_dataset)
        available = max(
            utc(data["created_at"]), utc(data["strict_source_available_at"])
        )
        now = datetime.now(UTC)
        if available <= now:
            with duckdb.connect() as db:
                rows = db.execute(
                    "SELECT station_id,target_hour_utc,prediction_type,min(target) "
                    "FROM read_parquet(?) WHERE eligibility='eligible' "
                    "AND target IS NOT NULL GROUP BY 1,2,3 "
                    "HAVING min(target)=max(target)",
                    [str(config.actuals_dataset / "examples.parquet")],
                ).fetchall()
            actuals = [
                {
                    "station_id": s,
                    "target_hour_utc": h,
                    "prediction_type": k,
                    "actual": a,
                    "available_at": available.isoformat(),
                    "label_status": "observed_zero" if a == 0 else "observed_nonzero",
                }
                for s, h, k, a in rows
            ]
            record["accuracy"] = delayed_scores(
                parquet_rows(Path(prediction["path"]) / "forecasts.parquet"),
                actuals,
                now,
            )
            record["actuals"] = {
                "dataset_id": data["id"],
                "manifest_sha256": checksum(config.actuals_dataset / "manifest.json"),
                "available_at": available.isoformat(),
            }
    return record


def run(config: Config, operation: str) -> dict[str, Any]:
    return {
        "trips": trips,
        "gbfs": gbfs,
        "forecast": forecast,
        "risk": risk,
        "review": review,
    }[operation](config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = run(load(args.config), args.operation)
        write_json(args.result, result)
    except (httpx.HTTPError, psycopg.Error, ConnectionError) as exc:
        write_json(
            args.result,
            {
                "status": "failed",
                "reason": "source_or_database_unavailable",
                "error_type": type(exc).__name__,
                "retryable": True,
            },
        )
        raise SystemExit(75) from None
    except Exception as exc:
        from citibike.serving.db import DatabaseUnavailable

        retryable = isinstance(exc, DatabaseUnavailable)
        # Only explicit gate codes can be emitted. Third-party exceptions may carry
        # DSNs, source tokens or local secrets; retain their type, never raw text.
        reason = (
            str(exc) if isinstance(exc, gates.GateRejected) else "operation_rejected"
        )
        write_json(
            args.result,
            {
                "status": "unavailable",
                "reason": reason,
                "error_type": type(exc).__name__,
                "retryable": retryable,
            },
        )
        raise SystemExit(75 if retryable else 2) from None


if __name__ == "__main__":
    main()
