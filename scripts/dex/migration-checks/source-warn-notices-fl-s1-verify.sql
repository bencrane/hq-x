-- s1 verify sidecar: returns single integer 1 iff both expected tables exist.
-- Pattern: FAA airmen / Overture precedent (sidecar SQL avoids 3-deep
-- quoting hell of bash -c '...' wrapping psql -c "..." with embedded SQL).
-- Invoked from harness as a piped string passed to dex_psql_query (one-liner)
-- or via psql -tAX -v ON_ERROR_STOP=1 -f <this-file>.
SELECT (
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'entities'
       AND table_name   = 'source_warn_notices') = 1
  AND
  (SELECT COUNT(*) FROM information_schema.tables
     WHERE table_schema = 'ops'
       AND table_name   = 'warn_notices_ingest_runs') = 1
)::int;
