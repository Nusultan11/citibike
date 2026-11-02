-- F6 additive read models; earlier migrations and their tables are unchanged.
CREATE TABLE IF NOT EXISTS station_geo_publications (
 geo_id TEXT PRIMARY KEY CHECK(geo_id ~ '^[0-9a-f]{64}$'),
 analytics_publication_id TEXT NOT NULL REFERENCES analytics_publications(publication_id),
 manifest JSONB NOT NULL,
 published_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS station_geo_monthly (
 geo_id TEXT NOT NULL REFERENCES station_geo_publications(geo_id),
 station_id TEXT NOT NULL,
 local_month DATE NOT NULL CHECK(extract(day FROM local_month)=1),
 event_type TEXT NOT NULL CHECK(event_type IN ('start','end')),
 lat DOUBLE PRECISION CHECK(lat BETWEEN -90 AND 90),
 lon DOUBLE PRECISION CHECK(lon BETWEEN -180 AND 180),
 station_name TEXT,
 events BIGINT NOT NULL CHECK(events>0),
 coordinate_rows BIGINT NOT NULL CHECK(coordinate_rows BETWEEN 0 AND events),
 modal_rows BIGINT NOT NULL CHECK(modal_rows BETWEEN 0 AND coordinate_rows),
 distinct_coordinates INTEGER NOT NULL CHECK(distinct_coordinates>=0),
 CHECK((lat IS NULL)=(lon IS NULL) AND (lat IS NULL)=(coordinate_rows=0)),
 PRIMARY KEY(geo_id,station_id,local_month,event_type));
CREATE INDEX IF NOT EXISTS station_geo_monthly_month
 ON station_geo_monthly(geo_id,local_month);
CREATE TABLE IF NOT EXISTS risk_publications (
 publication_id TEXT PRIMARY KEY CHECK(publication_id ~ '^[0-9a-f]{64}$'),
 manifest JSONB NOT NULL,
 published_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS risk_active (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
 publication_id TEXT NOT NULL REFERENCES risk_publications(publication_id));
CREATE TABLE IF NOT EXISTS risk_stations (
 publication_id TEXT NOT NULL REFERENCES risk_publications(publication_id),
 station_id TEXT NOT NULL,
 eligible BOOLEAN NOT NULL,
 reasons TEXT[] NOT NULL,
 gbfs_id TEXT,
 capacity INTEGER CHECK(capacity>=0),
 bikes INTEGER CHECK(bikes>=0),
 docks INTEGER CHECK(docks>=0),
 lat DOUBLE PRECISION,
 lon DOUBLE PRECISION,
 CHECK(eligible=(cardinality(reasons)=0)),
 CHECK(eligible OR (capacity IS NULL AND bikes IS NULL AND docks IS NULL)),
 PRIMARY KEY(publication_id,station_id));
CREATE TABLE IF NOT EXISTS risk_policy_outcomes (
 publication_id TEXT NOT NULL REFERENCES risk_publications(publication_id),
 scale DOUBLE PRECISION NOT NULL CHECK(scale>=0),
 event_order TEXT NOT NULL,
 policy TEXT NOT NULL CHECK(policy IN ('no_action','heuristic','optimizer')),
 repeats INTEGER NOT NULL CHECK(repeats>0),
 totals JSONB NOT NULL,
 movement_cost DOUBLE PRECISION NOT NULL CHECK(movement_cost>=0),
 moved_bikes INTEGER NOT NULL CHECK(moved_bikes>=0),
 vehicles INTEGER NOT NULL CHECK(vehicles>=0),
 constraint_violations INTEGER NOT NULL CHECK(constraint_violations>=0),
 gain_vs_no_action DOUBLE PRECISION,
 solver JSONB NOT NULL,
 PRIMARY KEY(publication_id,scale,event_order,policy));
CREATE TABLE IF NOT EXISTS risk_station_outcomes (
 publication_id TEXT NOT NULL REFERENCES risk_publications(publication_id),
 scale DOUBLE PRECISION NOT NULL,
 event_order TEXT NOT NULL,
 policy TEXT NOT NULL,
 station_id TEXT NOT NULL,
 status TEXT NOT NULL,
 empty_fraction DOUBLE PRECISION CHECK(empty_fraction BETWEEN 0 AND 1),
 empty_low DOUBLE PRECISION, empty_high DOUBLE PRECISION,
 full_fraction DOUBLE PRECISION CHECK(full_fraction BETWEEN 0 AND 1),
 full_low DOUBLE PRECISION, full_high DOUBLE PRECISION,
 repeats INTEGER NOT NULL CHECK(repeats>0),
 PRIMARY KEY(publication_id,scale,event_order,policy,station_id),
 FOREIGN KEY(publication_id,scale,event_order,policy)
  REFERENCES risk_policy_outcomes(publication_id,scale,event_order,policy));
CREATE TABLE IF NOT EXISTS risk_transfers (
 publication_id TEXT NOT NULL REFERENCES risk_publications(publication_id),
 scale DOUBLE PRECISION NOT NULL,
 policy TEXT NOT NULL CHECK(policy IN ('heuristic','optimizer')),
 sequence INTEGER NOT NULL CHECK(sequence>=0),
 source TEXT NOT NULL,
 destination TEXT NOT NULL CHECK(destination<>source),
 bikes INTEGER NOT NULL CHECK(bikes>0),
 vehicles INTEGER NOT NULL CHECK(vehicles>0),
 minutes DOUBLE PRECISION NOT NULL CHECK(minutes>=0),
 distance_km DOUBLE PRECISION NOT NULL CHECK(distance_km>=0),
 PRIMARY KEY(publication_id,scale,policy,sequence));
