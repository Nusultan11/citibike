"""Experimental A/B features, outside the frozen feature-code hash namespace."""

import math
from pathlib import Path
from typing import Any

import duckdb

from citibike.features.contracts import NY, utc
from citibike.storage import identity

VERSION = "f4-feature-research-2"
A = {
    **{
        f"{field}_{wave}": f"{wave}(2*pi*{field}/{period})"
        for field, period in (("local_hour", 24), ("weekday", 7), ("month_index", 12))
        for wave in ("sin", "cos")
    },
    "week_of_year": "ISO week of NY target date (1..53)",
    "weekday_am_peak": "weekday<5 and local_hour in [7,8,9]",
    "weekday_pm_peak": "weekday<5 and local_hour in [16,17,18,19]",
    "holiday_peak": "holiday * (weekday_am_peak + weekday_pm_peak)",
}
B = {
    "published_month_mean": "latest available month sum(observed actual)/n_observed",
    "prior_month_mean": "previous observed published month mean, not missing=zero",
    "monthly_trend": "(latest_mean-prior_mean)/calendar_month_gap",
    "seasonal_month_mean": "expanding sum(actual)/n_observed for NY target month",
    "seasonal_month_n": "n_observed for seasonal_month_mean; NULL if no history",
    "published_month_n": "n_observed in latest published month",
}
DERIVED = ("research_cutoff", "b_available_at", "b_max_event_at", "b_latest_month")
CATALOG = {
    name: {
        "group": group,
        "formula": formula,
        "dtype": "float64" if group == "A" else "nullable float64",
        "lineage": "target calendar / us-federal-observed-1"
        if group == "A"
        else "dataset dense.parquet observed labels; month_availability dependencies",
        "available_at": "known at origin"
        if group == "A"
        else "max dependency label_available_at <= min(origin,fold fit_as_of)",
        "max_availability_lag": "0; known calendar"
        if group == "A"
        else "UNKNOWN historically; hypothesis +45d after NY month end",
        "source_dependencies": ["calendar"]
        if group == "A"
        else ["verified Gold", "Silver source months", "approximate +45d policy"],
        "parity": "same enrich() at fit and saved-model evaluation; research only",
        "missing": "not applicable"
        if group == "A"
        else "retain NULL; trees native NaN; Poisson train median + missing indicator",
    }
    for group, fields in (("A", A), ("B", B))
    for name, formula in fields.items()
}
SCHEMA = identity([VERSION, CATALOG])


def calendar(frame: Any) -> Any:
    result = frame.copy()
    local = result["target_hour_utc"].dt.tz_convert(NY)
    fields = {
        "local_hour": (local.dt.hour, 24),
        "weekday": (local.dt.weekday, 7),
        "month_index": (local.dt.month - 1, 12),
    }
    for field, (values, period) in fields.items():
        for wave, function in (("sin", math.sin), ("cos", math.cos)):
            result[f"{field}_{wave}"] = values.map(
                lambda value, fn=function, p=period: fn(2 * math.pi * value / p)
            ).astype(float)
    result["week_of_year"] = local.dt.isocalendar().week.astype(float)
    result["weekday_am_peak"] = (
        (local.dt.weekday < 5) & local.dt.hour.isin([7, 8, 9])
    ).astype(float)
    result["weekday_pm_peak"] = (
        (local.dt.weekday < 5) & local.dt.hour.isin([16, 17, 18, 19])
    ).astype(float)
    result["holiday_peak"] = result["holiday"] * (
        result["weekday_am_peak"] + result["weekday_pm_peak"]
    )
    return result


def enrich(frame: Any, dense: Path, fit_asof: str) -> Any:
    """Only published observations; validation B state is capped at fold fit time."""
    frame = calendar(frame)
    frame["research_cutoff"] = frame["forecast_origin_utc"].clip(upper=utc(fit_asof))
    with duckdb.connect() as db:
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='512MB'")
        db.register("frame", frame)
        db.read_parquet(str(dense)).create_view("dense")
        db.execute("""CREATE TABLE monthly AS WITH labels AS (
            SELECT station_id,hour_utc,month_key,label_available_at,
                'departures' prediction_type,departures actual FROM dense
            UNION ALL SELECT station_id,hour_utc,month_key,label_available_at,
                'arrivals',arrivals FROM dense)
            SELECT station_id,prediction_type,month_key,
                max(label_available_at) available_at,max(hour_utc) event_at,
                sum(actual) total,count(actual) n,sum(actual)/count(actual) mean,
                month(min(timezone('America/New_York',hour_utc))) event_month,
                min(timezone('America/New_York',hour_utc))::DATE first_date
            FROM labels WHERE actual IS NOT NULL GROUP BY 1,2,3""")
        # A missing month is skipped, never treated as a zero-demand month.
        db.execute("""CREATE TABLE states AS SELECT *,
            lag(mean) OVER w previous_mean,
            date_diff('month',lag(first_date) OVER w,first_date) month_gap,
            max(available_at) OVER (PARTITION BY station_id,prediction_type
                ORDER BY month_key ROWS UNBOUNDED PRECEDING) state_available_at
            FROM monthly WINDOW w AS(PARTITION BY station_id,prediction_type
                ORDER BY month_key)""")
        db.execute("""CREATE TABLE seasonal AS SELECT *,
            sum(total) OVER w/sum(n) OVER w seasonal_mean,sum(n) OVER w seasonal_n,
            max(available_at) OVER w seasonal_available_at,
            max(event_at) OVER w seasonal_event_at
            FROM monthly WINDOW w AS(PARTITION BY station_id,prediction_type,
                event_month ORDER BY month_key ROWS UNBOUNDED PRECEDING)""")
        result = db.execute("""SELECT f.*,m.mean published_month_mean,
            m.previous_mean prior_month_mean,
            (m.mean-m.previous_mean)/nullif(m.month_gap,0) monthly_trend,
            s.seasonal_mean seasonal_month_mean,s.seasonal_n seasonal_month_n,
            m.n published_month_n,
            greatest(m.state_available_at,s.seasonal_available_at) b_available_at,
            greatest(m.event_at,s.seasonal_event_at) b_max_event_at,
            m.month_key b_latest_month
            FROM frame f ASOF LEFT JOIN (
                SELECT * FROM states QUALIFY row_number() OVER(
                    PARTITION BY station_id,prediction_type,state_available_at
                    ORDER BY month_key DESC)=1) m ON f.station_id=m.station_id
                AND f.prediction_type=m.prediction_type
                AND f.research_cutoff>=m.state_available_at
            ASOF LEFT JOIN seasonal s ON f.station_id=s.station_id
                AND f.prediction_type=s.prediction_type AND f.month=s.event_month
                AND f.research_cutoff>=s.seasonal_available_at
            ORDER BY prediction_type,station_id,forecast_origin_utc,horizon_h""").df()
    for name in B:
        result[name] = result[name].astype(float)
    gate(result, fit_asof)
    return result


def gate(frame: Any, fit_asof: str) -> None:
    if bool(
        (
            (frame["feature_available_at"] > frame["forecast_origin_utc"])
            | (frame["max_event_at"] >= frame["forecast_origin_utc"])
            | (frame["b_available_at"] > frame["forecast_origin_utc"])
            | (frame["b_available_at"] > utc(fit_asof))
            | (frame["b_max_event_at"] >= frame["forecast_origin_utc"])
            | (frame["b_max_event_at"] >= utc(fit_asof))
        ).any()
    ):
        raise ValueError("Research PIT gate failed")
