"""Independent direct-Silver SQL oracle, not contribution/rollup helpers.

Starts and ends are grouped separately and FULL JOINed. NYC dates are calculated
from UTC, not copied from Silver local fields. Integer epoch differences provide
an independent duration calculation. Group EXCEPT ALL compares every fact key.
"""

import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import duckdb

from citibike.analytics.contracts import MARTS


def _time(column: str, key: str) -> str:
    if key == "hour_utc":
        return f"strftime({column},'%Y-%m-%dT%H:00:00Z')::TIMESTAMPTZ"
    local = f"timezone('America/New_York',{column})::DATE"
    return local if key == "local_date" else f"date_trunc('month',{local})::DATE"


def expected_query(name: str) -> str:
    mart = MARTS[name]
    vehicle = (
        "CASE rideable_type WHEN 'classic_bike' THEN 'classic_bike' "
        "WHEN 'electric_bike' THEN 'electric_bike' ELSE 'unknown' END"
    )
    member = (
        "CASE member_casual WHEN 'member' THEN 'member' "
        "WHEN 'casual' THEN 'casual' ELSE 'unknown' END"
    )
    duration = "(epoch_us(ended_at_utc)-epoch_us(started_at_utc))"
    start = "started_at_utc >= $begin AND started_at_utc < $finish"
    end = "ended_at_utc >= $begin AND ended_at_utc < $finish"
    geo_start = (
        "start_lat BETWEEN -90 AND 90 AND start_lng BETWEEN -180 AND 180 "
        "AND isfinite(start_lat) AND isfinite(start_lng)"
    )
    if "route" in name:
        keys = {
            mart.time_key: _time("started_at_utc", mart.time_key),
            "from_station": "start_station_id",
            "to_station": "end_station_id",
        }
        if "vehicle" in mart.keys:
            keys.update(vehicle=vehicle, membership=member)
        select = ",".join(f"{v} {k}" for k, v in keys.items())
        return (
            f"SELECT {select},count(*)::BIGINT n,sum({duration})::BIGINT "
            f"duration_us,count(*)::BIGINT duration_n FROM audit_input WHERE "
            f"{start} AND start_station_id IS NOT NULL AND end_station_id IS NOT NULL "
            "GROUP BY ALL"
        )
    if name == "geo_category_daily":
        pieces = []
        for side, condition in [("start", start), ("end", end)]:
            col = "started_at_utc" if side == "start" else "ended_at_utc"
            pieces.append(f"""SELECT {_time(col, "local_date")} local_date,
                '{side}' event_type,
                CASE WHEN CAST({side}_lat AS DECIMAL(12,8))=90 THEN 8999 ELSE
                    floor(CAST({side}_lat AS DECIMAL(12,8))*100)::INT END cell_lat,
                CASE WHEN CAST({side}_lng AS DECIMAL(12,8))=180 THEN 17999 ELSE
                    floor(CAST({side}_lng AS DECIMAL(12,8))*100)::INT END cell_lon,
                coalesce({side}_station_id,'') station_key,{vehicle} vehicle,
                {member} membership,count(*)::BIGINT n FROM audit_input
                WHERE {condition} AND {side}_lat BETWEEN -90 AND 90
                AND {side}_lng BETWEEN -180 AND 180
                AND isfinite({side}_lat) AND isfinite({side}_lng) GROUP BY ALL""")
        return " UNION ALL ".join(pieces)
    projections = []
    for side, column in [("start", "started_at_utc"), ("end", "ended_at_utc")]:
        keys = {mart.time_key: _time(column, mart.time_key)}
        station = "station_id" in mart.keys
        if station:
            keys["station_id"] = f"{side}_station_id"
        if "vehicle" in mart.keys:
            keys.update(vehicle=vehicle, membership=member)
        expressions = ",".join(f"{keys[k]} {k}" for k in mart.keys)
        if side == "start":
            expressions += f""",count(*)::BIGINT n,sum({duration})::BIGINT duration_us,
                count(*)::BIGINT duration_n,
                count(start_station_id)::BIGINT departures,
                count(*) FILTER(WHERE start_station_id IS NOT NULL AND
                    end_station_id IS NOT NULL)::BIGINT route_n,
                count(*) FILTER(WHERE {geo_start})::BIGINT geo_n"""
        else:
            expressions += (
                ",count(end_station_id)::BIGINT arrivals,count(*)::BIGINT arrival_rows"
            )
        where = start if side == "start" else end
        if station:
            where += f" AND {side}_station_id IS NOT NULL"
        projections.append(
            f"SELECT {expressions} FROM audit_input WHERE {where} GROUP BY ALL"
        )
    measures = ",".join(
        f"coalesce({'e' if key in ['arrivals', 'arrival_rows'] else 's'}."
        f"{key},0)::BIGINT {key}"
        for key in mart.measures
    )
    return (
        f"WITH s AS ({projections[0]}),e AS ({projections[1]}) SELECT "
        f"{','.join(mart.keys)},{measures} FROM s FULL JOIN e "
        f"USING({','.join(mart.keys)})"
    )


def verify_month(
    db: duckdb.DuckDBPyConnection, month: str, artifacts: dict[str, str]
) -> dict[str, int]:
    year, number = map(int, month.split("-"))
    zone = ZoneInfo("America/New_York")
    begin = datetime(year, number, 1, tzinfo=zone).astimezone(UTC)
    finish = datetime(
        year + (number == 12), number % 12 + 1, 1, tzinfo=zone
    ).astimezone(UTC)
    counts = {}
    for name, path in artifacts.items():
        before = time.monotonic()
        db.execute("DROP TABLE IF EXISTS expected")
        db.execute(
            "CREATE TABLE expected AS " + expected_query(name),
            {"begin": begin, "finish": finish},
        )
        db.read_parquet(path).create_view("actual", replace=True)
        columns = ",".join([*MARTS[name].keys, *MARTS[name].measures])
        mismatch = db.execute(f"""SELECT count(*) FROM (
            (SELECT {columns} FROM expected EXCEPT ALL SELECT {columns} FROM actual)
            UNION ALL
            (SELECT {columns} FROM actual EXCEPT ALL
            SELECT {columns} FROM expected))""").fetchone()
        if mismatch != (0,):
            raise ValueError(
                f"Independent Silver/Gold grouping mismatch: {month}/{name}"
            )
        counts[name] = 0
        print(
            f"Independent audit {month}/{name}: matched "
            f"({time.monotonic() - before:.3f}s)",
            flush=True,
        )
    return counts
