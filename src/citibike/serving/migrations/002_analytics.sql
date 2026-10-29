-- F3 immutable aggregate partitions; independent of the F1 publication.
CREATE TABLE IF NOT EXISTS analytics_partitions (
 partition_id TEXT PRIMARY KEY CHECK(partition_id ~ '^[0-9a-f]{64}$'),
 local_month DATE NOT NULL, manifest JSONB NOT NULL);
CREATE TABLE IF NOT EXISTS analytics_publications (
 publication_id TEXT PRIMARY KEY CHECK(publication_id ~ '^[0-9a-f]{64}$'),
 manifest JSONB NOT NULL, published_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS analytics_publication_partitions (
 publication_id TEXT REFERENCES analytics_publications(publication_id),
 partition_id TEXT REFERENCES analytics_partitions(partition_id),
 local_month DATE NOT NULL, PRIMARY KEY(publication_id,local_month));
CREATE TABLE IF NOT EXISTS analytics_active (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
 publication_id TEXT NOT NULL REFERENCES analytics_publications(publication_id));
CREATE TABLE IF NOT EXISTS analytics_availability (
 publication_id TEXT NOT NULL REFERENCES analytics_publications(publication_id),
 station_id TEXT NOT NULL, trip_station_id TEXT, valid_from TIMESTAMPTZ NOT NULL,
 valid_to TIMESTAMPTZ NOT NULL CHECK(valid_to>valid_from),
 rentable_bikes BIGINT CHECK(rentable_bikes>=0),
 returnable_docks BIGINT CHECK(returnable_docks>=0),
 collection_id TEXT NOT NULL,source_updated TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(publication_id,station_id,valid_from));
CREATE INDEX IF NOT EXISTS analytics_availability_time ON analytics_availability(publication_id,valid_from,valid_to);
CREATE TABLE IF NOT EXISTS analytics_trip_hourly (
 partition_id TEXT NOT NULL,
 hour_utc TIMESTAMPTZ NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_trip_daily (
 partition_id TEXT NOT NULL,
 local_date DATE NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_trip_monthly (
 partition_id TEXT NOT NULL,
 local_month DATE NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_fact_station_hourly (
 partition_id TEXT NOT NULL,
 station_id VARCHAR NOT NULL,
 hour_utc TIMESTAMPTZ NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_fact_station_daily (
 partition_id TEXT NOT NULL,
 station_id VARCHAR NOT NULL,
 local_date DATE NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_station_category_hourly (
 partition_id TEXT NOT NULL,
 station_id VARCHAR NOT NULL,
 hour_utc TIMESTAMPTZ NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_station_category_daily (
 partition_id TEXT NOT NULL,
 station_id VARCHAR NOT NULL,
 local_date DATE NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_station_category_monthly (
 partition_id TEXT NOT NULL,
 station_id VARCHAR NOT NULL,
 local_month DATE NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0),
 departures BIGINT NOT NULL CHECK(departures>=0),
 arrivals BIGINT NOT NULL CHECK(arrivals>=0),
 arrival_rows BIGINT NOT NULL CHECK(arrival_rows>=0),
 route_n BIGINT NOT NULL CHECK(route_n>=0),
 geo_n BIGINT NOT NULL CHECK(geo_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_fact_route_daily (
 partition_id TEXT NOT NULL,
 local_date DATE NOT NULL,
 from_station VARCHAR NOT NULL,
 to_station VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_route_category_daily (
 partition_id TEXT NOT NULL,
 local_date DATE NOT NULL,
 from_station VARCHAR NOT NULL,
 to_station VARCHAR NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_route_monthly (
 partition_id TEXT NOT NULL,
 local_month DATE NOT NULL,
 from_station VARCHAR NOT NULL,
 to_station VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_route_category_monthly (
 partition_id TEXT NOT NULL,
 local_month DATE NOT NULL,
 from_station VARCHAR NOT NULL,
 to_station VARCHAR NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0)
) PARTITION BY LIST(partition_id);
CREATE TABLE IF NOT EXISTS analytics_geo_category_daily (
 partition_id TEXT NOT NULL,
 local_date DATE NOT NULL,
 event_type VARCHAR NOT NULL,
 cell_lat INTEGER NOT NULL,
 cell_lon INTEGER NOT NULL,
 station_key VARCHAR NOT NULL,
 vehicle VARCHAR NOT NULL,
 membership VARCHAR NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0)
) PARTITION BY LIST(partition_id);
