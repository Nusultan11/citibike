"""Source contribution SQL and additive, event-month Gold rollups."""

from pathlib import Path

import duckdb

from citibike.analytics.contracts import CORE, MARTS, MEASURES, category_sql


def prepare_events(db: duckdb.DuckDBPyConnection) -> None:
    db.execute(f"""CREATE VIEW typed AS SELECT *,
        {category_sql("rideable_type", "vehicle")} vehicle,
        {category_sql("member_casual", "membership")} membership,
        date_diff('microsecond',started_at_utc,ended_at_utc)::BIGINT du
        FROM silver""")
    if db.execute("SELECT count(*) FROM typed WHERE du<0 OR du IS NULL").fetchone() != (
        0,
    ):
        raise ValueError("F3 requires valid canonical durations")
    db.execute("""CREATE TABLE events AS
        SELECT date_trunc('hour',started_at_utc) hour_utc,
        started_at_local::DATE local_date,'start' event_type,
        start_station_id station_id,start_lat lat,start_lng lon,vehicle,membership,
        1::BIGINT n,du duration_us,1::BIGINT duration_n,
        (start_station_id IS NOT NULL)::BIGINT departures,0::BIGINT arrivals,
        0::BIGINT arrival_rows,
        (start_station_id IS NOT NULL AND end_station_id IS NOT NULL)::BIGINT route_n,
        coalesce(start_lat BETWEEN -90 AND 90 AND start_lng BETWEEN -180 AND 180
            AND isfinite(start_lat) AND isfinite(start_lng),false)::BIGINT geo_n
        FROM typed
        UNION ALL
        SELECT date_trunc('hour',ended_at_utc),ended_at_local::DATE,'end',
        end_station_id,end_lat,end_lng,vehicle,membership,
        0,0,0,0,(end_station_id IS NOT NULL)::BIGINT,1,0,0 FROM typed""")


def aggregate(
    db: duckdb.DuckDBPyConnection,
    name: str,
    relation: str,
    projections: dict[str, str] | None = None,
    where: str = "true",
) -> None:
    mart = MARTS[name]
    columns = projections or {key: key for key in mart.keys}
    select = ",".join(f"{expression} AS {key}" for key, expression in columns.items())
    sums = ",".join(f"sum({measure})::BIGINT AS {measure}" for measure in mart.measures)
    db.execute(
        f"CREATE TABLE {name} AS SELECT {select},{sums} "
        f"FROM {relation} WHERE {where} GROUP BY ALL"
    )


def contribution_tables(db: duckdb.DuckDBPyConnection) -> None:
    prepare_events(db)
    aggregate(db, "trip_hourly", "events")
    aggregate(db, "station_category_hourly", "events", where="station_id IS NOT NULL")
    db.execute("""CREATE TABLE route_category_daily AS
        SELECT started_at_local::DATE local_date,start_station_id from_station,
        end_station_id to_station,vehicle,membership,count(*)::BIGINT n,
        sum(du)::BIGINT duration_us,count(*)::BIGINT duration_n FROM typed
        WHERE start_station_id IS NOT NULL AND end_station_id IS NOT NULL
        GROUP BY ALL""")
    db.execute("""CREATE TABLE geo_category_daily AS
        SELECT local_date,event_type,
        least(floor(cast(lat AS DECIMAL(12,8))*100),8999)::INTEGER cell_lat,
        least(floor(cast(lon AS DECIMAL(12,8))*100),17999)::INTEGER cell_lon,
        coalesce(station_id,'') station_key,vehicle,membership,
        count(*)::BIGINT n FROM events
        WHERE lat IS NOT NULL AND lon IS NOT NULL AND isfinite(lat) AND isfinite(lon)
            AND lat BETWEEN -90 AND 90 AND lon BETWEEN -180 AND 180 GROUP BY ALL""")


def month_tables(db: duckdb.DuckDBPyConnection, month: str) -> None:
    for name in CORE:
        time_key = MARTS[name].time_key
        local = (
            f"timezone('America/New_York',{time_key})::DATE"
            if time_key == "hour_utc"
            else time_key
        )
        aggregate(
            db, name, f"input_{name}", where=f"strftime({local},'%Y-%m')='{month}'"
        )
    for prefix, source in [
        ("trip", "trip_hourly"),
        ("station_category", "station_category_hourly"),
    ]:
        for grain in ["daily", "monthly"]:
            name = f"{prefix}_{grain}"
            date_key = "local_date" if grain == "daily" else "local_month"
            expression = "timezone('America/New_York',hour_utc)::DATE"
            if grain == "monthly":
                expression = f"date_trunc('month',{expression})::DATE"
            projections = {
                key: (expression if key == date_key else key)
                for key in MARTS[name].keys
            }
            aggregate(db, name, source, projections)
    aggregate(db, "fact_station_hourly", "station_category_hourly")
    aggregate(db, "fact_station_daily", "station_category_daily")
    aggregate(db, "fact_route_daily", "route_category_daily")
    for name, source in [
        ("route_category_monthly", "route_category_daily"),
        ("route_monthly", "fact_route_daily"),
    ]:
        projection = {
            key: (
                "date_trunc('month',local_date)::DATE" if key == "local_month" else key
            )
            for key in MARTS[name].keys
        }
        aggregate(db, name, source, projection)


def write_tables(
    db: duckdb.DuckDBPyConnection, target: Path, names: tuple[str, ...] | list[str]
) -> dict[str, int]:
    rows = {}
    for name in names:
        mart = MARTS[name]
        keys = ",".join(mart.keys)
        if db.execute(
            f"SELECT count(*) FROM (SELECT {keys} FROM {name} "
            "GROUP BY ALL HAVING count(*)<>1)"
        ).fetchone() != (0,):
            raise ValueError(f"Gold grain violated: {name}")
        invalid = " OR ".join(f"{m}<0 OR {m} IS NULL" for m in mart.measures)
        if db.execute(f"SELECT count(*) FROM {name} WHERE {invalid}").fetchone() != (
            0,
        ):
            raise ValueError(f"Gold invalid additive measures: {name}")
        counted = db.execute(f"SELECT count(*) FROM {name}").fetchone()
        assert counted is not None
        rows[name] = counted[0]
        db.execute(
            f"COPY (SELECT * FROM {name} ORDER BY {keys}) TO ? "
            "(FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 32768)",
            [str(target / f"{name}.parquet")],
        )
    return rows


def scalar_metrics(db: duckdb.DuckDBPyConnection) -> dict[str, int]:
    row = db.execute(
        "SELECT "
        + ",".join(f"coalesce(sum({key}),0)::BIGINT {key}" for key in MEASURES)
        + " FROM trip_hourly"
    ).fetchone()
    assert row is not None
    return dict(zip(MEASURES, row, strict=True))
