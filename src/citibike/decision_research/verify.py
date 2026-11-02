"""Independent decision-research checks, paired uncertainty and evidence."""

import importlib
import itertools
import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

import duckdb

from citibike.decision_research import (
    assert_hashes,
    checked_protocol,
    code_hash,
    models,
    protected,
    read_frame,
    research_root,
    search_variants,
    select_features,
    select_params,
)
from citibike.decision_research.features import (
    CATALOG,
    CLOSURE_EXPECTED_EVENTS,
    CLOSURE_MIN_HOURS,
    GROUPS,
    SHRINK_LABELS,
)
from citibike.features.contracts import NY, SEED, TYPES, holiday
from citibike.forecasting.artifacts import active, checked, create
from citibike.forecasting.experiment import promotion
from citibike.forecasting.models import load as load_model
from citibike.forecasting.verify import diagnostic_oracle, oracle_file
from citibike.ingestion.archive import checksum
from citibike.storage import identity, read_json, write_json

KEYS = ["fold", "station_id", "forecast_origin_utc", "target_hour_utc", "horizon_h"]
CANDIDATES = (
    "lightgbm_reference",
    "catboost_reference",
    "catboost_reference_A",
    "poisson_selected",
    "lightgbm_selected",
    "catboost_selected",
    "hurdle_selected",
)
REPLAY = {
    "lightgbm__base": "lightgbm_bounded",
    "catboost__base": "catboost_bounded",
    "seasonal_naive": "seasonal_naive",
}


def protocol_evidence(path: Path, output: Path) -> None:
    """Public record of the frozen plan, committed and pushed before any fit."""
    report = checked(path)
    plan = report["plan"]
    write_json(
        output,
        {
            "status": "protocol_frozen_no_fits",
            "protocol_id": report["id"],
            "protocol_path": path.as_posix(),
            "protocol_manifest_sha256": checksum(path / "manifest.json"),
            "created_at": report["created_at"],
            "plan": {k: v for k, v in plan.items() if k != "protected_files"},
            "protected_files": len(plan["protected_files"]),
            "protected_files_sha256": identity(plan["protected_files"]),
        },
    )


def expected_calendar(hour: Any) -> dict[str, float]:
    local = hour.to_pydatetime().astimezone(NY)
    day = local.date()
    following = HOLIDAY_REACH
    for step in range(HOLIDAY_REACH):
        if holiday(day + timedelta(days=step)):
            following = step
            break
    previous = HOLIDAY_REACH
    for step in range(HOLIDAY_REACH):
        if holiday(day - timedelta(days=step)):
            previous = step
            break
    number = local.timetuple().tm_yday
    return {
        "doy_sin1": math.sin(2 * math.pi * number / 365.25),
        "doy_cos1": math.cos(2 * math.pi * number / 365.25),
        "doy_sin2": math.sin(4 * math.pi * number / 365.25),
        "doy_cos2": math.cos(4 * math.pi * number / 365.25),
        "days_to_holiday": float(following),
        "days_from_holiday": float(previous),
    }


HOLIDAY_REACH = 7


class Source:
    """Month aggregates of one dense file, by a separate pandas path."""

    def __init__(self, dense: Path) -> None:
        pd = importlib.import_module("pandas")
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='2GB'")
            raw = db.execute(
                "SELECT station_id,hour_utc,month_key,label_available_at,"
                "departures,arrivals FROM read_parquet(?)",
                [str(dense)],
            ).df()
        for kind in TYPES:
            raw[kind] = raw[kind].astype("Float64").to_numpy(float, na_value=math.nan)
        self.closures = closure_hours(raw)
        parts = []
        for kind in TYPES:
            part = raw[["station_id", "hour_utc", "month_key", "label_available_at"]]
            part = part.assign(prediction_type=kind, actual=raw[kind]).dropna(
                subset=["actual"]
            )
            parts.append(part)
        long = pd.concat(parts, ignore_index=True)
        local = long["hour_utc"].dt.tz_convert(NY)
        long["weekday"] = local.dt.weekday.astype(int)
        long["local_hour"] = local.dt.hour.astype(int)
        long["square"] = long["actual"] ** 2
        long["zero"] = (long["actual"] == 0).astype(int)
        self.available = (
            long.groupby("month_key")["label_available_at"].max().sort_index()
        )
        base = ["month_key", "prediction_type"]
        measures = {
            "n": ("actual", "size"),
            "total": ("actual", "sum"),
            "squares": ("square", "sum"),
            "zeros": ("zero", "sum"),
        }
        self.tables = {
            "station": long.groupby(["station_id", *base])
            .agg(**measures)
            .reset_index(),
            "cell": long.groupby(["station_id", "weekday", "local_hour", *base])
            .agg(**measures)
            .reset_index(),
            "pool_cell": long.groupby(["weekday", "local_hour", *base])
            .agg(**measures)
            .reset_index(),
            "pool": long.groupby(base).agg(**measures).reset_index(),
        }


def closure_hours(raw: Any) -> set[tuple[str, Any]]:
    """Plain run-length scan, independent of the SQL gaps-and-islands rule."""
    flagged: set[tuple[str, Any]] = set()
    ordered = raw.sort_values(["station_id", "hour_utc"])
    for (station, _), group in ordered.groupby(["station_id", "month_key"], sort=False):
        hours = group["hour_utc"].tolist()
        dep = group["departures"].tolist()
        arr = group["arrivals"].tolist()
        observed = [
            not (math.isnan(d) and math.isnan(a)) for d, a in zip(dep, arr, strict=True)
        ]
        events = [
            (0 if math.isnan(d) else d) + (0 if math.isnan(a) else a)
            for d, a in zip(dep, arr, strict=True)
        ]
        seen = sum(observed)
        rate = sum(events) / seen if seen else 0.0
        run: list[Any] = []
        for hour, flag, count in [
            *zip(hours, observed, events, strict=True),
            (None, False, 1),
        ]:
            if flag and count == 0:
                run.append(hour)
                continue
            if len(run) >= CLOSURE_MIN_HOURS and (
                len(run) * rate >= CLOSURE_EXPECTED_EVENTS
            ):
                flagged.update((str(station), h) for h in run)
            run = []
    return flagged


def state(table: Any, months: list[str], keys: list[str]) -> Any:
    visible = table[table["month_key"].isin(months)]
    return visible.groupby(keys)[["n", "total", "squares", "zeros"]].sum().reset_index()


