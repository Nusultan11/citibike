"""F6 source→API→HTML checks.

--mode fixture: controlled known-answer chain into the configured (CI/disposable)
database: F3 fixture Gold, station geo, fresh F4 forecast, current F5 scenario.
--mode real: read-only independent checks of the published real data; Silver and
the immutable F5 artifact are re-read directly, never through serving helpers.
"""

import argparse
import json
import re
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx


def attribute(html: str, name: str) -> str | None:
    found = re.search(rf'{name}="([^"]*)"', html)
    return found[1] if found else None


def check_action_verdict(
    html: str, cells: dict[tuple[float, str], dict[str, float]]
) -> str:
    """Independent same-cell comparison: a table winner is not an action approval."""
    no_action_best = bool(cells) and all(
        values["no_action"] <= min(values.values()) + 1e-9 for values in cells.values()
    )
    expected = "no_action" if no_action_best else "compare_only"
    assert attribute(html, "data-scenario-action") == expected
    if no_action_best:
        assert "Перевозки не рекомендуются" in html
    return expected


def get(client: httpx.Client, url: str, **params: Any) -> dict[str, Any]:
    response = client.get(url, params=params)
    response.raise_for_status()
    value: dict[str, Any] = response.json()
    return value


def page(client: httpx.Client, url: str, **params: Any) -> str:
    response = client.get(url, params=params)
    assert response.status_code == 200, (url, response.status_code)
    return response.text


def silver_counts(
    files: list[str], start: str, end: str, stations: list[str]
) -> dict[str, Any]:
    """Independent DuckDB pass over raw Silver files, not Gold or serving."""
    with duckdb.connect() as db:
        db.execute("SET memory_limit='2GB'; SET threads=2")
        total = db.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE disposition='accepted'"
            " AND started_at_local>=?::TIMESTAMP AND started_at_local<?::TIMESTAMP",
            [files, start, end],
        ).fetchone()
        per: dict[str, dict[str, int]] = {}
        for station in stations:
            row = db.execute(
                """SELECT
                count(*) FILTER (WHERE start_station_id=?
                    AND started_at_local>=?::TIMESTAMP
                    AND started_at_local<?::TIMESTAMP),
                count(*) FILTER (WHERE end_station_id=?
                    AND ended_at_local>=?::TIMESTAMP
                    AND ended_at_local<?::TIMESTAMP)
                FROM read_parquet(?) WHERE disposition='accepted'
                AND (start_station_id=? OR end_station_id=?)""",
                [station, start, end, station, start, end, files, station, station],
            ).fetchone()
            assert row is not None
            per[station] = {"departures": row[0], "arrivals": row[1]}
        coords: dict[str, list[float] | None] = {}
        for station in stations:
            # Latest NY month before `end`; start side preferred, else end side.
            pair = db.execute(
                """SELECT lat, lon FROM (
                  SELECT 0 side, start_lat lat, start_lng lon FROM read_parquet(?)
                  WHERE disposition='accepted' AND start_station_id=?
                    AND strftime(started_at_local,'%Y-%m')=strftime(?::DATE - 1,'%Y-%m')
                  UNION ALL SELECT 1, end_lat, end_lng FROM read_parquet(?)
                  WHERE disposition='accepted' AND end_station_id=?
                    AND strftime(ended_at_local,'%Y-%m')=strftime(?::DATE - 1,'%Y-%m'))
                WHERE isfinite(lat) AND isfinite(lon)
                GROUP BY side, lat, lon
                ORDER BY side, count(*) DESC, lat, lon LIMIT 1""",
                [files, station, end, files, station, end],
            ).fetchone()
            coords[station] = list(pair) if pair else None
    assert total is not None
    return {"completed_published_rides": total[0], "stations": per, "coords": coords}


def latency(
    client: httpx.Client, url: str, samples: int, **params: Any
) -> dict[str, Any]:
    first = time.perf_counter()
    client.get(url, params=params).raise_for_status()
    cold = (time.perf_counter() - first) * 1000
    values = []
    for _ in range(samples):
        begin = time.perf_counter()
        client.get(url, params=params).raise_for_status()
        values.append((time.perf_counter() - begin) * 1000)
    values.sort()
    return {
        "first_ms": round(cold, 2),
        "p50_ms": round(statistics.median(values), 2),
        "p95_ms": round(values[min(len(values) - 1, int(0.95 * len(values)))], 2),
        "max_ms": round(values[-1], 2),
        "samples": samples,
    }


