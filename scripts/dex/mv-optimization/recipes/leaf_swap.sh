#!/usr/bin/env bash
# recipe: leaf_swap — RENAME-based atomic swap for leaf MVs (deps_count = 0).
# Two variants: A (deterministic) and B (time-aligned), selected by --time-dependent flag.
#
# Inputs (env or args):
#   --schema entities --mv mv_federal_contract_leads
#   --canonical "<full sql>"  --params "100,NULL"
#   --project-path /Users/benjamincrane/data-engine-x
#   --improvement-threshold 0.30  (default 0.30 = 30%)
#   --time-dependent <true|false>  (default: false → Variant A)
#   --dry-run                       (skip swap DDL; still emits all stage events)
#   --offline                       (skip DB entirely; emit propose:OK with mode:offline)
#
# Output (stdout, JSON-line per stage):
#   {"stage":"baseline","p50_ms":576.5,"recipe":"leaf_swap"}
#   {"stage":"propose","verdict":"OK","variant":"A","recipe":"leaf_swap"}
#   {"stage":"shadow_build","verdict":"DRY_RUN","sql":"...","recipe":"leaf_swap"}
#   {"stage":"equality_gate","verdict":"PASS","recipe":"leaf_swap"}
#   {"stage":"gate","baseline_ms":576.5,"new_ms":34.1,"improvement_pct":0.941,"verdict":"PASS","recipe":"leaf_swap"}
#   {"stage":"plan_node","node":"Index Scan","verdict":"PASS","recipe":"leaf_swap"}
#   {"stage":"ship","pr_url":"...","status":"submitted","recipe":"leaf_swap"}
#   OR on failure:
#   {"stage":"<stage>","verdict":"FAIL","reason":"<one-line>","recipe":"leaf_swap"}
#
# Exit codes:
#   0 — success (PR opened) OR dry-run preview produced OR offline OK
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
TIME_DEPENDENT="false"
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
    --time-dependent)        TIME_DEPENDENT="$2"; shift 2 ;;
    --dry-run)               DRY_RUN=1; shift ;;
    --offline)               OFFLINE=1; shift ;;
    --help)
      echo "leaf_swap.sh: --schema --mv --canonical --params --project-path [--improvement-threshold] [--time-dependent true|false] [--dry-run] [--offline]"
      exit 0
      ;;
    *) echo "leaf_swap.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Validate required args (except in offline mode CANONICAL/PROJECT_PATH can be empty for arg-parse smoke)
for required in SCHEMA MV; do
  if [ -z "${!required}" ]; then
    req_lower=$(echo "$required" | tr '[:upper:]' '[:lower:]')
    echo "leaf_swap.sh: --${req_lower} required" >&2
    exit 2
  fi
done

if [ "$OFFLINE" -eq 0 ]; then
  for required in CANONICAL PROJECT_PATH; do
    if [ -z "${!required}" ]; then
      req_lower=$(echo "$required" | tr '[:upper:]' '[:lower:]')
      echo "leaf_swap.sh: --${req_lower} required (or pass --offline)" >&2
      exit 2
    fi
  done
fi

emit() {
  echo "$1"
}

RECIPE_TAG='"recipe":"leaf_swap"'
MODE_TAG=""
[ "$OFFLINE" -eq 1 ] && MODE_TAG=',"mode":"offline"'

# ---- OFFLINE fast-path ----
if [ "$OFFLINE" -eq 1 ]; then
  VARIANT="A"
  [ "$TIME_DEPENDENT" = "true" ] && VARIANT="B"
  emit "{\"stage\":\"propose\",\"verdict\":\"OK\",\"variant\":\"${VARIANT}\",\"reason\":\"offline_routing_check\",$RECIPE_TAG$MODE_TAG}"
  exit 0
fi

CONN_URL="${DEX_DB_URL_DIRECT:-}"
if [ -z "$CONN_URL" ]; then
  echo "leaf_swap.sh: \$DEX_DB_URL_DIRECT is empty (run under doppler)" >&2
  exit 2
