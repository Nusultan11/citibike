"""Versioned research features and isolated preprocessing; never used in serving."""

import importlib
import math
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from citibike.features.contracts import NY, holiday, utc
from citibike.features.research import A, calendar
from citibike.storage import identity

VERSION = "f4-decision-research-2"
# Schema of protocol 31be44f7: its cell_std was 0 instead of NULL on empty history
# (defect D1). Models trained without group P are unaffected and stay readable.
D1_SCHEMA = "11c8ec6a17cd18cc393f94da148a18476e3a47c227ebd3f2b267ab872fe21fa8"
SHRINK_LABELS = 8
HOLIDAY_WINDOW = 7
CLOSURE_MIN_HOURS = 24
CLOSURE_EXPECTED_EVENTS = -math.log(1e-6)
THIN_HISTORY_LABELS = 720
JOIN_ROWS = 160_000
CANONICAL_ID = re.compile(r"^(\d{4}\.\d{1,2}|[A-Z]{2}\d{3})$")
C = {
    **{
        f"doy_{wave}{k}": f"{wave}(2*pi*{k}*NY day_of_year/365.25)"
        for k in (1, 2)
        for wave in ("sin", "cos")
    },
    "days_to_holiday": f"days to next federal holiday, capped at {HOLIDAY_WINDOW}",
    "days_from_holiday": f"days since last federal holiday, capped at {HOLIDAY_WINDOW}",
}
P = {
    "hist_mean": "sum(actual)/n over published station/type labels",
    "hist_zero_share": "zero labels/n over published station/type labels",
    "hist_cv": "population std/mean over published station/type labels; NULL if mean=0",
    "hist_n": "n observed published station/type labels",
    "cell_std": "population std over published station/type/weekday/hour labels; "
    "NULL without cell history (DuckDB greatest() ignores NULL, so it is guarded)",
    "cell_zero_share": "zero labels/n over published station/type/weekday/hour",
    "cell_n": "n observed published station/type/weekday/hour labels",
    "shape_share": "cell mean/hist_mean; scale-free hour-of-week demand shape",
    "hist_band": "train-only quartile band of hist_mean, per type; thresholds at fit",
}
S = {
    "pool_cell_mean": "cohort sum(actual)/n for type/weekday/hour, published labels",
    "shrunk_cell_mean": f"(cell_n*cell_mean+{SHRINK_LABELS}*scaled_pool)/"
    f"(cell_n+{SHRINK_LABELS}); scaled_pool=pool_cell_mean*hist_mean/pool_hist_mean",
    "season_index": "cohort mean of target NY month one year earlier / cohort mean "
    "of latest published month; NULL if that month is not yet published",
    "adjusted_profile": "shrunk_cell_mean*season_index; NULL if season_index is NULL",
}
GROUPS: dict[str, dict[str, str]] = {"A": A, "C": C, "P": P, "S": S}
NUMERIC_GROUPS = ("P", "S")
DERIVED = (
    "research_cutoff",
    "d_available_at",
    "d_max_event_at",
    "anomalous_id",
    "suspect_closure",
    "history_depth",
)
CATALOG = {
    name: {
        "group": group,
        "formula": formula,
        "dtype": "float64" if group in ("A", "C") else "nullable float64",
        "lineage": "target calendar / us-federal-observed-1"
        if group in ("A", "C")
        else "dataset dense.parquet observed labels of this fold population",
        "availability": "known at origin"
        if group in ("A", "C")
        else "label_available_at <= min(origin, fold fit_as_of)",
        "max_availability_lag": "0; known calendar"
        if group in ("A", "C")
        else "UNKNOWN historically; hypothesis +45d after NY month end",
        "null_semantics": "not applicable"
        if group in ("A", "C")
        else "NULL means no published history; never zero; trees native NaN; "
        "Poisson train median plus missing indicator",
        "pit_check": "independent calendar recomputation"
        if group in ("A", "C")
        else "independent per-cutoff summation in verify; gate at build and worker",
    }
    for group, fields in GROUPS.items()
    for name, formula in fields.items()
}
SCHEMA = identity([VERSION, CATALOG, SHRINK_LABELS, CLOSURE_EXPECTED_EVENTS])
RULES = {
    "anomalous_id": {
        "problem": "Non-canonical IDs (`Shop Morgan`, `5308.04_`, `5348.06_old`, "
        "`1234.56`) may be service or transitional stations, not public docks",
        "rule": "ID outside ^dddd.d{1,2}$ and ^[A-Z]{2}ddd$, or equal to 1234.56, is "
        "excluded from training under preprocess=true; always retained in evaluation "
        "and flagged. `6242.1` (lost trailing zero) and `JC032` stay canonical",
        "test": "test_preprocessing_flags_and_train_only_filter",
    },
    "suspect_closure": {
        "problem": "A 7-day label bracket can label a closed station as true zeros",
        "rule": f"Observed zero run of >={CLOSURE_MIN_HOURS}h with same-month station "
        f"rate*hours>={CLOSURE_EXPECTED_EVENTS:.4f} (Poisson P<=1e-6) is excluded "
        "from training under preprocess=true; retained in evaluation and flagged",
        "test": "test_preprocessing_flags_and_train_only_filter",
    },
    "peaks_retained": {
        "problem": "Extreme counts dominate squared loss and tail MAE",
        "rule": "No clipping or winsorising: DATA_CONTRACTS forbids removing peaks",
        "test": "test_preprocessing_retains_extreme_values",
    },
    "missing_retained": {
        "problem": "Missing history can masquerade as zero demand",
        "rule": "NULL stays NULL in features; imputation only inside Poisson pipeline",
        "test": "test_missing_history_never_becomes_zero",
    },
}