def fixture(api: str, web: str, data_dir: Path) -> dict[str, Any]:
    from citibike.analytics.station_geo import build as build_geo
    from citibike.features.dataset import build
    from citibike.forecasting.artifacts import checked as f4_checked
    from citibike.forecasting.experiment import backtest, final_test, select
    from citibike.forecasting.inference import predict, train
    from citibike.risk import Config
    from citibike.risk.inputs import load
    from citibike.risk.run import execute
    from citibike.serving.analytics_publish import publish as publish_analytics
    from citibike.serving.forecast_publish import publish as publish_forecast
    from citibike.serving.product_publish import publish_geo, publish_risk
    from tests.fixtures_analytics import analytical_gold
    from tests.fixtures_forecasting import gold_fixture
    from tests.integration.test_risk import controlled_gbfs

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        # Shared with analytics_smoke: immutable fixture partitions are reused.
        gold = analytical_gold(data_dir)
        analytics_id = publish_analytics(gold)
        geo_id = publish_geo(build_geo(data_dir, gold))
        silver = [
            str(p) for p in sorted((data_dir / "fixture_silver").glob("*/*.parquet"))
        ]
        f4 = root / "f4root"
        dataset = build(f4, gold_fixture(f4), station_count=4, smoke=True)
        selection = select(f4, backtest(f4, dataset))
        final_test(f4, selection)
        forecast = predict(f4, train(f4, selection))
        forecast_id = publish_forecast(forecast)
        stations = f4_checked(forecast)["station_ids"]
        gbfs = controlled_gbfs(root, stations)
        config = Config(hours=1, repeats=2, scales=(0.0, 1.0), orders=("uniform",))
        as_of = datetime.now(UTC)
        scenario, gate = load(forecast, gbfs, as_of, config)
        assert scenario is not None
        risk_id = publish_risk(execute(root, scenario, gate, config))
        period = {"start_date": "2024-10-01", "end_date": "2025-04-01"}
        oracle = silver_counts(silver, "2024-10-01", "2025-04-01", ["001", "002"])
        with httpx.Client(timeout=60) as client:
            risk_html = page(client, web + "/scenarios")
            risk = get(client, api + "/v4/risk")
            action_cells: dict[tuple[float, str], dict[str, float]] = {}
            for outcome in risk["outcomes"]:
                action_cells.setdefault((outcome["scale"], outcome["event_order"]), {})[
                    outcome["policy"]
                ] = outcome["totals"]["scenario_objective"]["mean"]
            check_action_verdict(risk_html, action_cells)
            assert attribute(risk_html, "data-scenario-state") == "current_scenario"
            assert attribute(risk_html, "data-scenario-id") == risk_id
            coverage = risk["publication"]["coverage"]
            assert attribute(risk_html, "data-scenario-coverage") == (
                f"{coverage['eligible']}/{coverage['requested']}"
            )
            assert attribute(risk_html, "data-scenario-outcomes") == str(
                len(risk["outcomes"])
            )
            body = get(client, api + "/v4/stations", **period)
            assert body["geo"]["geo_id"] == geo_id
            html = page(client, web + "/stations", **period)
            assert attribute(html, "data-stations-total") == str(body["total_stations"])
            assert attribute(html, "data-geo-id") == geo_id
            for station, truth in oracle["stations"].items():
                api_row = get(client, api + f"/v4/stations/{station}", **period)
                row = api_row["rows"][0]
                assert row["departures"] == truth["departures"], (station, row)
                assert row["arrivals"] == truth["arrivals"], (station, row)
                detail = page(client, web + "/stations", station_id=station, **period)
                assert attribute(detail, "data-station-departures") == str(
                    truth["departures"]
                )
                assert attribute(detail, "data-station-arrivals") == str(
                    truth["arrivals"]
                )
            sid = stations[0]
            values = get(client, api + "/v3/forecasts", station_id=sid, limit=48)
            forecast_html = page(client, web + "/forecasts", station_id=sid)
            assert attribute(forecast_html, "data-forecast-page") == "valid"
            assert attribute(forecast_html, "data-forecast-values") == str(
                len(values["rows"])
            )
            overview = page(client, web + "/", **period)
            kpi = get(client, api + "/v2/analytics/kpi", **period)
            assert attribute(overview, "data-overview-rides") == str(
                kpi["metrics"]["completed_published_rides"]
            )
            assert (
                kpi["metrics"]["completed_published_rides"]
                == oracle["completed_published_rides"]
            )
            assert attribute(overview, "data-forecast-state") == "valid"
            assert attribute(overview, "data-risk-queue") == "current_scenario"
            invalid = page(client, web + "/stations", start_date="2025-01-02")
            assert attribute(invalid, "data-stations-state") == "error"
    return {
        "mode": "controlled_fixture",
        "analytics_publication": analytics_id,
        "geo_id": geo_id,
        "forecast_publication": forecast_id,
        "risk_publication": risk_id,
        "independent_silver": oracle,
        "checks": "fixture Silver→API→HTML for overview/stations/forecasts/scenarios",
    }


