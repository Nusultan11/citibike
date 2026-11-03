"""Observed labels, source coverage and an interpretable frozen-window profile."""

from pathlib import Path
from typing import Any

import duckdb

from citibike.features.contracts import utc
from citibike.forecasting.artifacts import checked
from citibike.forecasting.metrics import metrics
from citibike.ml_diagnostics.protocol import boundary
from citibike.storage import read_json

SEASON = "((month(timezone('America/New_York',hour_utc))%12)//3)::INT"


def connection() -> duckdb.DuckDBPyConnection:
    db = duckdb.connect()
    db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='1GB'")
    return db


def records(db: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    cur = db.execute(sql)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, row, strict=True)) for row in cur.fetchall()]


def tables(db: duckdb.DuckDBPyConnection, plan: dict[str, Any]) -> None:
    dataset = Path(plan["dataset"])
    data = checked(dataset)
    for name in ("dense", "examples", "stations"):
        db.read_parquet(str(dataset / f"{name}.parquet")).create_view(name)
    db.execute("CREATE TABLE excluded(station_id VARCHAR)")
    if plan["excluded_ids"]:
        db.executemany(
            "INSERT INTO excluded VALUES (?)", [(s,) for s in plan["excluded_ids"]]
        )
    db.execute(
        """CREATE TABLE labels AS SELECT station_id,hour_utc,month_key,
        label_available_at,'departures' prediction_type,departures actual FROM dense
        WHERE hour_utc<? UNION ALL SELECT station_id,hour_utc,month_key,
        label_available_at,'arrivals',arrivals FROM dense WHERE hour_utc<?""",
        [boundary(data["config"]["final"][0])] * 2,
    )


def coverage(db: duckdb.DuckDBPyConnection, plan: dict[str, Any]) -> dict[str, Any]:
    grouped = """SELECT prediction_type,{grouping},count(*) station_hours,
        count(actual) observed,count(*)-count(actual) missing,
        count(*) FILTER(WHERE actual=0) observed_zero,
        sum(actual) published_events,
        count(*) FILTER(WHERE station_id IN(SELECT * FROM excluded)) excluded_hours,
        count(actual) FILTER(WHERE station_id IN(SELECT * FROM excluded))
            excluded_observed,
        coalesce(sum(actual) FILTER(WHERE station_id IN(SELECT * FROM excluded)),0)
            excluded_events FROM labels GROUP BY ALL ORDER BY ALL"""
    gold = read_json(
        Path(checked(Path(plan["dataset"]))["gold_path"]) / "manifest.json"
    )
    db.read_parquet(
        [str(Path(m["path"]) / "trip_hourly.parquet") for m in gold["months"]]
    ).create_view("city_input")
    end = boundary(checked(Path(plan["dataset"]))["config"]["final"][0])
    db.execute(
        "CREATE TABLE city AS SELECT hour_utc,sum(n) dep,sum(arrival_rows) arr "
        "FROM city_input WHERE hour_utc<? GROUP BY 1",
        [end],
    )
    gaps = records(
        db,
        """SELECT 'departures' prediction_type,count(*) city_gap_hours FROM
        (SELECT DISTINCT hour_utc FROM labels) h LEFT JOIN city USING(hour_utc)
        WHERE coalesce(dep,0)=0 UNION ALL SELECT 'arrivals',count(*) FROM
        (SELECT DISTINCT hour_utc FROM labels) h LEFT JOIN city USING(hour_utc)
        WHERE coalesce(arr,0)=0""",
    )
    invalid = db.execute("""SELECT count(*) FROM labels l
        LEFT JOIN city c USING(hour_utc)
        WHERE actual IS NOT NULL AND (CASE WHEN prediction_type='departures'
        THEN coalesce(dep,0) ELSE coalesce(arr,0) END)=0""").fetchone()
    duplicates = db.execute("""SELECT count(*) FROM (SELECT station_id,hour_utc,
        prediction_type FROM labels GROUP BY ALL HAVING count(*)<>1)""").fetchone()
    if invalid != (0,) or duplicates != (0,):
        raise ValueError("Source coverage / station-hour grain failed")
    summary = records(db, grouped.format(grouping="'all' segment"))
    for row in summary:
        row["excluded_observed_share"] = (
            row["excluded_observed"] / row["observed"] if row["observed"] else None
        )
        row["excluded_event_share"] = (
            row["excluded_events"] / row["published_events"]
            if row["published_events"]
            else None
        )
    return {
        "summary": summary,
        "by_month": records(db, grouped.format(grouping="month_key")),
        "by_station": records(db, grouped.format(grouping="station_id")),
        "city_source_gaps": gaps,
        "observed_in_city_gap": 0,
        "grain_duplicates": 0,
        "excluded_ids": plan["excluded_ids"],
        "scope_end_exclusive": end.isoformat(),
        "semantics": "Observed zero requires city source-hour and +/-7d same-month "
        "station event brackets; missing and inactive/unknown stay distinct. "
        "Station operating state is UNKNOWN even at observed zero.",
    }