def anomalous(station: str) -> bool:
    return station == "1234.56" or CANONICAL_ID.match(station) is None


def holiday_distance(day: date) -> tuple[int, int]:
    following = next(
        (n for n in range(HOLIDAY_WINDOW) if holiday(day + timedelta(days=n))),
        HOLIDAY_WINDOW,
    )
    previous = next(
        (n for n in range(HOLIDAY_WINDOW) if holiday(day - timedelta(days=n))),
        HOLIDAY_WINDOW,
    )
    return following, previous


def cyclic(frame: Any) -> Any:
    """Bounded annual harmonics; no monotone week index a tree can run off."""
    result = frame.copy()
    local = result["target_hour_utc"].dt.tz_convert(NY)
    day = local.dt.dayofyear.astype(float)
    for k in (1, 2):
        angle = 2 * math.pi * k * day / 365.25
        result[f"doy_sin{k}"] = angle.map(math.sin).astype(float)
        result[f"doy_cos{k}"] = angle.map(math.cos).astype(float)
    days = local.dt.date
    distances = {d: holiday_distance(d) for d in days.unique()}
    result["days_to_holiday"] = days.map(lambda d: distances[d][0]).astype(float)
    result["days_from_holiday"] = days.map(lambda d: distances[d][1]).astype(float)
    return result


def states(db: duckdb.DuckDBPyConnection) -> None:
    """Cumulative published state per month; a skipped month stays skipped."""
    db.execute("""CREATE TABLE labels AS WITH long AS (
        SELECT station_id,hour_utc,month_key,label_available_at,
            'departures' prediction_type,departures actual FROM dense
        UNION ALL SELECT station_id,hour_utc,month_key,label_available_at,
            'arrivals',arrivals FROM dense)
        SELECT *,isodow(timezone('America/New_York',hour_utc))-1 weekday,
            hour(timezone('America/New_York',hour_utc)) local_hour
        FROM long WHERE actual IS NOT NULL""")
    measures = """count(*) n,sum(actual) total,sum(actual*actual) squares,
        count(*) FILTER(WHERE actual=0) zeros,max(label_available_at) available_at,
        max(hour_utc) event_at"""
    cumulative = """sum(n) OVER w c_n,sum(total) OVER w c_total,
        sum(squares) OVER w c_squares,sum(zeros) OVER w c_zeros,
        max(available_at) OVER w state_at,max(event_at) OVER w state_event"""
    for name, keys in (
        ("station", "station_id,prediction_type"),
        ("cell", "station_id,prediction_type,weekday,local_hour"),
        ("pool_cell", "prediction_type,weekday,local_hour"),
        ("pool", "prediction_type"),
    ):
        db.execute(
            f"""CREATE TABLE {name}_state AS WITH monthly AS (
            SELECT {keys},month_key,{measures} FROM labels GROUP BY ALL),
            running AS (SELECT *,{cumulative} FROM monthly
                WINDOW w AS(PARTITION BY {keys}
                    ORDER BY month_key ROWS UNBOUNDED PRECEDING))
            SELECT * FROM running QUALIFY row_number() OVER(
                PARTITION BY {keys},state_at ORDER BY month_key DESC)=1"""
        )
    db.execute(f"""CREATE TABLE pool_months AS SELECT prediction_type,month_key,
        {measures} FROM labels GROUP BY ALL""")


