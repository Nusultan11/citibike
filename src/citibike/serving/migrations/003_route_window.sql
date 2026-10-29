-- Exact shared serving rollup keyed by immutable Gold month IDs and bounds.
CREATE TABLE IF NOT EXISTS analytics_route_windows (
 window_id TEXT PRIMARY KEY CHECK(window_id ~ '^[0-9a-f]{64}$'),
 manifest JSONB NOT NULL, ready_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS analytics_route_window_facts (
 window_id TEXT NOT NULL,
 vehicle TEXT NOT NULL,membership TEXT NOT NULL,
 from_station TEXT NOT NULL,to_station TEXT NOT NULL,
 n BIGINT NOT NULL CHECK(n>=0),
 duration_us BIGINT NOT NULL CHECK(duration_us>=0),
 duration_n BIGINT NOT NULL CHECK(duration_n>=0)
) PARTITION BY LIST(window_id);