def calculate(db: duckdb.DuckDBPyConnection, plan: dict[str, Any]) -> Any:
    output = []
    for window in plan["windows"]:
        start, end = utc(window["start"]), utc(window["end"])
        db.execute(
            f"""CREATE OR REPLACE TABLE history AS SELECT station_id,prediction_type,
            isodow(timezone('America/New_York',hour_utc))-1 weekday,
            hour(timezone('America/New_York',hour_utc)) local_hour,{SEASON} season,
            count(actual) context_n,avg(actual) context_mean,
            var_pop(actual) context_var,
            max(hour_utc) context_max_event,
            count(actual) FILTER(WHERE label_available_at<=?) proxy_n,
            avg(actual) FILTER(WHERE label_available_at<=?) proxy_mean
            FROM labels WHERE hour_utc<? AND actual IS NOT NULL GROUP BY ALL""",
            [start, start, start],
        )
        db.execute(
            "CREATE OR REPLACE TABLE requests AS SELECT * FROM examples "
            "WHERE forecast_origin_utc>=? AND forecast_origin_utc<?",
            [start, end],
        )
        paths = [
            str(Path(t["path"]) / "predictions.parquet")
            for t in plan["trials"]
            if t["fold"] == window["fold"]
        ]
        db.read_parquet(paths).create_view("frozen", replace=True)
        db.execute(
            """CREATE OR REPLACE TABLE result AS SELECT r.*,
            p.predicted,p.predicted-r.actual sn_error,
            r.actual-h.context_mean context_residual,
            h.context_n,h.context_mean,h.context_var,h.context_max_event,
            h.proxy_n,h.proxy_mean,
            CASE WHEN r.station_id IN(SELECT * FROM excluded) THEN 'ambiguous_id'
                WHEN r.forecast_origin_utc<r.known_at THEN 'station_not_known'
                WHEN r.actual IS NULL THEN 'missing_observation'
                WHEN coalesce(h.context_n,0)<? THEN 'insufficient_history'
                ELSE 'observed' END input_quality,
            ?::TIMESTAMPTZ input_available_at,?::TIMESTAMPTZ analysis_as_of,
            ?::TIMESTAMPTZ history_cutoff,? fold,
            ((r.month%12)//3)::INT season
            FROM requests r LEFT JOIN history h ON r.station_id=h.station_id
                AND r.prediction_type=h.prediction_type AND r.weekday=h.weekday
                AND r.local_hour=h.local_hour AND ((r.month%12)//3)::INT=h.season
            LEFT JOIN frozen p ON r.station_id=p.station_id
                AND r.prediction_type=p.prediction_type
                AND r.forecast_origin_utc=p.forecast_origin_utc
                AND r.target_hour_utc=p.target_hour_utc AND r.horizon_h=p.horizon_h""",
            [
                plan["rules"]["min_cell_history"],
                plan["input_available_at"],
                plan["analysis_as_of"],
                start,
                window["fold"],
            ],
        )
        bad = db.execute("""SELECT count(*) FROM result WHERE
            (eligibility='eligible'
                AND (predicted IS NULL OR predicted<>seasonal_naive))
            OR (context_max_event>=history_cutoff)
            OR input_available_at>analysis_as_of""").fetchone()
        if bad != (0,):
            raise ValueError("Frozen SN / strict analysis availability violation")
        output.append(
            db.execute(
                "SELECT * FROM result ORDER BY "
                "station_id,prediction_type,target_hour_utc"
            ).df()
        )
    import importlib

    pd = importlib.import_module("pandas")
    frame = pd.concat(output, ignore_index=True)
    frame["row_key"] = frame.apply(
        lambda r: "|".join(
            [
                r["fold"],
                r["station_id"],
                r["prediction_type"],
                r["target_hour_utc"].isoformat(),
            ]
        ),
        axis=1,
    )
    return frame