def quality_flags(db: duckdb.DuckDBPyConnection) -> None:
    """Zero runs stay inside one NY month, so they share the label availability."""
    db.execute(f"""CREATE TABLE flags AS WITH hourly AS (
        SELECT station_id,hour_utc,month_key,
            (departures IS NOT NULL OR arrivals IS NOT NULL) observed,
            coalesce(departures,0)+coalesce(arrivals,0) events FROM dense),
        rates AS (SELECT station_id,month_key,
            sum(events)/nullif(count(*) FILTER(WHERE observed),0) rate
            FROM hourly GROUP BY 1,2),
        islands AS (SELECT *,sum((events>0 OR NOT observed)::INT) OVER(
                PARTITION BY station_id,month_key ORDER BY hour_utc) island
            FROM hourly),
        runs AS (SELECT station_id,month_key,island,count(*) run_hours,
                min(hour_utc) first_hour,max(hour_utc) last_hour
            FROM islands WHERE observed AND events=0 GROUP BY 1,2,3)
        SELECT r.station_id,r.first_hour,r.last_hour,r.run_hours,x.rate
        FROM runs r JOIN rates x USING(station_id,month_key)
        WHERE r.run_hours>={CLOSURE_MIN_HOURS}
            AND r.run_hours*x.rate>={CLOSURE_EXPECTED_EVENTS}""")


JOINED = f"""SELECT f.* EXCLUDE(target_month_key),
            s.c_total/s.c_n hist_mean,s.c_zeros/s.c_n hist_zero_share,
            CASE WHEN s.c_total>0 THEN sqrt(greatest(s.c_squares/s.c_n
                -pow(s.c_total/s.c_n,2),0))/(s.c_total/s.c_n) END hist_cv,
            s.c_n hist_n,
            CASE WHEN c.c_n IS NOT NULL THEN sqrt(greatest(
                c.c_squares/c.c_n-pow(c.c_total/c.c_n,2),0)) END cell_std,
            c.c_zeros/c.c_n cell_zero_share,c.c_n cell_n,
            CASE WHEN s.c_total>0 THEN (c.c_total/c.c_n)/(s.c_total/s.c_n)
                END shape_share,
            p.c_total/p.c_n pool_cell_mean,
            (coalesce(c.c_total,0)+{SHRINK_LABELS}*(p.c_total/p.c_n)
                *coalesce((s.c_total/s.c_n)/nullif(l.c_total/l.c_n,0),1))
                /(coalesce(c.c_n,0)+{SHRINK_LABELS}) shrunk_cell_mean,
            y.total/y.n/nullif(l.total/l.n,0) season_index,
            greatest(s.state_at,c.state_at,p.state_at,l.state_at,y.available_at)
                d_available_at,
            greatest(s.state_event,c.state_event,p.state_event,l.state_event,
                y.event_at) d_max_event_at,
            g.station_id IS NOT NULL suspect_closure
            FROM frame f
            ASOF LEFT JOIN station_state s ON f.station_id=s.station_id
                AND f.prediction_type=s.prediction_type
                AND f.research_cutoff>=s.state_at
            ASOF LEFT JOIN cell_state c ON f.station_id=c.station_id
                AND f.prediction_type=c.prediction_type AND f.weekday=c.weekday
                AND f.local_hour=c.local_hour AND f.research_cutoff>=c.state_at
            ASOF LEFT JOIN pool_cell_state p ON f.prediction_type=p.prediction_type
                AND f.weekday=p.weekday AND f.local_hour=p.local_hour
                AND f.research_cutoff>=p.state_at
            ASOF LEFT JOIN pool_state l ON f.prediction_type=l.prediction_type
                AND f.research_cutoff>=l.state_at
            LEFT JOIN pool_months y ON f.prediction_type=y.prediction_type
                AND f.target_month_key=y.month_key
                AND y.available_at<=f.research_cutoff
            LEFT JOIN flags g ON g.station_id=f.station_id
                AND f.target_hour_utc BETWEEN g.first_hour AND g.last_hour
            ORDER BY f.prediction_type,f.station_id,f.forecast_origin_utc,
                f.horizon_h"""