def feature_oracle(
    file: Path, source: Source, fit_asof: str, limits: dict[str, list[float]]
) -> dict[str, int]:
    pd = importlib.import_module("pandas")
    np = importlib.import_module("numpy")

    frame = read_frame(file)
    checks = 0
    hours = frame["target_hour_utc"].drop_duplicates()
    expected = pd.DataFrame(
        {"target_hour_utc": hours, **_calendar_columns(hours)}
    ).merge(
        frame[["target_hour_utc", *GROUPS["C"]]],
        on="target_hour_utc",
        suffixes=("_e", ""),
    )
    for column in GROUPS["C"]:
        if not np.allclose(expected[column], expected[column + "_e"]):
            raise ValueError("Independent calendar feature mismatch")
        checks += len(frame)
    available = source.available
    cutoff = frame["research_cutoff"]
    frame["visible"] = available.searchsorted(cutoff, side="right")
    key = ["station_id", "prediction_type"]
    cell_key = [*key, "weekday", "local_hour"]
    pool_key = ["prediction_type", "weekday", "local_hour"]
    months_order = list(available.index)
    parts = []
    for count, rows in frame.groupby("visible", sort=True):
        months = months_order[: int(count)]
        s = state(source.tables["station"], months, key)
        c = state(source.tables["cell"], months, cell_key)
        p = state(source.tables["pool_cell"], months, pool_key)
        o = state(source.tables["pool"], months, ["prediction_type"])
        merged = rows.merge(s, on=key, how="left", suffixes=("", "_s"), validate="m:1")
        merged = merged.merge(
            c.rename(columns=lambda n: n if n in cell_key else n + "_c"),
            on=cell_key,
            how="left",
            validate="m:1",
        )
        merged = merged.merge(
            p.rename(columns=lambda n: n if n in pool_key else n + "_p"),
            on=pool_key,
            how="left",
            validate="m:1",
        )
        merged = merged.merge(
            o.rename(columns=lambda n: n if n == "prediction_type" else n + "_o"),
            on="prediction_type",
            how="left",
            validate="m:1",
        )
        pool = source.tables["pool"]
        visible = pool[pool["month_key"].isin(months)]
        latest: dict[str, tuple[str, float]] = {}
        ago: dict[tuple[str, str], float] = {}
        for row in visible.itertuples(index=False):
            ago[(row.prediction_type, row.month_key)] = row.total / row.n
            current = latest.get(row.prediction_type)
            if current is None or row.month_key > current[0]:
                latest[row.prediction_type] = (row.month_key, row.total / row.n)
        local = merged["target_hour_utc"].dt.tz_convert(NY)
        wanted = [
            f"{y - 1}-{m:02d}"
            for y, m in zip(local.dt.year, local.dt.month, strict=True)
        ]
        merged["season_expected"] = [
            ago[(t, w)] / latest[t][1]
            if (t, w) in ago and latest[t][1]
            else float("nan")
            for t, w in zip(merged["prediction_type"], wanted, strict=True)
        ]
        parts.append(merged)
    data = pd.concat(parts, ignore_index=True)
    hist = data["total"] / data["n"]
    cell = data["total_c"] / data["n_c"]
    pool = data["total_p"] / data["n_p"]
    scale = (hist / (data["total_o"] / data["n_o"])).where(
        (data["total_o"] / data["n_o"]) != 0
    )
    scale = scale.fillna(1.0)
    shrunk = (data["total_c"].fillna(0) + SHRINK_LABELS * pool * scale) / (
        data["n_c"].fillna(0) + SHRINK_LABELS
    )
    variance = (data["squares"] / data["n"] - hist**2).clip(lower=0)
    cell_variance = (data["squares_c"] / data["n_c"] - cell**2).clip(lower=0)
    expect = {
        "hist_mean": hist,
        "hist_zero_share": data["zeros"] / data["n"],
        "hist_cv": (variance**0.5 / hist).where(hist > 0),
        "hist_n": data["n"],
        "cell_std": cell_variance**0.5,
        "cell_zero_share": data["zeros_c"] / data["n_c"],
        "cell_n": data["n_c"],
        "shape_share": (cell / hist).where(hist > 0),
        "pool_cell_mean": pool,
        "shrunk_cell_mean": shrunk,
        "season_index": data["season_expected"],
        "adjusted_profile": shrunk * data["season_expected"],
    }
    for column, values in expect.items():
        left, right = data[column].to_numpy(float), values.to_numpy(float)
        if not np.array_equal(np.isnan(left), np.isnan(right)):
            raise ValueError(f"Missing history became numeric or vice versa: {column}")
        if not np.allclose(left, right, rtol=1e-9, atol=1e-9, equal_nan=True):
            raise ValueError(f"Independent published feature mismatch: {column}")
        checks += len(data)
    bands = data["hist_mean"].copy() * float("nan")
    for kind, edges in limits.items():
        mask = (data["prediction_type"] == kind) & data["hist_mean"].notna()
        bands[mask] = sum(
            (data.loc[mask, "hist_mean"] >= e).astype(float) for e in edges
        )
    if not np.array_equal(
        bands.to_numpy(float), data["hist_band"].to_numpy(float), equal_nan=True
    ):
        raise ValueError("Train-only band mismatch")
    flagged = [
        (s, h) in source.closures
        for s, h in zip(data["station_id"], data["target_hour_utc"], strict=True)
    ]
    if flagged != data["suspect_closure"].astype(bool).tolist():
        raise ValueError("Independent suspect closure mismatch")
    canonical = data["station_id"].map(
        lambda s: (
            re.fullmatch(r"\d{4}\.\d{1,2}|[A-Z]{2}\d{3}", s) is None or s == "1234.56"
        )
    )
    if canonical.tolist() != data["anomalous_id"].astype(bool).tolist():
        raise ValueError("Independent anomalous ID mismatch")
    limit = datetime.fromisoformat(fit_asof)
    if bool(
        (
            (data["research_cutoff"] > limit)
            | (data["research_cutoff"] > data["forecast_origin_utc"])
            | (data["d_available_at"] > data["research_cutoff"])
            | (data["d_max_event_at"] >= data["research_cutoff"])
        ).any()
    ):
        raise ValueError("Independent feature availability violation")
    return {"rows": len(data), "feature_values": checks, "mismatches": 0}


