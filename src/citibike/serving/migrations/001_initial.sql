CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS publications (
    id TEXT PRIMARY KEY, metadata JSONB NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ride_hours (
    publication_id TEXT NOT NULL REFERENCES publications(id),
    hour_utc TIMESTAMPTZ NOT NULL,
    completed_published_rides BIGINT NOT NULL CHECK (completed_published_rides >= 0),
    PRIMARY KEY (publication_id,hour_utc)
);
CREATE TABLE IF NOT EXISTS station_hours (
    publication_id TEXT NOT NULL REFERENCES publications(id),
    station_id TEXT NOT NULL, hour_utc TIMESTAMPTZ NOT NULL,
    departures_s BIGINT NOT NULL CHECK (departures_s >= 0),
    arrivals_s BIGINT NOT NULL CHECK (arrivals_s >= 0),
    PRIMARY KEY (publication_id,station_id,hour_utc)
);
CREATE TABLE IF NOT EXISTS active_publication (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    publication_id TEXT NOT NULL REFERENCES publications(id)
);
