-- s1 verify sidecar: returns single integer 1 iff all expected tables exist.
-- Pattern: Overture precedent (2026-05-03-overture-places-ingest-s1-verify.sql).
-- Invoked from harness as: psql -tAX -v ON_ERROR_STOP=1 -f <this-file>
-- and the harness greps for "1" on stdout.
SELECT (
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'entities'
       AND table_name IN (
         'source_faa_airmen_pilot_basic',
         'source_faa_airmen_pilot_cert',
         'source_faa_airmen_nonpilot_basic',
         'source_faa_airmen_nonpilot_cert'
       )) = 4
  AND
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'ops'
       AND table_name   = 'faa_airmen_ingest_runs') = 1
)::int;
