-- SQL CHECK accepts UNKNOWN; explicitly forbid NULL on a successful forecast.
DO $$
BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint
                WHERE conrelid='ml_forecasts'::regclass
                AND conname='ml_forecasts_value_status') THEN
  ALTER TABLE ml_forecasts ADD CONSTRAINT ml_forecasts_value_status CHECK (
   (status='ok' AND predicted_value IS NOT NULL
    AND predicted_value>=0 AND predicted_value<'Infinity'::float8)
   OR (status='unavailable' AND predicted_value IS NULL AND reason IS NOT NULL));
 END IF;
END $$;
