-- Stage A: Top-N MV candidate selection ranked by total exec time, with structural filters.
--
-- Output columns:
--   schema, mv, size_pretty, size_bytes,
--   sum_total_exec_ms, top_query_mean_ms, has_unique_idx,
--   top_query_kind ('SELECT' | 'REFRESH' | 'CREATE' | 'OTHER'),
--   top_query_text (first 200 chars, for canonical-query identification)
--
-- Filters applied:
--   - has_unique_idx = true (REFRESH MATERIALIZED VIEW CONCURRENTLY requires it)
--   - top_query_kind = 'SELECT' (REFRESH/CREATE indicate no real downstream traffic)
--   - schema not in pg_catalog/information_schema/__autoresearch__
--
-- Self-time fraction is NOT computed here (Stage B does that with EXPLAIN ANALYZE per candidate).
--
-- Usage:
--   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -X -v limit=30 -f 00_select_candidates.sql'
--
-- The -v limit=N parameter is required. There is no default.

WITH mv_stats AS (
  SELECT
    n.nspname AS schemaname,
    c.relname AS matviewname,
    pg_total_relation_size(c.oid) AS size_bytes,
    pg_size_pretty(pg_total_relation_size(c.oid)) AS size_pretty,
    EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.oid AND i.indisunique
    ) AS has_unique_idx
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE c.relkind = 'm'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', '__autoresearch__')
),
mv_pgss AS (
  SELECT
    s.schemaname,
    s.matviewname,
    s.size_bytes,
    s.size_pretty,
    s.has_unique_idx,
    -- aggregate stats over all pg_stat_statements rows that reference the MV by name
    COALESCE((
      SELECT round(sum(pss.total_exec_time)::numeric, 2)
      FROM pg_stat_statements pss
      WHERE pss.query LIKE '%' || s.matviewname || '%'
    ), 0) AS sum_total_exec_ms,
    -- top single query (highest total_exec_time) referencing the MV
    (
      SELECT pss.query
      FROM pg_stat_statements pss
      WHERE pss.query LIKE '%' || s.matviewname || '%'
      ORDER BY pss.total_exec_time DESC
      LIMIT 1
    ) AS top_query_text,
    (
      SELECT round(pss.mean_exec_time::numeric, 2)
      FROM pg_stat_statements pss
      WHERE pss.query LIKE '%' || s.matviewname || '%'
      ORDER BY pss.total_exec_time DESC
      LIMIT 1
    ) AS top_query_mean_ms
  FROM mv_stats s
),
classified AS (
  SELECT
    schemaname,
    matviewname,
    size_pretty,
    size_bytes,
    has_unique_idx,
    sum_total_exec_ms,
    top_query_mean_ms,
    CASE
      WHEN top_query_text IS NULL THEN 'NONE'
      WHEN top_query_text ~* '^\s*REFRESH\s+MATERIALIZED' THEN 'REFRESH'
      WHEN top_query_text ~* '^\s*CREATE\s+(MATERIALIZED|UNIQUE\s+INDEX|INDEX)' THEN 'CREATE'
      WHEN top_query_text ~* '^\s*SELECT' THEN 'SELECT'
      ELSE 'OTHER'
    END AS top_query_kind,
    -- Collapse newlines and trim so each row is exactly one output line.
    -- Pipes inside the SQL are escaped (replaced with U+2502) so they don't break field-separator parsing.
    translate(
      regexp_replace(left(top_query_text, 200), E'[\n\r\t]+', ' ', 'g'),
      '|',
      '│'
    ) AS top_query_preview
  FROM mv_pgss
)
SELECT *
FROM classified
WHERE has_unique_idx = true
  AND top_query_kind = 'SELECT'
  AND sum_total_exec_ms > 0
ORDER BY sum_total_exec_ms DESC
LIMIT :limit;
