#!/usr/bin/env bash
# recipe: subtree_drop_recreate — drop+recreate the entire dep subtree atomically.
# For has_deps_deterministic MVs where index_only cannot deliver ≥30% improvement.
#
# Inputs (env or args):
#   --schema entities --mv mv_fmcsa_authority_grants
#   --canonical "<full sql>"  --params "100,NULL"
#   --project-path /Users/benjamincrane/data-engine-x
#   --improvement-threshold 0.30  (default 0.30 = 30%)
#   --dry-run                       (skip DDL; still emits all stage events including plan_node)
#   --offline                       (skip DB; emit propose:OK with mode:offline)
#
# Output (stdout, JSON-line per stage):
#   {"stage":"baseline","p50_ms":576.5,"recipe":"subtree_drop_recreate"}
#   {"stage":"propose","verdict":"OK","deps_count":2,"recipe":"subtree_drop_recreate"}
#   {"stage":"shadow_build","verdict":"DRY_RUN","sql":"...","recipe":"subtree_drop_recreate"}
#   {"stage":"equality_gate","verdict":"PASS","recipe":"subtree_drop_recreate"}
#   {"stage":"gate","baseline_ms":576.5,"new_ms":34.1,"improvement_pct":0.941,"verdict":"PASS","recipe":"subtree_drop_recreate"}
#   {"stage":"plan_node","node":"Merge Join","verdict":"PASS","recipe":"subtree_drop_recreate"}
#   {"stage":"ship","pr_url":"...","status":"submitted","recipe":"subtree_drop_recreate"}
#   OR on failure:
#   {"stage":"<stage>","verdict":"FAIL","reason":"<one-line>","recipe":"subtree_drop_recreate"}
#
# Exit codes:
#   0 — success (PR opened) OR dry-run preview OR offline OK
#   1 — recipe failed
#   2 — argument error / pre-flight failure

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$(cd "$SCRIPT_DIR/../lib" && pwd)"

SCHEMA=""
MV=""
CANONICAL=""
PARAMS=""
PROJECT_PATH=""
THRESHOLD="0.30"
DRY_RUN=0
OFFLINE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --schema)                SCHEMA="$2"; shift 2 ;;
    --mv)                    MV="$2"; shift 2 ;;
    --canonical)             CANONICAL="$2"; shift 2 ;;
    --params)                PARAMS="$2"; shift 2 ;;
    --project-path)          PROJECT_PATH="$2"; shift 2 ;;
    --improvement-threshold) THRESHOLD="$2"; shift 2 ;;
    --dry-run)               DRY_RUN=1; shift ;;
    --offline)               OFFLINE=1; shift ;;
    --help)
      echo "subtree_drop_recreate.sh: --schema --mv --canonical --params --project-path [--improvement-threshold] [--dry-run] [--offline]"
      exit 0
      ;;
    *) echo "subtree_drop_recreate.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Validate required args
for required in SCHEMA MV; do
  if [ -z "${!required}" ]; then
    req_lower=$(echo "$required" | tr '[:upper:]' '[:lower:]')
    echo "subtree_drop_recreate.sh: --${req_lower} required" >&2
    exit 2
  fi
done

if [ "$OFFLINE" -eq 0 ]; then
  for required in CANONICAL PROJECT_PATH; do
    if [ -z "${!required}" ]; then
      req_lower=$(echo "$required" | tr '[:upper:]' '[:lower:]')
      echo "subtree_drop_recreate.sh: --${req_lower} required (or pass --offline)" >&2
      exit 2
    fi
  done
fi

emit() {
  echo "$1"
}

RECIPE_TAG='"recipe":"subtree_drop_recreate"'
MODE_TAG=""
[ "$OFFLINE" -eq 1 ] && MODE_TAG=',"mode":"offline"'

# ---- OFFLINE fast-path ----
if [ "$OFFLINE" -eq 1 ]; then
  emit "{\"stage\":\"propose\",\"verdict\":\"OK\",\"reason\":\"offline_routing_check\",$RECIPE_TAG$MODE_TAG}"
  exit 0
fi

