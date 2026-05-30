-- classify.sql — Per-candidate classification (deps + time-volatility + strategy).
-- Port of v1's 01_classify_candidate.sql, adapted for MCP-native execution.
--
-- Caller: mv-optimization-validator agent, via execute_sql.
-- Substitute :schema and :mv with the candidate's schema and matview name before calling.
--
-- Output: JSON-shaped result with columns:
--   mv, deps_count, time_dependent, strategy_key, recipe_chain,
--   dependent_mvs (JSON array of {dependent_mv, depth}),
--   time_volatile_funcs (JSON array of matched function names)
--
-- Strategy grid (same as v1):
--   deps_count=0, time_dependent=false  → leaf_deterministic    → recipe: index_only -> leaf_swap
--   deps_count=0, time_dependent=true   → leaf_time_dependent   → recipe: index_only -> leaf_swap (time-aligned)
--   deps_count>0, time_dependent=false  → has_deps_deterministic → recipe: index_only -> subtree_drop_recreate
--   deps_count>0, time_dependent=true   → has_deps_time_dependent → recipe: index_only (only — no fallback)
--
-- v2 differences from v1:
--   - Single result set (v1 emitted 3 result sets; psql displayed all three separately).
--   - Aggregates the dependent MV list and time-volatile funcs into JSON columns.
--   - No shell text parsing needed — caller reads execute_sql JSON response directly.

WITH RECURSIVE deps_walk AS (
  SELECT
    c.oid AS dep_oid,
    n.nspname AS dep_schema,
    c.relname AS dep_mv,
    1 AS depth,
    ARRAY[c.oid] AS visited
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND d.refobjid = format('%I.%I', :'schema', :'mv')::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
  UNION ALL
  SELECT
    c.oid,
    n.nspname,
    c.relname,
    a.depth + 1,
    a.visited || c.oid
  FROM deps_walk a
  JOIN pg_depend d ON d.refobjid = a.dep_oid
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
    AND NOT (c.oid = ANY(a.visited))
    AND a.depth < 10
),
deduped_deps AS (
  SELECT
    format('%I.%I', dep_schema, dep_mv) AS dependent_mv,
    MAX(depth) AS depth
  FROM deps_walk
  GROUP BY dep_schema, dep_mv
),
classification AS (
  SELECT
    (SELECT count(DISTINCT dep_oid) FROM deps_walk) AS deps_count,
    pg_get_viewdef(format('%I.%I', :'schema', :'mv')::regclass) ~*
      '\m(current_date|current_timestamp|localtimestamp|now\s*\(|clock_timestamp|statement_timestamp|transaction_timestamp|timeofday)\M'
      AS time_dependent
)
SELECT
  format('%I.%I', :'schema', :'mv') AS mv,
  cl.deps_count,
  cl.time_dependent,
  CASE
    WHEN cl.deps_count = 0 AND NOT cl.time_dependent THEN 'leaf_deterministic'
    WHEN cl.deps_count = 0 AND cl.time_dependent     THEN 'leaf_time_dependent'
    WHEN cl.deps_count > 0 AND NOT cl.time_dependent THEN 'has_deps_deterministic'
    WHEN cl.deps_count > 0 AND cl.time_dependent     THEN 'has_deps_time_dependent'
  END AS strategy_key,
  CASE
    WHEN cl.deps_count = 0 AND NOT cl.time_dependent THEN 'index_only -> leaf_swap'
    WHEN cl.deps_count = 0 AND cl.time_dependent     THEN 'index_only -> leaf_swap (time-aligned)'
    WHEN cl.deps_count > 0 AND NOT cl.time_dependent THEN 'index_only -> subtree_drop_recreate'
    WHEN cl.deps_count > 0 AND cl.time_dependent     THEN 'index_only (only — no fallback)'
  END AS recipe_chain,
  COALESCE(
    (SELECT json_agg(json_build_object('dependent_mv', dependent_mv, 'depth', depth) ORDER BY depth DESC)
     FROM deduped_deps),
    '[]'::json
  ) AS dependent_mvs,
  ARRAY(
    SELECT m FROM unnest(ARRAY[
      'current_date','current_timestamp','localtimestamp','now()','clock_timestamp',
      'statement_timestamp','transaction_timestamp','timeofday'
    ]) AS m
    WHERE pg_get_viewdef(format('%I.%I', :'schema', :'mv')::regclass) ~* ('\m' || m || '\M')
  ) AS time_volatile_funcs
FROM classification cl;
