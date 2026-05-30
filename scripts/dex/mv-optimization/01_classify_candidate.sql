-- Stage C: Per-candidate classification.
--
-- Two axes:
--   deps_count       — # of distinct MVs (recursive) that reference this MV through pg_rewrite.
--                      Used to choose between leaf-swap and subtree-drop-recreate strategies.
--   time_dependent   — true if pg_get_viewdef matches a time-volatile function regex.
--                      Used to choose the equality gate.
--
-- Strategy is the cell at (deps_count > 0, time_dependent) in the strategy table.
--
-- Output: 3 result sets:
--   1. Dependent MV list with depth (drop order: highest depth first)
--   2. Time-volatility classification with matched function list
--   3. Strategy assignment (mv, deps_count, time_dependent, strategy_key, recipe_chain)
--
-- Usage:
--   doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -X \
--     -v schema=entities -v mv=mv_fmcsa_authority_grants -f 01_classify_candidate.sql'

\set ON_ERROR_STOP on

-- 1. Dependent MVs (recursive walk through pg_rewrite/pg_depend)
--    Filters: r.ev_class <> d.refobjid skips MV self-rules; visited array breaks cycles.
--    GROUP BY at the end dedups; MAX(depth) gives the deepest reachable path (drop order).
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
)
SELECT
  format('%I.%I', dep_schema, dep_mv) AS dependent_mv,
  MAX(depth) AS depth
FROM deps_walk
GROUP BY dep_schema, dep_mv
ORDER BY MAX(depth) DESC, dep_schema, dep_mv;

-- 2. Time-volatility classification
SELECT
  format('%I.%I', :'schema', :'mv') AS mv,
  pg_get_viewdef(format('%I.%I', :'schema', :'mv')::regclass) ~* '\m(current_date|current_timestamp|localtimestamp|now\s*\(|clock_timestamp|statement_timestamp|transaction_timestamp|timeofday)\M' AS time_dependent,
  ARRAY(
    SELECT m FROM unnest(ARRAY['current_date','current_timestamp','localtimestamp','now()','clock_timestamp','statement_timestamp','transaction_timestamp','timeofday']) AS m
    WHERE pg_get_viewdef(format('%I.%I', :'schema', :'mv')::regclass) ~* ('\m' || m || '\M')
  ) AS time_volatile_funcs;

-- 3. Strategy assignment (parsed by orchestrator)
WITH RECURSIVE deps_walk AS (
  SELECT
    c.oid AS dep_oid,
    1 AS depth,
    ARRAY[c.oid] AS visited
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND d.refobjid = format('%I.%I', :'schema', :'mv')::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
  UNION ALL
  SELECT
    c.oid,
    a.depth + 1,
    a.visited || c.oid
  FROM deps_walk a
  JOIN pg_depend d ON d.refobjid = a.dep_oid
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
    AND NOT (c.oid = ANY(a.visited))
    AND a.depth < 10
),
classification AS (
  SELECT
    (SELECT count(DISTINCT dep_oid) FROM deps_walk) AS deps_count,
    pg_get_viewdef(format('%I.%I', :'schema', :'mv')::regclass) ~* '\m(current_date|current_timestamp|localtimestamp|now\s*\(|clock_timestamp|statement_timestamp|transaction_timestamp|timeofday)\M' AS time_dependent
)
SELECT
  format('%I.%I', :'schema', :'mv') AS mv,
  deps_count,
  time_dependent,
  CASE
    WHEN deps_count = 0 AND NOT time_dependent THEN 'leaf_deterministic'
    WHEN deps_count = 0 AND     time_dependent THEN 'leaf_time_dependent'
    WHEN deps_count > 0 AND NOT time_dependent THEN 'has_deps_deterministic'
    WHEN deps_count > 0 AND     time_dependent THEN 'has_deps_time_dependent'
  END AS strategy_key,
  CASE
    WHEN deps_count = 0 AND NOT time_dependent THEN 'index_only -> leaf_swap'
    WHEN deps_count = 0 AND     time_dependent THEN 'index_only -> leaf_swap (time-aligned)'
    WHEN deps_count > 0 AND NOT time_dependent THEN 'index_only -> subtree_drop_recreate'
    WHEN deps_count > 0 AND     time_dependent THEN 'index_only (only — no fallback)'
  END AS recipe_chain
FROM classification;