def _calendar_columns(hours: Any) -> dict[str, list[float]]:
    values = [expected_calendar(h) for h in hours]
    return {k: [v[k] for v in values] for k in GROUPS["C"]}


def bootstrap(
    left: Any,
    right: Any,
    *,
    replicates: int,
    alpha: float | None = None,
) -> dict[str, Any]:
    """Crossed station x day weights on paired errors; rows are never independent."""
    np = importlib.import_module("numpy")

    a = left.sort_values(KEYS).reset_index(drop=True)
    b = right.sort_values(KEYS).reset_index(drop=True)
    if not a[[*KEYS, "actual"]].equals(b[[*KEYS, "actual"]]):
        raise ValueError("Paired populations differ")
    delta = (a["predicted"] - a["actual"]).abs() - (b["predicted"] - b["actual"]).abs()
    data = a[["fold"]].copy()
    data["station"], stations = a["station_id"].factorize(sort=True)
    data["day"], days = (
        a["target_hour_utc"].dt.tz_convert(NY).dt.date.factorize(sort=True)
    )
    data["delta"] = delta.to_numpy(float)
    grouped = (
        data.groupby(["fold", "station", "day"], sort=True)["delta"]
        .agg(["sum", "count"])
        .reset_index()
    )
    fold_index, fold_levels = grouped["fold"].factorize(sort=True)
    station_index = grouped["station"].to_numpy(int)
    day_index = grouped["day"].to_numpy(int)
    sums, counts = grouped["sum"].to_numpy(float), grouped["count"].to_numpy(float)
    rng = np.random.default_rng(SEED)
    boot = []
    for _ in range(replicates):
        sw = rng.multinomial(len(stations), np.ones(len(stations)) / len(stations))
        dw = rng.multinomial(len(days), np.ones(len(days)) / len(days))
        weight = sw[station_index] * dw[day_index]
        bottom = np.bincount(fold_index, weights=weight * counts)
        top = np.bincount(fold_index, weights=weight * sums)
        if (bottom > 0).all():
            boot.append(float(np.mean(top / bottom)))
    differences = [
        float(data.loc[data["fold"] == f, "delta"].mean()) for f in fold_levels
    ]
    result = {
        "macro_mae_difference_left_minus_right": float(np.mean(differences)),
        "fold_differences": dict(zip(fold_levels, differences, strict=True)),
        "folds_left_better": sum(v < 0 for v in differences),
        "percentile_95": np.quantile(boot, [0.025, 0.975]).tolist() if boot else None,
        "bootstrap_sign_p": min(
            1.0,
            2 * min(np.mean(np.array(boot) >= 0), np.mean(np.array(boot) <= 0)),
        )
        if boot
        else None,
        "fold_sign_flip_p": sign_flip(differences),
        "replicates": len(boot),
        "stations": len(stations),
        "days": len(days),
        "rows": len(a),
    }
    if alpha is not None and boot:
        result["familywise_interval"] = np.quantile(
            boot, [alpha / 2, 1 - alpha / 2]
        ).tolist()
    return result


def sign_flip(values: list[float]) -> float:
    """Exact two-sided test over fold means; four folds cannot go below 0.125."""
    observed = abs(mean(values))
    flips = list(itertools.product((1, -1), repeat=len(values)))
    extreme = sum(
        abs(mean(s * v for s, v in zip(signs, values, strict=True))) >= observed - 1e-15
        for signs in flips
    )
    return extreme / len(flips)


def holm(items: list[dict[str, Any]]) -> None:
    ordered = sorted(
        (i for i in items if i["bootstrap_sign_p"] is not None),
        key=lambda i: i["bootstrap_sign_p"],
    )
    running = 0.0
    for rank, row in enumerate(ordered):
        running = max(running, (len(ordered) - rank) * row["bootstrap_sign_p"])
        row["holm_p"] = min(1.0, running)


def station_metric(path: Path, station: str) -> dict[str, Any] | None:
    with duckdb.connect() as db:
        row = db.execute(
            """SELECT count(*),avg(abs(predicted-actual)) FROM read_parquet(?)
            WHERE station_id=?""",
            [str(path), station],
        ).fetchone()
    return {"n": row[0], "mae": row[1]} if row and row[0] else None


def gate(
    candidate: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    critical: list[str],
) -> dict[str, Any]:
    """The accepted F4 gate plus the critical segments of the previous study."""
    result = promotion(candidate, baseline, strict_validation=False)
    by_fold = {r["fold"]: r for r in baseline}
    for row in candidate:
        base = by_fold[row["fold"]]
        for name in critical:
            if name.startswith("station:"):
                ours = station_metric(row["predictions"], name[8:])
                theirs = station_metric(base["predictions"], name[8:])
            else:
                ours = row["metrics"].get(name)
                theirs = base["metrics"].get(name)
            if (
                ours
                and theirs
                and ours["n"] >= 100
                and ours["mae"] > theirs["mae"] * 1.2 + 1e-9
            ):
                result["degraded_segments"].append(f"{row['fold']}:{name}")
    result["promote"] = False
    return result


def passes(item: dict[str, Any]) -> bool:
    return bool(
        item["majority"]
        and not item["degraded_segments"]
        and item["resources_and_fit_ok"]
    )


def decide(
    gates: dict[tuple[str, str, str], dict[str, Any]],
    comparisons: list[dict[str, Any]],
    kind: str,
) -> dict[str, Any]:
    """The frozen rule; cohort256 is reported and cannot add a candidate."""
    intervals = {
        (c["population"], c["left"]): c
        for c in comparisons
        if c["target"] == kind and c["right"] == "seasonal_naive" and c["primary"]
    }
    eligible, blockers = [], {}
    for name in CANDIDATES:
        failed: dict[str, Any] = {}
        for population in ("cohort64", "cohort128"):
            item = gates.get((population, kind, name))
            paired = intervals.get((population, name))
            if item is None or paired is None:
                failed[population] = "no complete score"
                continue
            interval = paired.get("familywise_interval")
            significant = bool(interval and interval[1] < 0)
            if not (passes(item) and significant):
                failed[population] = {
                    "wins": item["wins"],
                    "windows": item["windows"],
                    "degraded_segments": len(item["degraded_segments"]),
                    "resources_and_fit_ok": item["resources_and_fit_ok"],
                    "familywise_interval": interval,
                }
        if failed:
            blockers[name] = failed
        else:
            eligible.append((gates[("cohort64", kind, name)]["macro_mae"], name))
    return {
        "target": kind,
        "research_candidate": min(eligible)[1] if eligible else "seasonal_naive",
        "eligible": [name for _, name in sorted(eligible)],
        "blockers": blockers,
        "operational": "unchanged Seasonal Naive; strict/forward NOT VERIFIED",
    }


