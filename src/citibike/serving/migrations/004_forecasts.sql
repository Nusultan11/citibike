-- F4 immutable versions; predictions never overwrite prior origins/models.
CREATE TABLE IF NOT EXISTS ml_forecast_publications (
 publication_id TEXT PRIMARY KEY CHECK(publication_id ~ '^[0-9a-f]{64}$'),
 manifest JSONB NOT NULL,
 published_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS ml_forecasts (
 publication_id TEXT NOT NULL REFERENCES ml_forecast_publications,
 station_id TEXT NOT NULL,
 forecast_origin_utc TIMESTAMPTZ NOT NULL,
 target_hour_utc TIMESTAMPTZ NOT NULL,
 horizon_h INTEGER NOT NULL CHECK(horizon_h BETWEEN 1 AND 24),
 prediction_type TEXT NOT NULL CHECK(prediction_type IN ('departures','arrivals')),
 predicted_value DOUBLE PRECISION,
 status TEXT NOT NULL CHECK(status IN ('ok','unavailable')),
 reason TEXT,
 model_id TEXT NOT NULL,
 feature_available_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(publication_id,station_id,forecast_origin_utc,target_hour_utc,horizon_h,prediction_type),
 CHECK((status='ok' AND predicted_value>=0 AND predicted_value<'Infinity'::float8)
    OR (status='unavailable' AND predicted_value IS NULL AND reason IS NOT NULL)),
 CHECK(feature_available_at<=forecast_origin_utc),
 CHECK(target_hour_utc=((date_trunc('hour',forecast_origin_utc AT TIME ZONE 'UTC'))
     AT TIME ZONE 'UTC')+horizon_h*INTERVAL '1 hour'));
CREATE TABLE IF NOT EXISTS ml_forecast_active (
 singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK(singleton),
 publication_id TEXT NOT NULL REFERENCES ml_forecast_publications);
