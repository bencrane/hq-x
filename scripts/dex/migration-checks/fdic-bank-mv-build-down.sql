BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM cron.job WHERE jobname = 'refresh_mv_fdic_targeting_and_failures_monthly') THEN
    PERFORM cron.unschedule('refresh_mv_fdic_targeting_and_failures_monthly');
  END IF;
END $$;

DROP MATERIALIZED VIEW IF EXISTS entities.mv_fdic_signal_delta_failures;
DROP MATERIALIZED VIEW IF EXISTS entities.mv_fdic_bank_targeting;

COMMIT;
