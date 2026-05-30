#!/usr/bin/env bash
# recipe: index_only — fully automated.
# Add an index to an existing MV based on heuristics, gate on ≥30% improvement, ship PR.
#
# This is the universal first-try recipe. Safe regardless of deps or time-dependence.
#
# Inputs (env or args):
#   --schema entities --mv mv_fmcsa_authority_grants
#   --canonical "<full sql>"  --params "100,NULL"
#   --project-path /Users/benjamincrane/data-engine-x
#   --improvement-threshold 0.30  (default 0.30 = 30%)
#   --dry-run                       (skip ship_pr; print what would happen)
#
# Output (stdout, JSON-line per stage):
#   {"stage":"baseline","p50_ms":576.5}
#   {"stage":"propose","verdict":"OK","kind":"btree","columns":"final_authority_decision_date"}
#   {"stage":"build","index_name":"idx_mv_fmcsa_ag_final_dec_date","seconds":47.2}
#   {"stage":"gate","baseline_ms":576.5,"new_ms":34.1,"improvement_pct":0.941,"verdict":"PASS"}
#   {"stage":"ship","pr_url":"https://github.com/.../pull/156","status":"submitted"}
#   OR on any failure:
#   {"stage":"<stage>","verdict":"FAIL","reason":"<one-line>"}
#
# Exit codes:
#   0 — success (PR opened) OR dry-run preview produced
#   1 — recipe failed (one of the stages reported FAIL)
#   2 — argument error / pre-flight failure
#
# This script makes mutations to prod:
#   - Creates an index via CREATE INDEX CONCURRENTLY (non-blocking but lasting)
#   - On gate failure, drops the index via DROP INDEX CONCURRENTLY
#   - On gate pass, leaves the index in place AND opens an auto-merge PR with the matching migration

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

while [ $# -gt 0 ]; do
  case "$1" in
    --schema)                SCHEMA="$2"; shift 2 ;;
    --mv)                    MV="$2"; shift 2 ;;
    --canonical)             CANONICAL="$2"; shift 2 ;;
    --params)                PARAMS="$2"; shift 2 ;;
    --project-path)          PROJECT_PATH="$2"; shift 2 ;;
    --improvement-threshold) THRESHOLD="$2"; shift 2 ;;
    --dry-run)               DRY_RUN=1; shift ;;
    *) echo "index_only.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

for required in SCHEMA MV CANONICAL PROJECT_PATH; do
  if [ -z "${!required}" ]; then
    echo "index_only.sh: --${required,,} required" >&2
    exit 2
  fi
done

CONN_URL="${DEX_DB_URL_DIRECT:-}"
if [ -z "$CONN_URL" ]; then
  echo "index_only.sh: \$DEX_DB_URL_DIRECT is empty (run under doppler)" >&2
  exit 2
fi

emit() {
  echo "$1"
}