def real(api: str, web: str, samples: int) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    with httpx.Client(timeout=60) as client:
        pub = get(client, api + "/v2/analytics/publication")["publication"]
        active = json.loads(Path("data/f3/active.json").read_text(encoding="utf-8"))
        assert active["publication_id"] == pub["publication_id"]
        manifest = json.loads(
            (Path(active["path"]) / "manifest.json").read_text(encoding="utf-8")
        )
        files = sorted(
            {
                str(Path(s["path"]) / "silver.parquet")
                for m in manifest["months"]
                for s in m["silver"]
            }
        )
        window = ("2026-09-03", "2026-10-01")
        baseline = ("2025-09-04", "2025-10-02")
        stations = ["6611.07", "5980.1", "5980.10", "3138.03"]
        period = {"start_date": window[0], "end_date": window[1]}
        oracle = silver_counts(files, *window, stations)
        base_oracle = silver_counts(files, *baseline, [])
        kpi = get(client, api + "/v2/analytics/kpi", **period)
        compare = get(
            client,
            api + "/v2/analytics/compare",
            **period,
            compare_start=baseline[0],
            compare_end=baseline[1],
        )
        overview = page(client, web + "/")
        rides = oracle["completed_published_rides"]
        change = (rides - base_oracle["completed_published_rides"]) / base_oracle[
            "completed_published_rides"
        ]
        checks["overview"] = {
            "silver": rides,
            "api": kpi["metrics"]["completed_published_rides"],
            "html": int(attribute(overview, "data-overview-rides") or -1),
            "baseline_silver": base_oracle["completed_published_rides"],
            "baseline_api": compare["comparison"]["previous_rides"],
            "change_independent": change,
            "change_api": compare["comparison"]["relative_change"],
            "change_html": float(attribute(overview, "data-overview-change") or "nan"),
            "publication_html": attribute(overview, "data-overview-publication"),
        }
        o = checks["overview"]
        assert o["silver"] == o["api"] == o["html"], o
        assert o["baseline_silver"] == o["baseline_api"], o
        assert abs(o["change_independent"] - o["change_api"]) < 1e-12
        assert abs(o["change_html"] - o["change_api"]) < 1e-12
        assert o["publication_html"] == pub["publication_id"]
        station_rows = {
            r["station_id"]: r
            for r in get(client, api + "/v4/stations", **period)["rows"]
        }
        checks["stations"] = {}
        for station in stations:
            truth = oracle["stations"][station]
            row = station_rows[station]
            f3 = get(
                client, api + "/v2/analytics/stations", **period, station_id=station
            )["rows"][0]["metrics"]
            html = page(client, web + "/stations", station_id=station, **period)
            result = {
                "silver": truth,
                "api_v4": {k: row[k] for k in ("departures", "arrivals")},
                "api_v2": {k: f3[k] for k in ("departures", "arrivals")},
                "html": {
                    "departures": int(attribute(html, "data-station-departures") or -1),
                    "arrivals": int(attribute(html, "data-station-arrivals") or -1),
                },
                "coordinate_silver": oracle["coords"][station],
                "coordinate_api": [row["lat"], row["lon"]],
                "split_partner": row["split_partner"],
                "split_html": attribute(html, "data-id-split-detail"),
            }
            assert (
                result["silver"]
                == result["api_v4"]
                == result["api_v2"]
                == result["html"]
            ), result
            assert result["coordinate_silver"] == result["coordinate_api"], result
            assert result["split_partner"] == result["split_html"], result
            checks["stations"][station] = result
        assert checks["stations"]["5980.1"]["split_partner"] == "5980.10"
        # DST: NY fall-back day has 25 UTC hours, spring-forward 23; never filled.
        checks["dst_hours"] = {
            day: len(
                get(
                    client,
                    api + "/v2/analytics/timeseries",
                    start_date=day,
                    end_date=end,
                    grain="hour",
                )["rows"]
            )
            for day, end in (("2025-11-02", "2025-11-03"), ("2026-03-08", "2026-03-09"))
        }
        assert checks["dst_hours"] == {"2025-11-02": 25, "2026-03-08": 23}
        forecast = get(client, api + "/v3/forecasts", limit=1)
        forecast_html = page(client, web + "/forecasts")
        expired = datetime.fromisoformat(forecast["publication"]["valid_until"]) < (
            datetime.now(UTC)
        )
        checks["forecast"] = {
            "api_status": forecast["status"],
            "valid_until": forecast["publication"]["valid_until"],
            "expired_now": expired,
            "html_state": attribute(forecast_html, "data-forecast-page"),
            "html_values_shown": "data-forecast-values" in forecast_html,
        }
        assert expired and checks["forecast"]["html_state"] == "expired"
        assert not checks["forecast"]["html_values_shown"]
        risk = get(client, api + "/v4/risk")
        result = Path("data/f5/results") / risk["publication"]["publication_id"]
        artifact = json.loads((result / "manifest.json").read_text(encoding="utf-8"))
        expected = {
            (float(c["scale"]), c["order"], p): v["totals"]["scenario_objective"][
                "mean"
            ]
            for c in artifact["comparisons"]
            for p, v in c["policies"].items()
        }
        served = {
            (o["scale"], o["event_order"], o["policy"]): o["totals"][
                "scenario_objective"
            ]["mean"]
            for o in risk["outcomes"]
        }
        scenario_html = page(client, web + "/scenarios")
        action_cells: dict[tuple[float, str], dict[str, float]] = {}
        for (scale, order, policy), objective in expected.items():
            action_cells.setdefault((scale, order), {})[policy] = objective
        action_verdict = check_action_verdict(scenario_html, action_cells)
        no_action_best = sum(
            1
            for scale, order, policy in expected
            if policy == "no_action"
            and expected[(scale, order, policy)]
            <= min(expected[(scale, order, p)] for p in ("heuristic", "optimizer"))
        )
        checks["scenario"] = {
            "artifact_cells": len(expected),
            "served_cells": len(served),
            "objective_mismatches": sum(
                abs(served[k] - v) > 1e-12 for k, v in expected.items()
            ),
            "status": risk["status"],
            "html_state": attribute(scenario_html, "data-scenario-state"),
            "html_coverage": attribute(scenario_html, "data-scenario-coverage"),
            "artifact_coverage": f"{artifact['gate']['coverage']['eligible']}/"
            f"{artifact['gate']['coverage']['requested']}",
            "no_action_best_cells": no_action_best,
            "as_of": artifact["data_as_of"],
            "action_verdict": action_verdict,
        }
        s = checks["scenario"]
        assert s["artifact_cells"] == s["served_cells"] == 27
        assert s["objective_mismatches"] == 0
        assert s["status"] == s["html_state"] == "archival_replay"
        assert s["html_coverage"] == s["artifact_coverage"] == "3/64"
        errors = {
            "reversed": attribute(
                page(
                    client,
                    web + "/stations",
                    start_date="2026-09-02",
                    end_date="2026-09-01",
                ),
                "data-stations-state",
            ),
            "half": attribute(
                page(client, web + "/", start_date="2026-09-02"), "data-overview-state"
            ),
            "unknown_station": attribute(
                page(client, web + "/stations", station_id="no-such-station"),
                "data-station-detail",
            ),
            "invalid_vehicle": attribute(
                page(client, web + "/analytics", vehicle="scooter"),
                "data-analytics-state",
            ),
        }
        assert errors == {
            "reversed": "error",
            "half": "error",
            "unknown_station": "empty",
            "invalid_vehicle": "error",
        }, errors
        checks["filter_states"] = errors
        endpoints: dict[str, tuple[str, dict[str, Any]]] = {
            "v2_kpi_28d": ("/v2/analytics/kpi", period),
            "v2_timeseries_28d": ("/v2/analytics/timeseries", period),
            "v2_compare_28d": (
                "/v2/analytics/compare",
                {**period, "compare_start": baseline[0], "compare_end": baseline[1]},
            ),
            "v4_stations_28d_map": ("/v4/stations", period),
            "v4_stations_full_window": ("/v4/stations", {}),
            "v4_station_detail": ("/v4/stations/6611.07", period),
            "v4_risk": ("/v4/risk", {}),
            "v3_forecasts": ("/v3/forecasts", {"limit": 48}),
        }
        checks["api_latency"] = {
            name: latency(client, api + path, samples, **params)
            for name, (path, params) in endpoints.items()
        }
        pages = {
            "overview": "/",
            "analytics_default": "/analytics",
            "analytics_28d_station": f"/analytics?start_date={window[0]}"
            f"&end_date={window[1]}&station_id=6611.07",
            "stations": "/stations",
            "station_detail": "/stations?station_id=6611.07",
            "forecasts": "/forecasts",
            "scenarios": "/scenarios",
        }
        checks["html_render"] = {}
        for name, path in pages.items():
            stats = latency(client, web + path, max(3, samples // 4))
            stats["bytes"] = len(client.get(web + path).content)
            checks["html_render"][name] = stats
    return {"mode": "real_official", "publication_id": pub["publication_id"], **checks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("fixture", "real"), default="fixture")
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--web-url", default="http://127.0.0.1:3000")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--data-dir", type=Path, default=Path(".cache/f3-smoke"))
    args = parser.parse_args()
    api, web = args.api_url.rstrip("/"), args.web_url.rstrip("/")
    result = (
        fixture(api, web, args.data_dir)
        if args.mode == "fixture"
        else real(api, web, args.samples)
    )
    result["checked_at"] = datetime.now(UTC).isoformat()
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text[:4000])


if __name__ == "__main__":
    main()
