from pathlib import Path

import duckdb
import pytest

from citibike.analytics.audit import verify_month
from citibike.analytics.contracts import CORE, MARTS
from citibike.analytics.transform import (
    contribution_tables,
    month_tables,
    scalar_metrics,
    write_tables,
)
from tests.fixtures_analytics import analytical_silver


def test_analytical_grains_boundaries_dst_unknown_and_independent_oracle(
    tmp_path: Path,
) -> None:
    contributions = tmp_path / "contribution"
    contributions.mkdir()
    source = tmp_path / "silver.parquet"
    with duckdb.connect() as db:
        analytical_silver(db)
        db.execute("COPY silver TO ? (FORMAT PARQUET)", [str(source)])
        contribution_tables(db)
        write_tables(db, contributions, CORE)
        assert db.execute(
            "SELECT count(*) FROM geo_category_daily WHERE cell_lat=8999 "
            "AND cell_lon=17999"
        ).fetchone() == (1,)
        assert db.execute(
            "SELECT count(*) FROM geo_category_daily WHERE cell_lat>8999"
        ).fetchone() == (0,)
    for month in ["2024-09", "2024-10", "2024-11", "2025-03"]:
        target = tmp_path / month
        target.mkdir()
        with duckdb.connect() as db:
            db.execute("SET TimeZone='UTC'")
            for name in CORE:
                db.read_parquet(str(contributions / f"{name}.parquet")).create_view(
                    f"input_{name}"
                )
            month_tables(db, month)
            write_tables(db, target, list(MARTS))
            if month == "2024-10":
                metrics = scalar_metrics(db)
                assert metrics == {
                    "n": 2,
                    "duration_us": 420000000,
                    "duration_n": 2,
                    "departures": 1,
                    "arrivals": 1,
                    "arrival_rows": 2,
                    "route_n": 1,
                    "geo_n": 1,
                }
            if month == "2024-11":
                assert db.execute(
                    "SELECT count(DISTINCT hour_utc) FROM trip_hourly WHERE "
                    "hour_utc IN ('2024-11-03 05:00:00+00','2024-11-03 "
                    "06:00:00+00')"
                ).fetchone() == (2,)
        with duckdb.connect() as oracle:
            oracle.execute("SET TimeZone='UTC'")
            oracle.read_parquet(str(source)).create_view("audit_input")
            assert all(
                value == 0
                for value in verify_month(
                    oracle,
                    month,
                    {name: str(target / f"{name}.parquet") for name in MARTS},
                ).values()
            )
    bad = tmp_path / "bad.parquet"
    with duckdb.connect() as db:
        db.read_parquet(str(tmp_path / "2024-10/trip_hourly.parquet")).create_view(
            "original"
        )
        db.execute(
            "COPY (SELECT * REPLACE(n+1 AS n) FROM original) TO ? (FORMAT PARQUET)",
            [str(bad)],
        )
        db.read_parquet(str(source)).create_view("audit_input")
        with pytest.raises(ValueError, match="grouping mismatch"):
            verify_month(db, "2024-10", {"trip_hourly": str(bad)})