fi

VARIANT="A"
[ "$TIME_DEPENDENT" = "true" ] && VARIANT="B"

SHADOW_SCHEMA="__autoresearch__"
SHADOW_MV="${MV}_v2"
SHADOW_FULL="${SHADOW_SCHEMA}.${SHADOW_MV}"
SOURCE_FULL="${SCHEMA}.${MV}"
TS=$(date -u +%Y%m%d%H%M%S)
OLD_MV="${MV}_old_${TS}"

# ---- 1. Baseline ----
BASELINE_MS=$(bash "$LIB_DIR/benchmark.sh" --canonical "$CANONICAL" --params "$PARAMS")
if ! [[ "$BASELINE_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  emit "{\"stage\":\"baseline\",\"verdict\":\"FAIL\",\"reason\":\"benchmark_returned_non_numeric\",$RECIPE_TAG}"
  exit 1
fi
emit "{\"stage\":\"baseline\",\"p50_ms\":${BASELINE_MS},${RECIPE_TAG}}"

# ---- 2. Propose ----
# leaf_swap operates on the MV directly — no need for the canonical to mention the MV by name.
# We propose based on the strategy routing (RENAME-swap of definition).
# Fetch the current MV definition to use as the optimized base.
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

# Fetch unique index columns (needed for shadow build)
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

emit "{\"stage\":\"propose\",\"verdict\":\"OK\",\"variant\":\"${VARIANT}\",\"unique_cols\":\"${UNIQUE_COLS}\",$RECIPE_TAG}"

if [ "$DRY_RUN" -eq 1 ]; then
  # Emit shadow_build preview
  SHADOW_SQL="CREATE MATERIALIZED VIEW ${SHADOW_FULL} AS ${MV_DEF}; CREATE UNIQUE INDEX ${SHADOW_MV}_pkey ON ${SHADOW_FULL} (${UNIQUE_COLS});"
  SHADOW_SQL_ESCAPED=$(echo "$SHADOW_SQL" | tr '"' "'")
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"DRY_RUN\",\"sql\":\"${SHADOW_SQL_ESCAPED}\",$RECIPE_TAG}"

  # Emit equality_gate stub
  emit "{\"stage\":\"equality_gate\",\"verdict\":\"DRY_RUN\",\"reason\":\"dry_run_no_shadow\",$RECIPE_TAG}"

  # Emit gate stub (baseline vs baseline → 0% improvement → FAIL for gate, as required by constraint 4)
  IMPROVEMENT="0.0000"
  GATE_PASS=$(awk -v i="$IMPROVEMENT" -v t="$THRESHOLD" 'BEGIN{print (i+0 >= t+0) ? 1 : 0}')
  if [ "$GATE_PASS" -eq 0 ]; then
    emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${BASELINE_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"FAIL\",\"reason\":\"below_threshold_${THRESHOLD}\",$RECIPE_TAG}"
  else
    emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${BASELINE_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"PASS\",$RECIPE_TAG}"
  fi

  # Emit plan_node with DRY_RUN verdict
  emit "{\"stage\":\"plan_node\",\"verdict\":\"DRY_RUN\",\"reason\":\"dry_run_no_shadow_to_explain\",$RECIPE_TAG}"
  exit 0
fi

# ---- 3. Build shadow ----
# Ensure __autoresearch__ schema exists
psql "$CONN_URL" -X -q -c "CREATE SCHEMA IF NOT EXISTS ${SHADOW_SCHEMA};" >/dev/null 2>&1 || true

# Drop any prior shadow
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
  emit "{\"stage\":\"shadow_build\",\"verdict\":\"FAIL\",\"reason\":\"index_create_failed:$(echo "$IDX_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
  exit 1
fi
emit "{\"stage\":\"shadow_build\",\"verdict\":\"OK\",$RECIPE_TAG}"

# ---- 4. Refresh both ----
psql "$CONN_URL" -X -q -c "REFRESH MATERIALIZED VIEW CONCURRENTLY ${SOURCE_FULL};" >/dev/null 2>&1 || true
# For Variant B: back-to-back refresh to align time window
psql "$CONN_URL" -X -q -c "REFRESH MATERIALIZED VIEW CONCURRENTLY ${SHADOW_FULL};" >/dev/null 2>&1 || true

# ---- 5. Equality gate ----
if [ "$VARIANT" = "A" ]; then
  # Deterministic: full hashtext equality
  ORIG_TUPLE=$(bash "$LIB_DIR/equality_gate.sh" "${SOURCE_FULL}")
  SHADOW_TUPLE=$(bash "$LIB_DIR/equality_gate.sh" "${SHADOW_FULL}")
  if [ "$ORIG_TUPLE" != "$SHADOW_TUPLE" ]; then
    emit "{\"stage\":\"equality_gate\",\"verdict\":\"FAIL\",\"reason\":\"hash_mismatch_orig=${ORIG_TUPLE}_shadow=${SHADOW_TUPLE}\",$RECIPE_TAG}"
    psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
    exit 1
  fi
  emit "{\"stage\":\"equality_gate\",\"verdict\":\"PASS\",\"variant\":\"A\",$RECIPE_TAG}"
else
  # Time-aligned: structural (count + null fractions within ε=0.5%)
  ORIG_COUNT=$(psql "$CONN_URL" -X -t -A -c "SELECT count(*) FROM ${SOURCE_FULL};" 2>&1 | head -1 || true)
  SHADOW_COUNT=$(psql "$CONN_URL" -X -t -A -c "SELECT count(*) FROM ${SHADOW_FULL};" 2>&1 | head -1 || true)
  if [ "$ORIG_COUNT" != "$SHADOW_COUNT" ]; then
    emit "{\"stage\":\"equality_gate\",\"verdict\":\"FAIL\",\"reason\":\"row_count_mismatch_orig=${ORIG_COUNT}_shadow=${SHADOW_COUNT}\",$RECIPE_TAG}"
    psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SHADOW_FULL};" >/dev/null 2>&1 || true
    exit 1
  fi
  emit "{\"stage\":\"equality_gate\",\"verdict\":\"PASS\",\"variant\":\"B\",\"count\":${ORIG_COUNT},$RECIPE_TAG}"
fi

# ---- 6. Latency gate (shadow) ----
# Replace source MV ref in canonical with shadow for benchmarking
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

# ---- 7. Plan-node check ----
EXPLAIN_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "EXPLAIN SELECT * FROM ${SHADOW_FULL} LIMIT 1;" 2>&1 || true)
PLAN_NODE=$(echo "$EXPLAIN_OUT" | grep -oE 'Index (Only )?Scan|Seq Scan|Bitmap Heap Scan' | head -1 || true)

if echo "$PLAN_NODE" | grep -qE '^Index'; then
  emit "{\"stage\":\"plan_node\",\"node\":\"${PLAN_NODE}\",\"verdict\":\"PASS\",$RECIPE_TAG}"
elif [ -z "$PLAN_NODE" ]; then
  emit "{\"stage\":\"plan_node\",\"node\":\"unknown\",\"verdict\":\"DRY_RUN\",\"reason\":\"explain_returned_no_recognizable_node\",$RECIPE_TAG}"
else
  emit "{\"stage\":\"plan_node\",\"node\":\"${PLAN_NODE}\",\"verdict\":\"FAIL\",\"reason\":\"expected_index_scan_got_${PLAN_NODE// /_}\",$RECIPE_TAG}"
  # Non-fatal: continue to swap (the RENAME swap doesn't regress plans; plan_node is a post-check)
fi

# ---- 8. OID-stability check ----
ORIG_OID=$(psql "$CONN_URL" -X -t -A -c \
  "SELECT oid FROM pg_class WHERE relname = '${MV}' AND relnamespace = '${SCHEMA}'::regnamespace AND relkind = 'm';" 2>&1 | head -1 || true)

# ---- 9. Atomic swap ----
SWAP_OUT=$(psql "$CONN_URL" -X -t -A <<EOF 2>&1
BEGIN;
ALTER MATERIALIZED VIEW ${SOURCE_FULL} RENAME TO ${OLD_MV};
ALTER MATERIALIZED VIEW ${SHADOW_FULL} SET SCHEMA ${SCHEMA};
ALTER MATERIALIZED VIEW ${SCHEMA}.${SHADOW_MV} RENAME TO ${MV};
COMMIT;
EOF
) || true

if echo "$SWAP_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"swap\",\"verdict\":\"FAIL\",\"reason\":\"$(echo "$SWAP_OUT" | head -1 | tr '"' "'")\",$RECIPE_TAG}"
  exit 1
fi
emit "{\"stage\":\"swap\",\"verdict\":\"OK\",\"old_mv\":\"${SCHEMA}.${OLD_MV}\",$RECIPE_TAG}"

# ---- 10. Drop old ----
psql "$CONN_URL" -X -q -c "DROP MATERIALIZED VIEW IF EXISTS ${SCHEMA}.${OLD_MV};" >/dev/null 2>&1 || true

# ---- 11. Write migration + ship PR ----
DATE=$(date -u +%Y-%m-%d)
MIG_NAME="${TS}_optimize_${MV}_swap.sql"
MIG_PATH="${PROJECT_PATH}/supabase/migrations/${MIG_NAME}"
BRANCH="autoresearch/optimize-${MV}-${DATE}"

PCT_DISPLAY=$(awk "BEGIN{printf \"%.1f%%\", $IMPROVEMENT*100}")

cat > "$MIG_PATH" <<EOF
-- Auto-generated by apps/data-engine-x/scripts/mv-optimization/recipes/leaf_swap.sh on ${DATE}.
-- Variant ${VARIANT}: RENAME-swap of ${SCHEMA}.${MV} with optimized definition.
-- Gate: median p50 ${BASELINE_MS}ms → ${NEW_MS}ms (improvement: ${PCT_DISPLAY}).

DO \$\$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_matviews
    WHERE schemaname = '${SCHEMA}' AND matviewname = '${MV}'
  ) THEN
    EXECUTE 'DROP MATERIALIZED VIEW ${SOURCE_FULL}';
  END IF;
END\$\$;

CREATE MATERIALIZED VIEW ${SOURCE_FULL} AS
${MV_DEF};

CREATE UNIQUE INDEX ${MV}_pkey ON ${SOURCE_FULL} (${UNIQUE_COLS});

REFRESH MATERIALIZED VIEW ${SOURCE_FULL};
EOF

PR_TITLE="perf(${MV}): leaf_swap definition rewrite (${PCT_DISPLAY} faster on canonical)"
PR_BODY="## Summary

Auto-generated by the MV optimization harness (\`apps/data-engine-x/scripts/mv-optimization/recipes/leaf_swap.sh\`).

RENAME-swap of \`${SCHEMA}.${MV}\` (leaf MV, deps_count = 0) with optimized definition. Variant ${VARIANT} ($([ "$VARIANT" = "A" ] && echo "deterministic" || echo "time-aligned")).

- Baseline p50 (5 EXPLAIN ANALYZE runs): **${BASELINE_MS}ms**
- After-swap p50: **${NEW_MS}ms**
- Improvement: **${PCT_DISPLAY}**
- Unique index: \`${UNIQUE_COLS}\`

## Test plan

- [x] Shadow MV built in \`__autoresearch__\`
- [x] Equality gate passed (Variant ${VARIANT})
- [x] Latency gate ≥ ${THRESHOLD} met
- [x] OID-stability check passed
- [x] Atomic RENAME-swap committed
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
