"""F3 fact grains and exact additive measures; normative formulas in METRICS."""

from dataclasses import dataclass

VERSION = "f3.0.0"
GEO_VERSION = "trip-wgs84-grid-001"
GBFS_VERSION = "f3-ttl-step-2"
NY_ZONE = "America/New_York"
MEASURES = {
    "n": "BIGINT",
    "duration_us": "BIGINT",
    "duration_n": "BIGINT",
    "departures": "BIGINT",
    "arrivals": "BIGINT",
    "arrival_rows": "BIGINT",
    "route_n": "BIGINT",
    "geo_n": "BIGINT",
}
ROUTE_MEASURES = {k: MEASURES[k] for k in ["n", "duration_us", "duration_n"]}
CATEGORIES = {
    "vehicle": ("classic_bike", "electric_bike", "unknown"),
    "membership": ("member", "casual", "unknown"),
}


@dataclass(frozen=True)
class Mart:
    keys: dict[str, str]
    measures: dict[str, str]
    time_key: str
    serving: bool = True


HOUR = {"hour_utc": "TIMESTAMPTZ"}
DAY = {"local_date": "DATE"}
MONTH = {"local_month": "DATE"}
CAT = {"vehicle": "VARCHAR", "membership": "VARCHAR"}
STATION = {"station_id": "VARCHAR"}
ROUTE = {"from_station": "VARCHAR", "to_station": "VARCHAR"}
GEO = {
    "event_type": "VARCHAR",
    "cell_lat": "INTEGER",
    "cell_lon": "INTEGER",
    "station_key": "VARCHAR",
}
MARTS = {
    "trip_hourly": Mart({**HOUR, **CAT}, MEASURES, "hour_utc"),
    "trip_daily": Mart({**DAY, **CAT}, MEASURES, "local_date"),
    "trip_monthly": Mart({**MONTH, **CAT}, MEASURES, "local_month"),
    "fact_station_hourly": Mart({**STATION, **HOUR}, MEASURES, "hour_utc"),
    "fact_station_daily": Mart({**STATION, **DAY}, MEASURES, "local_date"),
    "station_category_hourly": Mart({**STATION, **HOUR, **CAT}, MEASURES, "hour_utc"),
    "station_category_daily": Mart({**STATION, **DAY, **CAT}, MEASURES, "local_date"),
    "station_category_monthly": Mart(
        {**STATION, **MONTH, **CAT}, MEASURES, "local_month"
    ),
    "fact_route_daily": Mart({**DAY, **ROUTE}, ROUTE_MEASURES, "local_date"),
    "route_category_daily": Mart({**DAY, **ROUTE, **CAT}, ROUTE_MEASURES, "local_date"),
    "route_monthly": Mart({**MONTH, **ROUTE}, ROUTE_MEASURES, "local_month"),
    "route_category_monthly": Mart(
        {**MONTH, **ROUTE, **CAT}, ROUTE_MEASURES, "local_month"
    ),
    "geo_category_daily": Mart({**DAY, **GEO, **CAT}, {"n": "BIGINT"}, "local_date"),
}
CORE = (
    "trip_hourly",
    "station_category_hourly",
    "route_category_daily",
    "geo_category_daily",
)


def category_sql(field: str, dimension: str) -> str:
    known = CATEGORIES[dimension][:-1]
    literals = ",".join(f"'{value}'" for value in known)
    return f"CASE WHEN {field} IN ({literals}) THEN {field} ELSE 'unknown' END"