def enrich(
    frame: Any,
    dense: Path,
    fit_asof: str,
    thresholds: dict[str, list[float]] | None = None,
) -> tuple[Any, dict[str, list[float]]]:
    """Published history capped at min(origin, fit time); row order is irrelevant."""
    np = importlib.import_module("numpy")
    pd = importlib.import_module("pandas")

    frame = cyclic(calendar(frame))
    frame["research_cutoff"] = frame["forecast_origin_utc"].clip(upper=utc(fit_asof))
    local = frame["target_hour_utc"].dt.tz_convert(NY)
    frame["target_month_key"] = (
        (local.dt.year - 1).astype(str) + "-" + local.dt.month.astype(str).str.zfill(2)
    )
    with duckdb.connect() as db:
        # Under pressure DuckDB dies natively here (0xC0000409), so labels get 3GB
        # and rows are joined in slices; a join result does not depend on the slice.
        db.execute("SET TimeZone='UTC'; SET threads=2; SET memory_limit='3GB'")
        db.read_parquet(str(dense)).create_view("dense")
        states(db)
        quality_flags(db)
        parts = []
        for start in range(0, len(frame), JOIN_ROWS):
            db.register("frame", frame.iloc[start : start + JOIN_ROWS])
            parts.append(db.execute(JOINED).df())
            db.unregister("frame")
    result = pd.concat(parts, ignore_index=True).sort_values(
        ["prediction_type", "station_id", "forecast_origin_utc", "horizon_h"],
        ignore_index=True,
        kind="stable",
    )
    for name in [*P, *S]:
        if name in result:
            result[name] = result[name].astype(float)
    result["adjusted_profile"] = result["shrunk_cell_mean"] * result["season_index"]
    if thresholds is None:
        thresholds = {
            kind: np.quantile(
                part.groupby("station_id")["hist_mean"].max().dropna(),
                [0.25, 0.5, 0.75],
            ).tolist()
            for kind, part in result.groupby("prediction_type", sort=True)
            if part["hist_mean"].notna().any()
        }
    result["hist_band"] = float("nan")
    for kind, limits in thresholds.items():
        mask = (result["prediction_type"] == kind) & result["hist_mean"].notna()
        result.loc[mask, "hist_band"] = np.searchsorted(
            limits, result.loc[mask, "hist_mean"], side="right"
        ).astype(float)
    result["anomalous_id"] = result["station_id"].map(anomalous).astype(bool)
    result["suspect_closure"] = result["suspect_closure"].astype(bool)
    result["history_depth"] = "none"
    result.loc[result["hist_n"].notna(), "history_depth"] = "thin"
    result.loc[result["hist_n"] >= THIN_HISTORY_LABELS, "history_depth"] = "deep"
    gate(result, fit_asof)
    return result, thresholds


def gate(frame: Any, fit_asof: str) -> None:
    limit = utc(fit_asof)
    if bool(
        (
            (frame["feature_available_at"] > frame["forecast_origin_utc"])
            | (frame["max_event_at"] >= frame["forecast_origin_utc"])
            | (frame["d_available_at"] > frame["research_cutoff"])
            | (frame["d_max_event_at"] >= frame["research_cutoff"])
            | (frame["research_cutoff"] > frame["forecast_origin_utc"])
            | (frame["research_cutoff"] > limit)
        ).any()
    ):
        raise ValueError("Decision research PIT gate failed")


def train_filter(frame: Any, *, preprocess: bool) -> Any:
    """Training-only exclusions; evaluation rows are never filtered."""
    if not preprocess:
        return frame
    return frame[~(frame["anomalous_id"] | frame["suspect_closure"])]