def trial_check(
    row: dict[str, Any], plan: dict[str, Any], protocol: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Every saved trial: metrics, diagnostics, rows, train limits, model parity."""
    path = Path(row["path"])
    assert_hashes({str(path / "manifest.json"): row["sha256"]})
    result = checked(path)
    request = result["request"]
    if request["protocol_id"] != protocol["id"] or result["final_test_accessed"]:
        raise ValueError("Trial protocol/test isolation mismatch")
    if datetime.fromisoformat(result["created_at"]) < datetime.fromisoformat(
        protocol["created_at"]
    ):
        raise ValueError("Trial preceded protocol")
    predictions = path / result["predictions"]
    count = oracle_file(predictions, result["metrics"])
    diagnostic_oracle(predictions, result["diagnostics"])
    kind = request["prediction_type"]
    with duckdb.connect() as db:
        keys = "station_id,forecast_origin_utc,target_hour_utc,horizon_h,actual"
        db.read_parquet(str(predictions)).create_view("p")
        db.read_parquet(request["evaluation_file"]).create_view("reference_all")
        db.sql(
            f"SELECT * FROM reference_all WHERE prediction_type='{kind}'"
        ).create_view("reference")
        for left, right in (("p", "reference"), ("reference", "p")):
            if db.execute(
                f"SELECT count(*) FROM(SELECT {keys} FROM {left} "
                f"EXCEPT ALL SELECT {keys} FROM {right})"
            ).fetchone() != (0,):
                raise ValueError("Independent same-row membership failure")
        if db.execute(
            "SELECT count(*) FROM read_parquet(?) WHERE label_available_at>"
            "?::TIMESTAMPTZ OR target_hour_utc>=?::TIMESTAMPTZ-INTERVAL '24 hours'",
            [request["train_file"], request["fit_asof"], request["fit_asof"]],
        ).fetchone() != (0,):
            raise ValueError("Independent train availability/embargo failure")
        final = plan["final_window_not_used"][0] + "T00:00:00+00:00"
        if db.execute(
            "SELECT count(*) FROM p WHERE target_hour_utc>=?::TIMESTAMPTZ", [final]
        ).fetchone() != (0,):
            raise ValueError("Final-window row reached a trial")
        scoring = db.execute(
            """SELECT r.*,p.predicted FROM reference r JOIN p USING
            (station_id,forecast_origin_utc,horizon_h) ORDER BY
            station_id,forecast_origin_utc,horizon_h LIMIT 48"""
        ).df()
    fitted = load_model(path / "model.joblib")
    if fitted["feature_names"] != models.names(request["config"]["groups"]):
        raise ValueError("Ablation feature matrix mismatch")
    if models.predict(fitted, scoring)[0] != scoring["predicted"].tolist():
        raise ValueError("Independent saved-model parity failure")
    return result, count


def frozen_replay(root: Path, stage: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bounded base variants must reproduce the frozen F4 validation exactly."""
    experiment = active(root, "experiments")
    report = checked(experiment)
    keys = ["station_id", "forecast_origin_utc", "horizon_h"]
    rows = []
    for item in stage:
        if item["name"] not in REPLAY or item["status"] != "success":
            continue
        fold = int(item["fold"][1:])
        reference = next(
            r
            for r in report["results"]
            if r["fold"] == fold
            and r["model_name"] == REPLAY[item["name"]]
            and r["prediction_type"] == item["prediction_type"]
        )
        ours = read_frame(Path(item["path"]) / "predictions.parquet")
        frozen = read_frame(experiment / reference["predictions"])
        merged = ours[[*keys, "predicted"]].merge(
            frozen[[*keys, "predicted"]],
            on=keys,
            how="outer",
            suffixes=("", "_frozen"),
            indicator=True,
        )
        if bool((merged["_merge"] != "both").any()):
            raise ValueError("Frozen replay row membership mismatch")
        difference = (merged["predicted"] - merged["predicted_frozen"]).abs()
        rows.append(
            {
                "fold": item["fold"],
                "target": item["prediction_type"],
                "variant": item["name"],
                "rows": len(merged),
                "max_absolute_difference": float(difference.max()),
            }
        )
    return rows


def frames_of(rows: list[dict[str, Any]]) -> Any:
    pd = importlib.import_module("pandas")
    parts = []
    for row in rows:
        frame = read_frame(Path(row["path"]) / "predictions.parquet")
        frame["fold"] = row["fold"]
        parts.append(frame[[*KEYS, "actual", "predicted"]])
    return pd.concat(parts, ignore_index=True)


def fold_mean(
    reports: list[dict[str, Any]], field: str, segment: str = "all"
) -> float | None:
    values = [
        r["metrics"][segment][field]
        for r in reports
        if segment in r["metrics"] and r["metrics"][segment][field] is not None
    ]
    return round(mean(values), 6) if values else None


def score_rows(
    stage: str, rows: list[dict[str, Any]], reports: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "success":
            key = (row["population"], row["prediction_type"], row["name"])
            grouped.setdefault(key, []).append(row)
    output = []
    for (population, kind, name), members in sorted(grouped.items()):
        reports_for = [reports[m["path"]] for m in members]
        folds = {
            m["fold"]: reports[m["path"]]["metrics"]["all"]["mae"] for m in members
        }

        output.append(
            {
                "stage": stage,
                "population": population,
                "target": kind,
                "variant": name,
                "folds": len(folds),
                "macro_mae": fold_mean(reports_for, "mae"),
                "macro_wape": fold_mean(reports_for, "wape"),
                "macro_rmse": fold_mean(reports_for, "rmse"),
                "macro_bias": fold_mean(reports_for, "bias"),
                "macro_peak_hour_mae": fold_mean(reports_for, "mae", "peak_hour"),
                "fold_mae": folds,
                "fold_std": round(pstdev(folds.values()), 6),
                "max_fit_seconds": max(
                    r["measurements"]["fit_seconds"] for r in reports_for
                ),
                "max_peak_rss_bytes": max(
                    r["measurements"]["peak_rss_bytes"] for r in reports_for
                ),
                "train_rows": reports_for[0]["measurements"]["training_rows"],
            }
        )
    return output


def segment_table(
    rows: list[dict[str, Any]], reports: dict[str, Any]
) -> dict[str, Any]:
    """Macro MAE and bias per segment for each model; absent is absent, never zero."""
    table: dict[str, Any] = {}
    for row in rows:
        if row["status"] != "success":
            continue
        report = reports[row["path"]]
        bucket = table.setdefault(row["prediction_type"], {}).setdefault(
            row["name"], {}
        )
        for source in ("metrics", "extra_metrics"):
            for segment, value in report[source].items():
                bucket.setdefault(segment, []).append(
                    (value["n"], value["mae"], value["bias"], value["wape"])
                )
    return {
        target: {
            name: {
                segment: {
                    "folds": len(values),
                    "n": sum(v[0] for v in values),
                    "macro_mae": round(mean(v[1] for v in values), 6),
                    "macro_bias": round(mean(v[2] for v in values), 6),
                }
                for segment, values in bucket.items()
            }
            for name, bucket in models_part.items()
        }
        for target, models_part in table.items()
    }


def stdlib_rank_correlation(left: list[float], right: list[float]) -> float:
    def ranks(values: list[float]) -> list[float]:
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            for k in range(i, j + 1):
                out[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return out

    a, b = ranks(left), ranks(right)
    ma, mb = mean(a), mean(b)
    top = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    bottom = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return top / bottom


def silver_oracle(gold: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Analytics numbers again from Silver rows, a different grain and code path."""
    source = read_json(gold / "manifest.json")
    snapshot = read_json(Path(source["source_snapshot"]) / "manifest.json")
    files = [
        str(Path(part["path"]) / "silver.parquet") for part in snapshot["partitions"]
    ]
    sections = report["sections"]
    checks = 0
    mismatches: list[str] = []

    def same(name: str, left: Any, right: Any) -> None:
        nonlocal checks
        checks += 1
        if isinstance(left, float) or isinstance(right, float):
            ok = math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-9)
        else:
            ok = left == right
        if not ok:
            mismatches.append(f"{name}: {left} != {right}")

    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='4GB'")
        db.read_parquet(files, union_by_name=True).create_view("silver")
        rows_months = db.execute(
            """SELECT strftime(timezone('America/New_York',started_at_utc),'%Y-%m') m,
            count(*),count(start_station_id) FROM silver GROUP BY 1"""
        ).fetchall()
        ends = dict(
            db.execute(
                """SELECT strftime(timezone('America/New_York',ended_at_utc),'%Y-%m'),
                count(end_station_id) FROM silver GROUP BY 1"""
            ).fetchall()
        )
        by_month = {r[0]: r for r in rows_months}
        for row in sections["1_trends"]["result"]["months"]:
            counted = by_month[row["month"]]
            same(f"rides:{row['month']}", row["completed_published_rides"], counted[1])
            same(f"departures:{row['month']}", row["departures"], counted[2])
            same(f"arrivals:{row['month']}", row["arrivals"], ends[row["month"]])
        hours = {
            (r[0], r[1]): r[2]
            for r in db.execute(
                """SELECT isodow(timezone('America/New_York',started_at_utc))-1,
                hour(timezone('America/New_York',started_at_utc)),count(*) FROM silver
                WHERE started_at_utc>=TIMESTAMPTZ '2024-10-01 04:00:00+00'
                AND started_at_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00'
                GROUP BY 1,2"""
            ).fetchall()
        }
        for row in sections["2_patterns"]["result"]["hour_of_week"]:
            slot = (row["weekday"], row["local_hour"])
            same(f"hour_of_week:{slot}", row["rides"], hours[slot])
        window = """started_at_utc>=TIMESTAMPTZ '2025-10-01 04:00:00+00'
            AND started_at_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00'"""
        for row in sections["4_imbalance"]["result"]["routes"][:40]:
            found = db.execute(
                f"""SELECT count(*) FROM silver WHERE {window}
                AND start_station_id=? AND end_station_id=?""",
                [row["from_station"], row["to_station"]],
            ).fetchone()
            assert found is not None
            same(
                f"route:{row['from_station']}>{row['to_station']}",
                row["route_volume"],
                found[0],
            )
        for row in sections["4_imbalance"]["result"]["stations"][:20]:
            leave = db.execute(
                f"""SELECT count(*) FROM silver WHERE {window}
                AND start_station_id=?""",
                [row["station_id"]],
            ).fetchone()
            enter = db.execute(
                """SELECT count(*) FROM silver WHERE end_station_id=?
                AND ended_at_utc>=TIMESTAMPTZ '2025-10-01 04:00:00+00'
                AND ended_at_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00'""",
                [row["station_id"]],
            ).fetchone()
            assert leave is not None and enter is not None
            same(f"station_dep:{row['station_id']}", row["departures"], leave[0])
            same(f"station_arr:{row['station_id']}", row["arrivals"], enter[0])
        categories = {
            (r[0], r[1], r[2]): (r[3], r[4])
            for r in db.execute(
                """SELECT CASE WHEN timezone('America/New_York',started_at_utc)
                    <DATE '2025-10-01' THEN '2024-10..2025-09'
                    ELSE '2025-10..2026-09' END,
                CASE WHEN rideable_type IN ('classic_bike','electric_bike')
                    THEN rideable_type ELSE 'unknown' END,
                CASE WHEN member_casual IN ('member','casual')
                    THEN member_casual ELSE 'unknown' END,count(*),
                sum(date_diff('microsecond',started_at_utc,ended_at_utc))::HUGEINT
                FROM silver WHERE
                started_at_utc>=TIMESTAMPTZ '2024-10-01 04:00:00+00'
                AND started_at_utc<TIMESTAMPTZ '2026-10-01 04:00:00+00'
                GROUP BY 1,2,3"""
            ).fetchall()
        }
        for row in sections["5_categories"]["result"]["annual"]:
            key = (row["period"], row["vehicle"], row["membership"])
            same(f"category:{key}", row["rides"], categories[key][0])
            same(
                f"duration:{key}",
                row["average_duration_s"],
                float(categories[key][1]) / categories[key][0] / 1e6,
            )
        station_totals = db.execute(
            """WITH d AS (SELECT strftime(timezone('America/New_York',started_at_utc),
                '%Y-%m') m,start_station_id s,count(*) n FROM silver
                WHERE start_station_id IS NOT NULL GROUP BY 1,2),
            e AS (SELECT strftime(timezone('America/New_York',ended_at_utc),'%Y-%m') m,
                end_station_id s,count(*) n FROM silver
                WHERE end_station_id IS NOT NULL GROUP BY 1,2)
            SELECT coalesce(d.m,e.m),coalesce(d.s,e.s),
                coalesce(d.n,0)+coalesce(e.n,0) FROM d FULL JOIN e USING(m,s)"""
        ).fetchall()
    grouped: dict[str, list[int]] = {}
    for month, _, events in station_totals:
        grouped.setdefault(month, []).append(events)
    for row in sections["3_concentration"]["result"]["monthly"]:
        values = sorted(grouped[row["month"]])
        n, total = len(values), sum(values)
        gini = (
            2 * sum((i + 1) * v for i, v in enumerate(values)) / (n * total)
            - (n + 1) / n
        )
        same(f"gini:{row['month']}", row["gini"], gini)
        same(f"ids:{row['month']}", row["observed_ids"], n)
        top = math.ceil(n * 0.1)
        same(f"top10:{row['month']}", row["top10pct_share"], sum(values[-top:]) / total)
    return {
        "checks": checks,
        "mismatches": len(mismatches),
        "examples": mismatches[:10],
    }


def f5_oracle(path: Path, section: dict[str, Any]) -> dict[str, Any]:
    """Policy means again from the immutable outcomes file with stdlib only."""
    groups: dict[tuple[Any, ...], list[int]] = {}
    with (path / "outcomes.jsonl").open(encoding="utf-8") as lines:
        for line in lines:
            row = json.loads(line)
            key = (row["scale"], row["order"], row["policy"])
            groups.setdefault(key, []).append(
                sum(
                    sum(field.values()) for field in row["outcome"]["rejected"].values()
                )
            )
    checks = mismatches = 0
    for cell in section["result"]["comparisons"]:
        for policy, values in cell["policies"].items():
            checks += 1
            expected = mean(groups[(cell["scale"], cell["order"], policy)])
            mismatches += not math.isclose(
                expected, values["unserved_total_mean"], abs_tol=1e-9
            )
    return {
        "checks": checks,
        "mismatches": mismatches,
        "outcome_rows": sum(map(len, groups.values())),
    }


def analytics_oracle(report: dict[str, Any]) -> dict[str, Any]:
    """Internal arithmetic of derived shares and changes, without SQL."""
    checks = mismatches = 0
    for row in report["sections"]["1_trends"]["result"]["year_over_year"]:
        checks += 1
        value = row["rides"] / row["baseline_rides"] - 1
        mismatches += not math.isclose(value, row["rides_change"], abs_tol=1e-12)
    for row in report["sections"]["4_imbalance"]["result"]["stations"]:
        checks += 1
        share = (row["arrivals"] - row["departures"]) / (
            row["arrivals"] + row["departures"]
        )
        mismatches += not math.isclose(share, row["net_share"], abs_tol=1e-12)
    for block in report["sections"]["5_categories"]["result"]["shares"].values():
        checks += 1
        mismatches += not math.isclose(sum(block["shares"].values()), 1.0)
    return {"checks": checks, "mismatches": mismatches}


def correlation_oracle(
    outcome: dict[str, Any], report: dict[str, Any]
) -> dict[str, Any]:
    """Station WAPE from raw predictions, then Spearman in plain Python."""
    section = report["sections"]["7_errors_vs_flows"]["result"]
    rows = [
        r
        for r in outcome["stages"]["S3_holdout"]
        if r["population"] == "cohort64"
        and r["name"] == "seasonal_naive"
        and r["status"] == "success"
    ]
    data = frames_of(rows)
    data["absolute"] = (data["predicted"] - data["actual"]).abs()
    totals = data.groupby("station_id")[["absolute", "actual"]].sum()
    table = [
        r
        for r in section["stations"]
        if r["model"] == "seasonal_naive" and r["abs_net_share"] is not None
    ]
    usable = [
        r
        for r in table
        if r["station_id"] in totals.index and totals.loc[r["station_id"], "actual"] > 0
    ]
    wape = [
        float(
            totals.loc[r["station_id"], "absolute"]
            / totals.loc[r["station_id"], "actual"]
        )
        for r in usable
    ]
    rho = stdlib_rank_correlation(wape, [float(r["abs_net_share"]) for r in usable])
    expected = section["spearman"]["seasonal_naive:wape~abs_net_share"]["rho"]
    return {
        "stations": len(usable),
        "stdlib_rho": rho,
        "sql_rho": expected,
        "mismatches": int(not math.isclose(rho, expected, abs_tol=1e-9)),
    }


def evidence_summary(
    protocol: dict[str, Any],
    outcome: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    plan = protocol["plan"]
    return {
        "status": outcome["status"],
        "mode": "exploratory validation-only; no new independent test period",
        "protocol_id": protocol["id"],
        "protocol_created_at": protocol["created_at"],
        "outcome_id": outcome["id"],
        "remote_proof": outcome["remote_proof"],
        "catalog": CATALOG,
        "preprocessing_rules": plan["preprocessing_rules"],
        "feature_selection": outcome["feature_selection"],
        "parameter_selection": outcome["parameter_selection"],
        "outcome_counts": outcome["outcome_counts"],
        "cumulative_worker_seconds": outcome["cumulative_worker_seconds"],
        "audit": audit,
    }


def verify(result: Path, analytics_path: Path, output: Path) -> Path:
    outcome = checked(result)
    protocol_path = Path(outcome["protocol"])
    protocol = checked_protocol(protocol_path)
    plan = protocol["plan"]
    root = result.parents[3]
    work = research_root(root)
    if outcome["protocol_id"] != protocol["id"] or outcome["final_test_accessed"]:
        raise ValueError("Mixed protocol/test access")
    every = [row for rows in outcome["stages"].values() for row in rows]
    seen: dict[str, dict[str, Any]] = {}
    oracle_rows = 0
    for row in every:
        if row["status"] == "success" and row["path"] not in seen:
            report, count = trial_check(row, plan, protocol)
            seen[row["path"]] = report
            oracle_rows += count
    feature_audit: dict[str, int] = {
        "rows": 0,
        "feature_values": 0,
        "files": 0,
        "mismatches": 0,
    }
    for population in {p["inputs"]: p for p in plan["populations"].values()}.values():
        path = Path(population["inputs"])
        source = Source(Path(population["dataset"]) / "dense.parquet")
        for split in checked(path)["splits"]:
            for field in ("train_file", "evaluation_file"):
                checked_file = feature_oracle(
                    path / split[field],
                    source,
                    split["fit_asof"],
                    split["band_thresholds"],
                )
                feature_audit["rows"] += checked_file["rows"]
                feature_audit["feature_values"] += checked_file["feature_values"]
                feature_audit["files"] += 1
    replay = frozen_replay(root, outcome["stages"]["S1_features"])
    chain = selection_chain(outcome, plan)
    scores = [
        s
        for stage, rows in outcome["stages"].items()
        for s in score_rows(stage, rows, seen)
    ]
    holdout = outcome["stages"]["S3_holdout"]
    gates: dict[tuple[str, str, str], dict[str, Any]] = {}
    frames: dict[tuple[str, str, str], Any] = {}
    for population in plan["populations"]:
        for kind in TYPES:
            part = [
                r
                for r in holdout
                if r["population"] == population
                and r["prediction_type"] == kind
                and r["status"] == "success"
            ]
            baseline = [
                {
                    **seen[r["path"]],
                    "fold": r["fold"],
                    "predictions": Path(r["path"]) / "predictions.parquet",
                }
                for r in part
                if r["name"] == "seasonal_naive"
            ]
            names = sorted({r["name"] for r in part} - {"seasonal_naive"})
            for name in ["seasonal_naive", *names]:
                members = [r for r in part if r["name"] == name]
                frames[(population, kind, name)] = frames_of(members)
                if name == "seasonal_naive":
                    continue
                candidate = [
                    {
                        **seen[r["path"]],
                        "fold": r["fold"],
                        "predictions": Path(r["path"]) / "predictions.parquet",
                    }
                    for r in members
                ]
                gates[(population, kind, name)] = {
                    **gate(candidate, baseline, plan["critical_extra"]),
                    "macro_mae": mean(c["metrics"]["all"]["mae"] for c in candidate),
                }
    primary = [
        (population, kind, name)
        for population in ("cohort64", "cohort128")
        for kind in TYPES
        for name in CANDIDATES
        if (population, kind, name) in frames
    ]
    uncertainty = plan["uncertainty"]
    alpha = 0.05 / max(len(primary), 1)
    comparisons: list[dict[str, Any]] = []
    for population, kind, name in primary:
        comparisons.append(
            {
                "population": population,
                "target": kind,
                "left": name,
                "right": "seasonal_naive",
                "primary": True,
                **bootstrap(
                    frames[(population, kind, name)],
                    frames[(population, kind, "seasonal_naive")],
                    replicates=uncertainty["replicates_primary"],
                    alpha=alpha,
                ),
            }
        )
    holm(comparisons)
    secondary = [
        ("hurdle_selected", "lightgbm_selected"),
        ("lightgbm_selected", "lightgbm_reference"),
        ("catboost_selected", "catboost_reference"),
        ("catboost_selected", "catboost_reference_A"),
    ]
    for population in plan["populations"]:
        for kind in TYPES:
            for left, right in secondary:
                if (population, kind, left) in frames and (
                    population,
                    kind,
                    right,
                ) in frames:
                    comparisons.append(
                        {
                            "population": population,
                            "target": kind,
                            "left": left,
                            "right": right,
                            "primary": False,
                            **bootstrap(
                                frames[(population, kind, left)],
                                frames[(population, kind, right)],
                                replicates=uncertainty["replicates_secondary"],
                            ),
                        }
                    )
            if population == "cohort256":
                for name in CANDIDATES:
                    if (population, kind, name) in frames:
                        comparisons.append(
                            {
                                "population": population,
                                "target": kind,
                                "left": name,
                                "right": "seasonal_naive",
                                "primary": False,
                                **bootstrap(
                                    frames[(population, kind, name)],
                                    frames[(population, kind, "seasonal_naive")],
                                    replicates=uncertainty["replicates_secondary"],
                                ),
                            }
                        )
    decisions = {kind: decide(gates, comparisons, kind) for kind in TYPES}
    ablation = ablations(outcome, plan, frames, uncertainty)
    analytics = checked(analytics_path)
    gold = Path(checked(Path(plan["populations"]["cohort64"]["dataset"]))["gold_path"])
    assert_hashes(plan["protected_files"])
    if protected(root, gold) != plan["protected_files"]:
        raise ValueError("Protected artifact set changed")
    silver = silver_oracle(gold, analytics)
    if silver["mismatches"]:
        raise ValueError(f"Independent analytics mismatch: {silver['examples']}")
    policy = f5_oracle(
        Path(analytics["f5_result"]), analytics["sections"]["8_f5_policies"]
    )
    arithmetic = analytics_oracle(analytics)
    correlation = correlation_oracle(outcome, analytics)
    if policy["mismatches"] or arithmetic["mismatches"] or correlation["mismatches"]:
        raise ValueError("Independent analytics arithmetic mismatch")
    robustness = [
        {
            "population": r["population"],
            "fold": r["fold"],
            "target": r["prediction_type"],
            "variant": r["name"],
            **seen[r["path"]]["robustness"],
        }
        for r in holdout
        if r["status"] == "success" and seen[r["path"]].get("robustness")
    ]
    calibration = [
        {
            "fold": r["fold"],
            "population": r["population"],
            "target": r["prediction_type"],
            **{
                k: v
                for k, v in seen[r["path"]]["occurrence_calibration"].items()
                if k != "deciles"
            },
        }
        for r in holdout
        if r["status"] == "success" and seen[r["path"]].get("occurrence_calibration")
    ]
    fit_times = [
        s["measurements"]["fit_seconds"]
        for s in seen.values()
        if "fit_seconds" in s["measurements"]
    ]
    rss = [
        s["measurements"]["peak_rss_bytes"]
        for s in seen.values()
        if s["measurements"].get("peak_rss_bytes")
    ]
    audit: dict[str, Any] = {
        "unique_trials": len(seen),
        "stdlib_oracle_rows": oracle_rows,
        "metric_mismatches": 0,
        "row_membership_mismatches": 0,
        "feature_oracle": feature_audit,
        "selection_chain": chain,
        "frozen_replay": {
            "comparisons": len(replay),
            "exact": sum(r["max_absolute_difference"] == 0 for r in replay),
            "max_absolute_difference": max(
                (r["max_absolute_difference"] for r in replay), default=None
            ),
        },
        "silver_oracle": silver,
        "f5_oracle": policy,
        "arithmetic_oracle": arithmetic,
        "correlation_oracle": correlation,
        "protected_files_unchanged": len(plan["protected_files"]),
        "verifier_sha256": code_hash(Path(__file__)),
        "max_fit_seconds": max(fit_times, default=None),
        "max_child_peak_rss_bytes": max(rss, default=None),
    }
    evidence = {
        **evidence_summary(protocol, outcome, audit),
        "readiness": read_json(Path(plan["readiness"]) / "manifest.json")
        if plan["readiness"]
        else None,
        "plan": {k: v for k, v in plan.items() if k != "protected_files"},
        "scores": scores,
        "gates": [
            {
                "population": p,
                "target": k,
                "variant": n,
                **{f: v for f, v in g.items() if f != "reason"},
            }
            for (p, k, n), g in sorted(gates.items())
        ],
        "comparisons": comparisons,
        "familywise_alpha": alpha,
        "primary_comparisons": len(primary),
        "decisions": decisions,
        "segments": {
            population: segment_table(
                [r for r in holdout if r["population"] == population], seen
            )
            for population in plan["populations"]
        },
        "ablation": ablation,
        "station_comparisons": [
            {
                "population": population,
                "target": kind,
                "variant": name,
                **station_compare(
                    frames[(population, kind, name)],
                    frames[(population, kind, "seasonal_naive")],
                ),
            }
            for (population, kind, name) in sorted(frames)
            if name != "seasonal_naive"
        ],
        "degraded_by_fold": degraded_by_fold(gates),
        "robustness": robustness,
        "occurrence_calibration": calibration,
        "seed_stability": seed_stability(outcome, scores),
        "provenance_counts": {
            label: sum(r.get("provenance") == label for r in every)
            for label in sorted({str(r.get("provenance")) for r in every})
        },
        "analytics": {
            "id": analytics["id"],
            "gold_publication_id": analytics["gold_publication_id"],
            "gold_manifest_sha256": analytics["gold_manifest_sha256"],
            "measurements": analytics["measurements"],
            "sections": analytics["sections"],
        },
        "dashboard": dashboard_part(analytics, scores),
        "independent_new_test_period": "NOT VERIFIED: none exists; September 2026 "
        "was consumed and is not reused",
        "created_at": datetime.now(UTC).isoformat(),
    }
    target = create(
        work,
        "audits",
        identity(["audit", outcome["id"], analytics["id"], code_hash(Path(__file__))]),
        lambda stage: write_details(stage, evidence),
        activate=False,
    )
    write_json(output, evidence)
    return target


def selection_chain(outcome: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Selections are recomputed from trial scores with the frozen rules."""
    chosen = select_features(outcome["stages"]["S1_features"], plan["feature_variants"])
    if chosen != outcome["feature_selection"]:
        raise ValueError("Feature selection differs from the frozen rule")
    searched = search_variants(chosen, plan["smoke"])
    selected = select_params(outcome["stages"]["S2_search"], searched)
    if selected != outcome["parameter_selection"]:
        raise ValueError("Parameter selection differs from the frozen rule")
    names = {r["name"] for r in outcome["stages"]["S2_search"]}
    if names != set(searched):
        raise ValueError("Search trials differ from the frozen enumeration")
    return {"feature_selection": "equal", "parameter_selection": "equal"}


def station_compare(frame: Any, baseline: Any) -> dict[str, Any]:
    """Per-station MAE against Seasonal Naive on identical rows."""

    def errors(data: Any) -> Any:
        return (
            (data["predicted"] - data["actual"])
            .abs()
            .groupby(data["station_id"])
            .mean()
        )

    difference = (errors(frame) - errors(baseline)).sort_values()
    return {
        "stations": len(difference),
        "better": int((difference < 0).sum()),
        "worse": int((difference > 0).sum()),
        "largest_gain": {k: round(float(v), 4) for k, v in difference.head(5).items()},
        "largest_loss": {k: round(float(v), 4) for k, v in difference.tail(5).items()},
    }


def write_details(stage: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    write_json(stage / "evidence.json", evidence)
    return {"kind": "decision_audit", "outcome_id": evidence["outcome_id"]}


def dashboard_part(analytics: dict[str, Any], scores: list[Any]) -> dict[str, Any]:
    from citibike.decision_research.analytics import dashboard

    return dashboard(
        analytics["sections"], [s for s in scores if s["stage"] == "S3_holdout"]
    )


def degraded_by_fold(
    gates: dict[tuple[str, str, str], dict[str, Any]],
) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for (population, _, _), item in gates.items():
        bucket = table.setdefault(population, {})
        for segment in item["degraded_segments"]:
            fold = str(segment).split(":")[0]
            bucket[fold] = bucket.get(fold, 0) + 1
    return table


def seed_stability(outcome: dict[str, Any], scores: list[dict[str, Any]]) -> list[Any]:
    base = {
        (s["target"], s["variant"]): s["macro_mae"]
        for s in scores
        if s["stage"] == "S3_holdout" and s["population"] == "cohort64"
    }
    table: dict[tuple[str, str], list[float]] = {}
    for row in scores:
        if row["stage"] == "S5_seeds":
            name = row["variant"].split("__")[0]
            table.setdefault((row["target"], name), []).append(row["macro_mae"])
    return [
        {
            "target": kind,
            "variant": name,
            "seed42": base.get((kind, name)),
            "other_seeds": values,
            "range": max([*values, base[(kind, name)]])
            - min([*values, base[(kind, name)]])
            if (kind, name) in base
            else None,
        }
        for (kind, name), values in sorted(table.items())
    ]


def ablations(
    outcome: dict[str, Any],
    plan: dict[str, Any],
    frames: dict[tuple[str, str, str], Any],
    uncertainty: dict[str, Any],
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in outcome["stages"]["S4_ablation"]:
        if row["status"] == "success":
            grouped.setdefault((row["prediction_type"], row["name"]), []).append(row)
    result = []
    for (kind, name), rows in sorted(grouped.items()):
        reference = ("cohort64", kind, name.split("__")[0])
        if reference not in frames:
            continue
        result.append(
            {
                "target": kind,
                "variant": name,
                "reference": reference[2],
                **bootstrap(
                    frames_of(rows),
                    frames[reference],
                    replicates=uncertainty["replicates_secondary"],
                ),
            }
        )
    return result