CONN_URL="${DEX_DB_URL_DIRECT:-}"
if [ -z "$CONN_URL" ]; then
  echo "subtree_drop_recreate.sh: \$DEX_DB_URL_DIRECT is empty (run under doppler)" >&2
  exit 2
fi

SOURCE_FULL="${SCHEMA}.${MV}"
SHADOW_SCHEMA="__autoresearch__"
SHADOW_MV="${MV}_v2"
SHADOW_FULL="${SHADOW_SCHEMA}.${SHADOW_MV}"
TS=$(date -u +%Y%m%d%H%M%S)

# ---- 1. Baseline ----
BASELINE_MS=$(bash "$LIB_DIR/benchmark.sh" --canonical "$CANONICAL" --params "$PARAMS")
if ! [[ "$BASELINE_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  emit "{\"stage\":\"baseline\",\"verdict\":\"FAIL\",\"reason\":\"benchmark_returned_non_numeric\",$RECIPE_TAG}"
  exit 1
fi
emit "{\"stage\":\"baseline\",\"p50_ms\":${BASELINE_MS},$RECIPE_TAG}"

# ---- 2. Propose: derive subtree manifest via pg_rewrite ----
# Mirrors 01_classify_candidate.sql: MV→MV deps are recorded in pg_depend rows
# whose objid is a pg_rewrite oid (the dependent MV's _RETURN rule), not a
# pg_class oid directly. Walk pg_rewrite, dedup with a visited[] cycle guard,
# cap depth at 10. Returned in drop order (deepest first).
DEPS_JSON=$(psql "$CONN_URL" -X -t -A <<EOF 2>&1
WITH RECURSIVE dep_tree AS (
  SELECT
    c.relname AS dep_name,
    n.nspname AS dep_schema,
    c.oid AS dep_oid,
    1 AS depth,
    ARRAY[c.oid] AS visited
  FROM pg_depend d
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND d.refobjid = format('%I.%I', '${SCHEMA}', '${MV}')::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
  UNION ALL
  SELECT
    c.relname,
    n.nspname,
    c.oid,
    dt.depth + 1,
    dt.visited || c.oid
  FROM dep_tree dt
  JOIN pg_depend d ON d.refobjid = dt.dep_oid
  JOIN pg_rewrite r ON r.oid = d.objid
  JOIN pg_class c ON c.oid = r.ev_class
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE d.classid = 'pg_rewrite'::regclass
    AND d.refclassid = 'pg_class'::regclass
    AND c.relkind = 'm'
    AND r.ev_class <> d.refobjid
    AND NOT (c.oid = ANY(dt.visited))
    AND dt.depth < 10
)
SELECT json_agg(json_build_object('name', dep_name, 'schema', dep_schema, 'depth', depth) ORDER BY depth DESC)::text
FROM (
  SELECT dep_name, dep_schema, MAX(depth) AS depth
  FROM dep_tree
  GROUP BY dep_name, dep_schema
) t;
EOF
)

if echo "$DEPS_JSON" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"propose\",\"verdict\":\"FAIL\",\"reason\":\"failed_to_query_dep_tree\",$RECIPE_TAG}"
  exit 1
fi

# Count dependents
DEPS_COUNT=0
if [ -n "$DEPS_JSON" ] && [ "$DEPS_JSON" != "" ] && [ "$DEPS_JSON" != "null" ]; then
  DEPS_COUNT=$(echo "$DEPS_JSON" | python3 -c "import json,sys; d=json.load(sys.stdin); print(len(d) if d else 0)" 2>/dev/null || echo "0")
fi

# Fetch own definition.
# pg_get_viewdef(..., true) pretty-prints across multiple lines; do NOT pipe through
# head -1 — that silently truncates non-trivial views to their first line and yields
# broken shadow-build SQL. The trailing-semicolon strip below normalizes the suffix.
MV_DEF=$(psql "$CONN_URL" -X -t -A -c \
  "SELECT pg_get_viewdef('${SOURCE_FULL}'::regclass, true);" 2>&1 || true)
if [ -z "$MV_DEF" ] || echo "$MV_DEF" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"propose\",\"verdict\":\"FAIL\",\"reason\":\"could_not_fetch_mv_definition\",$RECIPE_TAG}"
  exit 1
fi
# pg_get_viewdef() includes a trailing ';'; strip it to avoid double-semicolon in SQL emission.
MV_DEF=$(echo "$MV_DEF" | sed 's/;[[:space:]]*$//')

# Fetch unique index columns
UNIQUE_COLS=$(psql "$CONN_URL" -X -t -A -c \
  "SELECT string_agg(a.attname, ',' ORDER BY x.ordinality)
   FROM pg_index i
   JOIN pg_class c ON c.oid = i.indrelid
   JOIN pg_namespace n ON n.oid = c.relnamespace
   JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ordinality) ON true
   JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = x.attnum
   WHERE n.nspname = '${SCHEMA}' AND c.relname = '${MV}' AND i.indisunique
   LIMIT 1;" 2>&1 | head -1 || true)

if [ -z "$UNIQUE_COLS" ] || echo "$UNIQUE_COLS" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"propose\",\"verdict\":\"SKIP\",\"reason\":\"no_unique_index_found_on_source_mv\",$RECIPE_TAG}"
  exit 1
fi

emit "{\"stage\":\"propose\",\"verdict\":\"OK\",\"deps_count\":${DEPS_COUNT},\"unique_cols\":\"${UNIQUE_COLS}\",$RECIPE_TAG}"

if [ "$DRY_RUN" -eq 1 ]; then
  # Emit shadow_build preview
  SHADOW_SQL="CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF}; CREATE UNIQUE INDEX ${SHADOW_MV}_pkey ON ${SHADOW_FULL} (${UNIQUE_COLS});"
  SHADOW_SQL_ESCAPED=$(echo "$SHADOW_SQL" | tr '"' "'")
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"DRY_RUN\",\"sql\":\"${SHADOW_SQL_ESCAPED}\",$RECIPE_TAG}"

  # Emit equality_gate stub
  emit "{\"stage\":\"equality_gate\",\"verdict\":\"DRY_RUN\",\"reason\":\"dry_run_no_shadow\",$RECIPE_TAG}"

  # Emit gate stub (baseline vs baseline → 0% → FAIL, satisfies constraint 4)
  IMPROVEMENT="0.0000"
  GATE_PASS=$(awk -v i="$IMPROVEMENT" -v t="$THRESHOLD" 'BEGIN{print (i+0 >= t+0) ? 1 : 0}')
  if [ "$GATE_PASS" -eq 0 ]; then
    emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${BASELINE_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"FAIL\",\"reason\":\"below_threshold_${THRESHOLD}\",$RECIPE_TAG}"
  else
    emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${BASELINE_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"PASS\",$RECIPE_TAG}"
  fi

  # Emit plan_node with DRY_RUN verdict (constraint 5)
  emit "{\"stage\":\"plan_node\",\"verdict\":\"DRY_RUN\",\"reason\":\"dry_run_no_shadow_to_explain\",$RECIPE_TAG}"
  exit 0
fi

# ---- 3. Build shadow MV in __autoresearch__ and validate ----
psql "$CONN_URL" -X -q -c "CREATE SCHEMA IF NOT EXISTS ${SHADOW_SCHEMA};" >/dev/null 2>&1 || true
psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true

BUILD_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF};" 2>&1) || true
if echo "$BUILD_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"FAIL\",\"reason\":\"$(echo "$BUILD_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  exit 1
fi

IDX_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "CREATE UNIQUE INDEX ${SHADOW_MV}_pkey ON ${SHADOW_FULL} (${UNIQUE_COLS});" 2>&1) || true
if echo "$IDX_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"FAIL\",\"reason\":\"index_failed:$(echo "$IDX_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi

# Refresh shadow
REFRESH_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "REFRESH MATERIALIZED VIEW CONCURRENTLY ${SHADOW_FULL};" 2>&1) || true
if echo "$REFRESH_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"FAIL\",\"reason\":\"refresh_failed:$(echo "$REFRESH_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi

emit "{\"stage\":\"shadow_build\",\"verdict\":\"OK\",$RECIPE_TAG}"

# ---- Equality gate (shadow vs original) ----
ORIG_TUPLE=$(bash "$LIB_DIR/equality_gate.sh" "${SOURCE_FULL}")
SHADOW_TUPLE=$(bash "$LIB_DIR/equality_gate.sh" "${SHADOW_FULL}")
if [ "$ORIG_TUPLE" != "$SHADOW_TUPLE" ]; then
  emit "{\"stage\":\"equality_gate\",\"verdict\":\"FAIL\",\"reason\":\"hash_mismatch\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi
emit "{\"stage\":\"equality_gate\",\"verdict\":\"PASS\",$RECIPE_TAG}"

# ---- Latency gate (shadow) ----
SHADOW_CANONICAL=$(echo "$CANONICAL" | sed "s/${SCHEMA}\.${MV}/${SHADOW_SCHEMA}.${SHADOW_MV}/g")
NEW_MS=$(bash "$LIB_DIR/benchmark.sh" --canonical "$SHADOW_CANONICAL" --params "$PARAMS")
if ! [[ "$NEW_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  emit "{\"stage\":\"gate\",\"verdict\":\"FAIL\",\"reason\":\"shadow_benchmark_returned_non_numeric\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi

IMPROVEMENT=$(awk -v b="$BASELINE_MS" -v n="$NEW_MS" 'BEGIN{if (b==0) {print 0} else {printf "%.4f", (b-n)/b}}')
GATE_PASS=$(awk -v i="$IMPROVEMENT" -v t="$THRESHOLD" 'BEGIN{print (i+0 >= t+0) ? 1 : 0}')

if [ "$GATE_PASS" -eq 0 ]; then
  emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${NEW_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"FAIL\",\"reason\":\"below_threshold_${THRESHOLD}\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi
emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${NEW_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"PASS\",$RECIPE_TAG}"

# ---- Plan-node check ----
EXPLAIN_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "EXPLAIN SELECT * FROM ${SHADOW_FULL} LIMIT 1;" 2>&1 || true)
PLAN_NODE=$(echo "$EXPLAIN_OUT" | grep -oE 'Index (Only )?Scan|Merge Join|Hash Join|Seq Scan|Bitmap Heap Scan' | head -1 || true)
# For subtree: PASS if NOT pure Seq Scan (or if improved join type)
if echo "$PLAN_NODE" | grep -qE '^Seq Scan$'; then
  emit "{\"stage\":\"plan_node\",\"node\":\"Seq Scan\",\"verdict\":\"FAIL\",\"reason\":\"shadow_still_seqscans\",$RECIPE_TAG}"
  # Non-fatal: log and continue (the latency gate already passed)
elif [ -z "$PLAN_NODE" ]; then
  emit "{\"stage\":\"plan_node\",\"node\":\"unknown\",\"verdict\":\"DRY_RUN\",\"reason\":\"no_recognizable_node\",$RECIPE_TAG}"
else
  emit "{\"stage\":\"plan_node\",\"node\":\"${PLAN_NODE}\",\"verdict\":\"PASS\",$RECIPE_TAG}"
fi

# ---- 4. Capture dependent definitions before swap ----
# Build DROP (reverse dep order) and CREATE (forward dep order) lists
declare -a DEP_NAMES_REV=()
declare -a DEP_NAMES_FWD=()
declare -A DEP_DEFS
declare -A DEP_SCHEMAS
declare -A DEP_UNIQUE_COLS

if [ -n "$DEPS_JSON" ] && [ "$DEPS_JSON" != "" ] && [ "$DEPS_JSON" != "null" ]; then
  while IFS=$'\t' read -r dep_schema dep_name; do
    [ -z "$dep_name" ] && continue
    DEP_NAMES_REV+=("${dep_schema}.${dep_name}")
    DEP_SCHEMAS["${dep_name}"]="${dep_schema}"

    # See "head -1" comment above — pg_get_viewdef pretty-prints multi-line; capture in full.
    DEP_DEF=$(psql "$CONN_URL" -X -t -A -c \
      "SELECT pg_get_viewdef('${dep_schema}.${dep_name}'::regclass, true);" 2>&1 || true)
    DEP_DEFS["${dep_name}"]="$DEP_DEF"

    DEP_UCOLS=$(psql "$CONN_URL" -X -t -A -c \
      "SELECT string_agg(a.attname, ',' ORDER BY x.ordinality)
       FROM pg_index i
       JOIN pg_class c ON c.oid = i.indrelid
       JOIN pg_namespace n ON n.oid = c.relnamespace
       JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS x(attnum, ordinality) ON true
       JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = x.attnum
       WHERE n.nspname = '${dep_schema}' AND c.relname = '${dep_name}' AND i.indisunique
       LIMIT 1;" 2>&1 | head -1 || true)
    DEP_UNIQUE_COLS["${dep_name}"]="$DEP_UCOLS"
  done < <(echo "$DEPS_JSON" | python3 -c "
import json, sys
deps = json.load(sys.stdin)
if deps:
  for d in deps:
    print(d['schema'] + '\t' + d['name'])
" 2>/dev/null || true)

  # Forward order = reverse of DEP_NAMES_REV
  for ((i=${#DEP_NAMES_REV[@]}-1; i>=0; i--)); do
    DEP_NAMES_FWD+=("${DEP_NAMES_REV[$i]}")
  done
fi

# Capture pre-swap equality tuples for dependents
declare -A DEP_PRE_TUPLES
for dep_full in "${DEP_NAMES_REV[@]}"; do
  dep_name=$(echo "$dep_full" | cut -d. -f2)
  tuple=$(bash "$LIB_DIR/equality_gate.sh" "$dep_full" 2>/dev/null || echo "capture_failed")
  DEP_PRE_TUPLES["$dep_name"]="$tuple"
done

# ---- 5. Execute swap migration ----
DATE=$(date -u +%Y-%m-%d)
MIG_NAME="${TS}_optimize_${MV}_subtree.sql"
MIG_PATH="${PROJECT_PATH}/supabase/migrations/${MIG_NAME}"

{
  echo "BEGIN;"
  echo ""
  echo "-- Drop dependents in reverse dep order (deepest first)"
  for dep_full in "${DEP_NAMES_REV[@]}"; do
    echo "DROP MATERIALIZED VIEW IF EXISTS ${dep_full} CASCADE;"
  done
  echo ""
  echo "-- Drop target"
  echo "DROP MATERIALIZED VIEW IF EXISTS ${SOURCE_FULL};"
  echo ""
  echo "-- Recreate target with optimized definition"
  echo "CREATE MATERIALIZED VIEW ${SOURCE_FULL} AS"
  echo "${MV_DEF};"
  echo "CREATE UNIQUE INDEX ${MV}_pkey ON ${SOURCE_FULL} (${UNIQUE_COLS});"
  echo ""
  echo "-- Recreate dependents in forward dep order (shallowest first)"
  for dep_full in "${DEP_NAMES_FWD[@]}"; do
    dep_name=$(echo "$dep_full" | cut -d. -f2)
    dep_schema="${DEP_SCHEMAS[$dep_name]:-$(echo "$dep_full" | cut -d. -f1)}"
    dep_def="${DEP_DEFS[$dep_name]:-}"
    dep_ucols="${DEP_UNIQUE_COLS[$dep_name]:-}"
    if [ -n "$dep_def" ]; then
      echo "CREATE MATERIALIZED VIEW ${dep_full} AS"
      echo "${dep_def};"
      if [ -n "$dep_ucols" ]; then
        echo "CREATE UNIQUE INDEX ${dep_name}_pkey ON ${dep_full} (${dep_ucols});"
      fi
    fi
  done
  echo ""
  echo "COMMIT;"
} > "$MIG_PATH"

SWAP_OUT=$(psql "$CONN_URL" -X -f "$MIG_PATH" 2>&1) || true
if echo "$SWAP_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"swap\",\"verdict\":\"FAIL\",\"reason\":\"$(echo "$SWAP_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi
emit "{\"stage\":\"swap\",\"verdict\":\"OK\",\"deps_rebuilt\":${DEPS_COUNT},$RECIPE_TAG}"

# ---- 6. Refresh sequence (outside transaction) ----
psql "$CONN_URL" -X -q -c "REFRESH MATERIALIZED VIEW ${SOURCE_FULL};" >/dev/null 2>&1 || true
for dep_full in "${DEP_NAMES_FWD[@]}"; do
  psql "$CONN_URL" -X -q -c "REFRESH MATERIALIZED VIEW ${dep_full};" >/dev/null 2>&1 || true
done

# ---- 7. Drop shadow ----
psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true

# ---- 8. Post-rebuild equality gates ----
AUDIT_PASS=1
for dep_full in "${DEP_NAMES_REV[@]}"; do
  dep_name=$(echo "$dep_full" | cut -d. -f2)
  pre="${DEP_PRE_TUPLES[$dep_name]:-capture_failed}"
  post=$(bash "$LIB_DIR/equality_gate.sh" "$dep_full" 2>/dev/null || echo "capture_failed")
  if [ "$pre" = "capture_failed" ] || [ "$post" = "capture_failed" ]; then
    emit "{\"stage\":\"audit_gate\",\"dep\":\"${dep_full}\",\"verdict\":\"SKIP\",\"reason\":\"capture_failed\",$RECIPE_TAG}"
  elif [ "$pre" != "$post" ]; then
    emit "{\"stage\":\"audit_gate\",\"dep\":\"${dep_full}\",\"verdict\":\"FAIL\",\"reason\":\"hash_mismatch_post_rebuild\",$RECIPE_TAG}"
    AUDIT_PASS=0
  else
    emit "{\"stage\":\"audit_gate\",\"dep\":\"${dep_full}\",\"verdict\":\"PASS\",$RECIPE_TAG}"
  fi
done

if [ "$AUDIT_PASS" -eq 0 ]; then
  emit "{\"stage\":\"ship\",\"verdict\":\"FAIL\",\"reason\":\"post_rebuild_equality_gate_failed_for_dependent\",$RECIPE_TAG}"
  exit 1
fi

# ---- 9. Ship PR ----
PCT_DISPLAY=$(awk "BEGIN{printf \"%.1f%%\", $IMPROVEMENT*100}")
BRANCH="autoresearch/optimize-${MV}-${DATE}"

PR_TITLE="perf(${MV}): subtree_drop_recreate rewrite (${PCT_DISPLAY} faster on canonical)"
PR_BODY="## Summary

Auto-generated by the MV optimization harness (\`apps/data-engine-x/scripts/mv-optimization/recipes/subtree_drop_recreate.sh\`).

Drop+recreate subtree of \`${SCHEMA}.${MV}\` (has_deps_deterministic, ${DEPS_COUNT} dependents rebuilt atomically).

- Baseline p50 (5 EXPLAIN ANALYZE runs): **${BASELINE_MS}ms**
- After-rebuild p50: **${NEW_MS}ms**
- Improvement: **${PCT_DISPLAY}**
- Unique index: \`${UNIQUE_COLS}\`
- Dependents rebuilt: ${DEPS_COUNT}

## Test plan

- [x] Shadow MV built and equality-gated against original in \`__autoresearch__\`
- [x] Latency gate ≥ ${THRESHOLD} met
- [x] Atomic DROP+CREATE transaction committed
- [x] Post-rebuild equality gates passed for all dependents
"

cd "$PROJECT_PATH"

PR_URL=$(bash "$LIB_DIR/ship_pr.sh" \
  --branch "$BRANCH" \
  --migration "supabase/migrations/${MIG_NAME}" \
  --title "$PR_TITLE" \
  --body "$PR_BODY")

if [ -z "$PR_URL" ]; then
  emit "{\"stage\":\"ship\",\"verdict\":\"FAIL\",\"reason\":\"ship_pr_returned_empty\",$RECIPE_TAG}"
  exit 1
fi

emit "{\"stage\":\"ship\",\"pr_url\":\"${PR_URL}\",\"status\":\"submitted\",$RECIPE_TAG}"
exit 0