def summary(frame: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    # Use the established forecasting definitions. These are diagnostic paired
    # scores, not a model competition; unsupported rows remain in coverage.
    result: dict[str, Any] = {
        "quality": {},
        "segments": [],
        "paired": [],
        "sn_segments": [],
    }
    frame = frame.copy()
    import importlib

    pd = importlib.import_module("pandas")
    frame["actual_band"] = frame["actual"].apply(
        lambda y: (
            "missing"
            if pd.isna(y)
            else "zero"
            if y == 0
            else "1-4"
            if y < 5
            else "5-19"
            if y < 20
            else "20+"
        )
    )
    keys = {key for event in events for key in event["evidence_keys"]}
    frame["contextual_event"] = frame["row_key"].isin(keys)
    forecast_rows = frame[
        frame["predicted"].notna()
        & ~frame["input_quality"].isin(
            ["ambiguous_id", "station_not_known", "missing_observation"]
        )
    ]
    for field in (
        "fold",
        "horizon_h",
        "local_hour",
        "season",
        "activity",
        "actual_band",
        "new_known",
        "input_quality",
        "contextual_event",
    ):
        for (kind, key), group in forecast_rows.groupby(
            ["prediction_type", field], sort=True
        ):
            result["sn_segments"].append(
                {
                    "target": kind,
                    "dimension": field,
                    "key": str(key),
                    "metrics": metrics(
                        group["actual"].astype(float).tolist(),
                        group["predicted"].tolist(),
                    ),
                }
            )
    result["largest_model_errors"] = (
        forecast_rows.assign(absolute=forecast_rows["sn_error"].abs())
        .sort_values(["absolute", "row_key"], ascending=[False, True])
        .head(10)[
            [
                "row_key",
                "station_id",
                "fold",
                "prediction_type",
                "forecast_origin_utc",
                "target_hour_utc",
                "horizon_h",
                "actual",
                "predicted",
                "sn_error",
                "input_quality",
            ]
        ]
        .astype(str)
        .to_dict("records")
    )
    for (fold, kind, quality), group in frame.groupby(
        ["fold", "prediction_type", "input_quality"], sort=True
    ):
        result["quality"][f"{fold}/{kind}/{quality}"] = len(group)
    valid = frame[(frame["input_quality"] == "observed") & frame["predicted"].notna()]
    for field in (
        "fold",
        "horizon_h",
        "station_id",
        "local_hour",
        "season",
        "activity",
        "actual_band",
        "new_known",
        "contextual_event",
    ):
        for (kind, key), group in valid.groupby(["prediction_type", field], sort=True):
            actual = group["actual"].astype(float).tolist()
            result["segments"].append(
                {
                    "target": kind,
                    "dimension": field,
                    "key": str(key),
                    "seasonal_naive": metrics(actual, group["predicted"].tolist()),
                    "context": metrics(actual, group["context_mean"].tolist()),
                }
            )
    for (fold, kind), group in frame.groupby(["fold", "prediction_type"], sort=True):
        eligible = group[
            (group["predicted"].notna())
            & ~group["input_quality"].isin(
                ["ambiguous_id", "station_not_known", "missing_observation"]
            )
        ]
        supported = eligible[eligible["input_quality"] == "observed"]
        delta = (
            float(
                (
                    (supported["context_mean"] - supported["actual"]).abs()
                    - (supported["predicted"] - supported["actual"]).abs()
                ).mean()
            )
            if len(supported)
            else None
        )
        result["paired"].append(
            {
                "fold": fold,
                "target": kind,
                "eligible": len(eligible),
                "supported": len(supported),
                "coverage": len(supported) / len(eligible) if len(eligible) else None,
                "mae_delta_context_minus_sn": delta,
            }
        )
    result["decision"] = "inconclusive"
    if all(
        sum(
            1
            for r in result["paired"]
            if r["target"] == kind
            and r["coverage"] is not None
            and r["coverage"] >= 0.5
            and r["mae_delta_context_minus_sn"] < 0
        )
        >= 3
        for kind in ("departures", "arrivals")
    ):
        result["decision"] = (
            "ONE future seasonal-profile feature ablation accepted for research; "
            "no fit/promotion"
        )
    return result
