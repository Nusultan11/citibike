from pathlib import Path

import duckdb


def build_gold(
    db: duckdb.DuckDBPyConnection, target: Path, silver_partition: str
) -> dict[str, int]:
    db.execute("""
        CREATE TABLE ride_hours AS
        SELECT date_trunc('hour', started_at_utc) hour_utc,
               count(*)::BIGINT completed_published_rides
        FROM silver GROUP BY 1;
        CREATE TABLE station_hours AS
        SELECT station_id, hour_utc, sum(d)::BIGINT departures_s,
               sum(a)::BIGINT arrivals_s, (sum(a)-sum(d))::BIGINT net_trip_flow_s
        FROM (
            SELECT start_station_id station_id,
                   date_trunc('hour', started_at_utc) hour_utc, 1 d, 0 a
            FROM silver WHERE start_station_id IS NOT NULL
            UNION ALL
            SELECT end_station_id, date_trunc('hour', ended_at_utc), 0, 1
            FROM silver WHERE end_station_id IS NOT NULL
        ) events GROUP BY 1,2
    """)
    expected = db.execute("""
        SELECT count(*), count(start_station_id), count(end_station_id),
        count(*) FILTER (WHERE started_at_utc >= (SELECT period_start FROM context)
                        AND started_at_utc < (SELECT period_end FROM context))
        FROM silver
    """).fetchone()
    observed = db.execute("""
        SELECT (SELECT sum(completed_published_rides) FROM ride_hours),
               (SELECT coalesce(sum(departures_s),0) FROM station_hours),
               (SELECT coalesce(sum(arrivals_s),0) FROM station_hours),
               (SELECT coalesce(sum(completed_published_rides),0) FROM ride_hours
                WHERE hour_utc >= (SELECT period_start FROM context)
                  AND hour_utc < (SELECT period_end FROM context))
    """).fetchone()
    if expected != observed:
        raise ValueError("Silver/Gold reconciliation failed")
    if db.execute("""
        SELECT count(*) FROM (
            SELECT station_id,hour_utc FROM station_hours
            GROUP BY 1,2 HAVING count(*)<>1
        )
    """).fetchone() != (0,):
        raise ValueError("Gold station/hour grain violated")
    for name in ["ride_hours", "station_hours"]:
        db.execute(f"ALTER TABLE {name} ADD COLUMN source_object_id VARCHAR")
        db.execute(f"ALTER TABLE {name} ADD COLUMN pipeline_version VARCHAR")
        db.execute(f"ALTER TABLE {name} ADD COLUMN silver_partition VARCHAR")
        db.execute(
            f"""UPDATE {name} SET
            source_object_id=(SELECT min(source_object_id) FROM silver),
            pipeline_version=(SELECT min(pipeline_version) FROM silver),
            silver_partition=?""",
            [silver_partition],
        )
        db.execute(
            f"COPY {name} TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(target / f"{name}.parquet")],
        )
    assert expected is not None
    return dict(
        zip(
            [
                "canonical_rides",
                "known_start_station_rides",
                "known_end_station_rides",
                "completed_published_rides",
            ],
            map(int, expected),
            strict=True,
        )
    )
