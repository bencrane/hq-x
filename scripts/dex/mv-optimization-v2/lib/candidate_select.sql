-- candidate_select.sql — Stage A: Top-N MV candidate selection.
-- Port of v1's 00_select_candidates.sql, adapted for MCP-native execution.
--
-- Caller: mv-optimization-validator agent, via execute_sql.
-- Parameters: set :limit to the desired cap (default 30).
--
-- Output: JSON-shaped rows via execute_sql result. Columns:
--   schemaname, matviewname, size_pretty, size_bytes,
--   has_unique_idx, sum_total_exec_ms, top_query_mean_ms,
--   top_query_kind, top_query_preview (first 200 chars)
--
-- Filters:
--   - has_unique_idx = true (REFRESH MATERIALIZED VIEW CONCURRENTLY requires it)
--   - top_query_kind = 'SELECT' (REFRESH/CREATE = no real downstream traffic)
--   - sum_total_exec_ms > 0 (at least some recorded traffic)
--   - schema not in pg_catalog/information_schema/__autoresearch__
--
-- v2 differences from v1:
--   - No shell wrapper, no doppler, no psql variable binding via -v.
--   - Output is JSON from execute_sql, not pipe-delimited text.
--   - Caller substitutes :limit directly in the query before calling execute_sql.

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
    COALESCE((
      SELECT round(sum(pss.total_exec_time)::numeric, 2)
      FROM pg_stat_statements pss
      WHERE pss.query LIKE '%' || s.matviewname || '%'
    ), 0) AS sum_total_exec_ms,
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
    -- Newlines collapsed; first 200 chars
    regexp_replace(left(top_query_text, 200), E'[\n\r\t]+', ' ', 'g') AS top_query_preview
  FROM mv_pgss
)
SELECT *
FROM classified
WHERE has_unique_idx = true
  AND top_query_kind = 'SELECT'
  AND sum_total_exec_ms > 0
ORDER BY sum_total_exec_ms DESC
LIMIT 30;  -- caller should substitute desired limit here