# ---- 1. baseline ----
BASELINE_MS=$(bash "$LIB_DIR/benchmark.sh" --canonical "$CANONICAL" --params "$PARAMS")
if ! [[ "$BASELINE_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  emit '{"stage":"baseline","verdict":"FAIL","reason":"benchmark_returned_non_numeric"}'
  exit 1
fi
emit "{\"stage\":\"baseline\",\"p50_ms\":${BASELINE_MS}}"

# ---- 2. propose index ----
PROP=$(bash "$LIB_DIR/propose_index.sh" \
  --schema "$SCHEMA" --mv "$MV" \
  --canonical "$CANONICAL" --params "$PARAMS")

if [[ "$PROP" == SKIP* ]]; then
  REASON=$(echo "$PROP" | cut -d'|' -f2)
  emit "{\"stage\":\"propose\",\"verdict\":\"SKIP\",\"reason\":\"${REASON}\"}"
  exit 1
fi

if [[ "$PROP" != OK* ]]; then
  emit "{\"stage\":\"propose\",\"verdict\":\"FAIL\",\"reason\":\"propose_returned:${PROP}\"}"
  exit 1
fi

KIND=$(echo "$PROP" | cut -d'|' -f2)
COLUMNS=$(echo "$PROP" | cut -d'|' -f3)
emit "{\"stage\":\"propose\",\"verdict\":\"OK\",\"kind\":\"${KIND}\",\"columns\":\"${COLUMNS}\"}"

# Build index name. Truncate to PG's 63-char identifier limit.
COL_SHORT=$(echo "$COLUMNS" | tr ',' '_' | tr -d ' ' | cut -c1-30)
MV_SHORT=$(echo "$MV" | sed 's/^mv_//' | cut -c1-25)
IDX_NAME="idx_mv_${MV_SHORT}_${COL_SHORT}"
IDX_NAME=$(echo "$IDX_NAME" | cut -c1-63)

if [ "$DRY_RUN" -eq 1 ]; then
  emit "{\"stage\":\"build\",\"verdict\":\"DRY_RUN\",\"index_name\":\"${IDX_NAME}\",\"sql\":\"CREATE INDEX CONCURRENTLY IF NOT EXISTS ${IDX_NAME} ON ${SCHEMA}.${MV} USING ${KIND} (${COLUMNS});\"}"
  exit 0
fi

# ---- 3. build index ----
BUILD_START=$(date +%s)
BUILD_OUT=$(psql "$CONN_URL" -X -t -A -c \
  "CREATE INDEX CONCURRENTLY IF NOT EXISTS ${IDX_NAME} ON ${SCHEMA}.${MV} USING ${KIND} (${COLUMNS});" 2>&1) || true
BUILD_END=$(date +%s)
BUILD_SECONDS=$((BUILD_END - BUILD_START))

if echo "$BUILD_OUT" | grep -qE '^ERROR|^FATAL'; then
  emit "{\"stage\":\"build\",\"verdict\":\"FAIL\",\"reason\":\"$(echo "$BUILD_OUT" | head -1 | tr '"' '\047')\"}"
  exit 1
fi
emit "{\"stage\":\"build\",\"index_name\":\"${IDX_NAME}\",\"seconds\":${BUILD_SECONDS}}"

# ---- 4. re-benchmark ----
NEW_MS=$(bash "$LIB_DIR/benchmark.sh" --canonical "$CANONICAL" --params "$PARAMS")
if ! [[ "$NEW_MS" =~ ^[0-9]+(\.[0-9]+)?$ ]]; then
  # rollback the index
  psql "$CONN_URL" -X -q -c "DROP INDEX CONCURRENTLY IF EXISTS ${SCHEMA}.${IDX_NAME};" >/dev/null 2>&1 || true
  emit '{"stage":"rebenchmark","verdict":"FAIL","reason":"benchmark_returned_non_numeric_after_index"}'
  exit 1
fi

# ---- 5. gate ----
IMPROVEMENT=$(awk -v b="$BASELINE_MS" -v n="$NEW_MS" 'BEGIN{if (b==0) {print 0} else {printf "%.4f", (b-n)/b}}')
GATE_PASS=$(awk -v i="$IMPROVEMENT" -v t="$THRESHOLD" 'BEGIN{print (i+0 >= t+0) ? 1 : 0}')

if [ "$GATE_PASS" -eq 0 ]; then
  # Improvement insufficient — rollback the index, recipe failed.
  psql "$CONN_URL" -X -q -c "DROP INDEX CONCURRENTLY IF EXISTS ${SCHEMA}.${IDX_NAME};" >/dev/null 2>&1 || true
  emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${NEW_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"FAIL\",\"reason\":\"below_threshold_${THRESHOLD}\"}"
  exit 1
fi
emit "{\"stage\":\"gate\",\"baseline_ms\":${BASELINE_MS},\"new_ms\":${NEW_MS},\"improvement_pct\":${IMPROVEMENT},\"verdict\":\"PASS\"}"

# ---- 6. write migration + ship PR ----
TS=$(date -u +%Y%m%d%H%M%S)
DATE=$(date -u +%Y-%m-%d)
MIG_NAME="${TS}_optimize_${MV}_index.sql"
MIG_PATH="${PROJECT_PATH}/supabase/migrations/${MIG_NAME}"
BRANCH="autoresearch/optimize-${MV}-${DATE}"

cat > "$MIG_PATH" <<EOF
-- Auto-generated by apps/data-engine-x/scripts/mv-optimization/recipes/index_only.sh on ${DATE}.
-- Adds an index to ${SCHEMA}.${MV} based on canonical-query EXPLAIN analysis.
-- Gate: median p50 ${BASELINE_MS}ms → ${NEW_MS}ms (improvement: $(awk "BEGIN{printf \"%.1f%%\", $IMPROVEMENT*100}")).

CREATE INDEX IF NOT EXISTS ${IDX_NAME}
  ON ${SCHEMA}.${MV} USING ${KIND} (${COLUMNS});
EOF

PCT_DISPLAY=$(awk "BEGIN{printf \"%.1f%%\", $IMPROVEMENT*100}")
PR_TITLE="perf(${MV}): add ${IDX_NAME} (${PCT_DISPLAY} faster on canonical)"
PR_BODY="## Summary

Auto-generated by the MV optimization harness ([\`apps/data-engine-x/scripts/mv-optimization/recipes/index_only.sh\`](https://github.com/bencrane/hq-all/tree/main/apps/data-engine-x/scripts/mv-optimization)).

Adds a single btree index to \`${SCHEMA}.${MV}\` to speed up the canonical downstream query. Index applied to prod during validation.

- Baseline p50 (5 EXPLAIN ANALYZE runs): **${BASELINE_MS}ms**
- After-index p50: **${NEW_MS}ms**
- Improvement: **${PCT_DISPLAY}**

## Index

\`\`\`sql
CREATE INDEX IF NOT EXISTS ${IDX_NAME}
  ON ${SCHEMA}.${MV} USING ${KIND} (${COLUMNS});
\`\`\`

## Test plan

- [x] Index applied to prod during validation
- [x] Canonical query benchmarked 5× before / 5× after; median compared
- [x] Improvement ≥ 30% threshold met
"

cd "$PROJECT_PATH"

PR_URL=$(bash "$LIB_DIR/ship_pr.sh" \
  --branch "$BRANCH" \
  --migration "supabase/migrations/${MIG_NAME}" \
  --title "$PR_TITLE" \
  --body "$PR_BODY")

if [ -z "$PR_URL" ]; then
  emit "{\"stage\":\"ship\",\"verdict\":\"FAIL\",\"reason\":\"ship_pr_returned_empty\"}"
  exit 1
fi

emit "{\"stage\":\"ship\",\"pr_url\":\"${PR_URL}\",\"status\":\"submitted\"}"
exit 0
