import copy
import json
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from citibike.forecasting.artifacts import checked as f4_checked
from citibike.ingestion.gbfs import collect
from citibike.risk import Config
from citibike.risk.inputs import load
from citibike.risk.run import checked, execute
from citibike.risk.verify import verify
from citibike.serving.db import DatabaseSettings
from citibike.serving.forecast_publish import publish
from citibike.storage import write_json
from tests.unit.test_gbfs import feeds

pytest_plugins = ("tests.integration.test_serving",)


def controlled_gbfs(root: Path, station_ids: list[str]) -> Path:
    discovery, information, status = feeds()
    info_template = information["data"]["stations"][0]
    status_template = status["data"]["stations"][0]
    information["data"]["stations"] = []
    status["data"]["stations"] = []
    for n, sid in enumerate(station_ids):
        info = copy.deepcopy(info_template)
        state = copy.deepcopy(status_template)
        info.update(
            station_id=f"controlled-{sid}", short_name=sid, lat=40.7 + n * 0.001
        )
        bikes = 8 if n % 2 == 0 else 1
        state.update(
            station_id=info["station_id"],
            num_bikes_available=bikes,
            num_docks_available=10 - bikes,
        )
        information["data"]["stations"].append(info)
        status["data"]["stations"].append(state)
    replies = {
        "gbfs": discovery,
        "station_information": information,
        "station_status": status,
    }

    def respond(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1].removesuffix(".json")
        return httpx.Response(200, json=replies[name])

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        return collect(root, client=client)


def test_published_forecast_gbfs_to_risk_cli_and_stale_gate(
    database: DatabaseSettings, forecast_workflow: dict[str, Path], tmp_path: Path
) -> None:
    forecast = forecast_workflow["forecast"]
    pointer_before = (
        forecast_workflow["root"] / "f4/active_forecasts.json"
    ).read_bytes()
    publication = publish(forecast, database)
    gbfs = controlled_gbfs(tmp_path, f4_checked(forecast)["station_ids"])
    as_of = datetime.now(UTC)
    config = Config(
        hours=1, repeats=2, scales=(0.0, 1.0), orders=("uniform", "arrivals_first")
    )
    scenario, gate = load(forecast, gbfs, as_of, config, database)
    assert scenario is not None and gate["coverage"]["eligible"] == 4
    path = execute(tmp_path, scenario, gate, config)
    assert checked(path)["data_mode"] == "controlled_fixture"
    oracle = verify(path)
    assert oracle["outcomes"] == 24
    config_file = tmp_path / "config.json"
    write_json(config_file, asdict(config))
    command = [
        sys.executable,
        "-m",
        "citibike.risk",
        "run",
        "--data-dir",
        str(tmp_path),
        "--forecast",
        str(forecast),
        "--gbfs",
        str(gbfs),
        "--as-of",
        as_of.isoformat(),
        "--config",
        str(config_file),
    ]
    environment = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[key] = "2"
    completed = subprocess.run(
        command, check=True, capture_output=True, text=True, timeout=60, env=environment
    )
    assert json.loads(completed.stdout.splitlines()[-1])["id"] == path.name
    verified = subprocess.run(
        [sys.executable, "-m", "citibike.risk", "verify", "--result", str(path)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    assert json.loads(verified.stdout)["status"] == "PASS"
    stale, reason = load(forecast, gbfs, as_of + timedelta(minutes=2), config, database)
    assert stale is None and any("stale_gbfs" in r for r in reason["reasons"])
    assert not (tmp_path / "f5/active.json").exists()
    active = (tmp_path / "f5/active_controlled.json").read_bytes()
    stale_path = execute(tmp_path, None, reason, config)
    assert verify(stale_path)["source_audit"]["computed_feed_rejections"]
    assert (tmp_path / "f5/active_controlled.json").read_bytes() == active
    assert (
        forecast_workflow["root"] / "f4/active_forecasts.json"
    ).read_bytes() == pointer_before
    from citibike.serving.forecast_api import ForecastFilters, read

    current = read(ForecastFilters(), database).publication
    assert current is not None and current["publication_id"] == publication
    bronze = gbfs.parents[2] / "bronze" / gbfs.name / "station_status.json"
    bronze.write_bytes(b"{}")
    invalid, rejection = load(forecast, gbfs, as_of, config, database)
    assert invalid is None and "gbfs_bronze_checksum_mismatch" in str(
        rejection["reasons"]
    )


def main() -> None:
    """Repeat the measured controlled experiment; never use the real serving DB."""
    import argparse
    import time
    from uuid import uuid4

    from psycopg import sql

    from citibike.features.dataset import build
    from citibike.forecasting.experiment import backtest, select
    from citibike.forecasting.inference import predict, train
    from citibike.serving.db import connect
    from tests.fixtures_forecasting import gold_fixture

    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument(
        "--output", type=Path, default=Path(".cache/f5-controlled-run.json")
    )
    args = parser.parse_args()
    root = args.data_dir / "f5/controlled-inputs"
    dataset = build(root, gold_fixture(root), station_count=4, smoke=True)
    model = train(root, select(root, backtest(root, dataset)))
    forecast = predict(root, model)
    admin = DatabaseSettings()
    name = "bikeflow_test_" + uuid4().hex
    with connect(admin) as db:
        db.autocommit = True
        db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    old_database = os.environ.get("PGDATABASE")
    try:
        os.environ["PGDATABASE"] = name
        publication = publish(forecast)
        gbfs = controlled_gbfs(root, f4_checked(forecast)["station_ids"])
        as_of = datetime.now(UTC).isoformat()
        config_path = args.data_dir / "f5/controlled-config.json"
        write_json(config_path, asdict(Config()))
        command = [
            sys.executable,
            "-m",
            "citibike.risk",
            "run",
            "--data-dir",
            str(args.data_dir),
            "--forecast",
            str(forecast),
            "--gbfs",
            str(gbfs),
            "--as-of",
            as_of,
            "--config",
            str(config_path),
        ]
        tick = time.perf_counter()
        first = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=300
        )
        elapsed = time.perf_counter() - tick
        result = json.loads(first.stdout.splitlines()[-1])
        tick = time.perf_counter()
        repeat = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=300
        )
        repeated_seconds = time.perf_counter() - tick
        if json.loads(repeat.stdout.splitlines()[-1])["id"] != result["id"]:
            raise ValueError("Controlled CLI repeat changed immutable identity")
        oracle = verify(
            Path(result["path"]), args.output.with_name("f5-controlled-oracle.json")
        )
        metadata = {
            "data_mode": "controlled_fixture",
            "command": subprocess.list2cmdline(command),
            "reproduce": "uv run --locked --group ml python -m "
            "tests.integration.test_risk",
            "publication": publication,
            "as_of": as_of,
            "forecast": str(forecast),
            "gbfs": str(gbfs),
            "config": str(config_path),
            "result": result,
            "full_cli_seconds": elapsed,
            "repeat_cli_seconds": repeated_seconds,
            "repeat_exact_id": True,
            "oracle": oracle,
        }
        write_json(args.output, metadata)
        print(
            json.dumps(
                {
                    "path": result["path"],
                    "status": result["status"],
                    "oracle": oracle["status"],
                    "metadata": str(args.output),
                }
            )
        )
    finally:
        if old_database is None:
            os.environ.pop("PGDATABASE", None)
        else:
            os.environ["PGDATABASE"] = old_database
        with connect(admin) as db:
            db.autocommit = True
            db.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


if __name__ == "__main__":
    main()
