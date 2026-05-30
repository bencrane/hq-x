#!/usr/bin/env bash
# Orchestrator: pre-flight pipeline for MV optimization batches.
#
# Runs Stages A, C, D (Stage B requires per-candidate canonical-query analysis with
# parameter binding — that step is left to the validator agent which has judgment about
# representative parameter values). Emits a markdown manifest the validator copies into
# the directive's "Validator output" section.
#
# Output: a manifest table at /tmp/mv-opt-manifest.md and a per-candidate execution-plan
# stub showing the strategy_key and recipe_chain for each.
#
# Usage:
#   Invoke from inside a doppler-configured project worktree (e.g. ~/data-engine-x).
#   The orchestrator inherits the cwd's doppler context and references SQL files
#   under its own SCRIPT_DIR.
#
#   bash apps/data-engine-x/scripts/mv-optimization/run.sh                 # top 30
#   bash apps/data-engine-x/scripts/mv-optimization/run.sh --limit 50      # custom cap
#
# Pre-reqs:
#   - cwd has doppler configured for the target project (e.g. data-engine-x/prd)
#   - DEX_DB_URL_DIRECT injects via doppler
#   - psql available

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIMIT=30
OUT="/tmp/mv-opt-manifest.md"

while [ $# -gt 0 ]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --out) OUT="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Verify doppler config in cwd (NOT in SCRIPT_DIR — doppler is per-cwd).
PROJECT=$(doppler configure get project --plain 2>/dev/null || true)
CONFIG=$(doppler configure get config --plain 2>/dev/null || true)
if [ -z "$PROJECT" ] || [ -z "$CONFIG" ]; then
  echo "ERROR: doppler not configured in cwd ($PWD)." >&2
  echo "Invoke this script from inside a project worktree (e.g. ~/data-engine-x) where" >&2
  echo "doppler.yaml pins to the right project/config." >&2
  exit 1
fi
echo "doppler: project=${PROJECT} config=${CONFIG}"
echo "raw-candidate limit: ${LIMIT}"
echo "manifest output: ${OUT}"
echo

# -------------- Stage A: candidate selection --------------
echo "== Stage A: candidate selection =="
RAW_CSV=$(mktemp)
doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -A -F '|' -t -v limit=${LIMIT} -f ${SCRIPT_DIR}/00_select_candidates.sql" \
  > "$RAW_CSV"

COUNT=$(wc -l < "$RAW_CSV" | tr -d ' ')
echo "  ${COUNT} candidates passed Stage A (has_unique_idx=true, top_query=SELECT)"
echo

if [ "$COUNT" -eq 0 ]; then
  echo "no candidates. exiting."
  exit 0
fi

# -------------- Stage C+D: per-candidate classification --------------
echo "== Stage C+D: per-candidate classification + strategy assignment =="
{
  echo "# MV Optimization Pre-Flight Manifest"
  echo
  echo "Generated: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Project: ${PROJECT} / ${CONFIG}"
  echo "Raw candidate limit: ${LIMIT}"
  echo
  echo "## Stage A: top candidates (filtered)"
  echo
  echo "| # | schema | mv | size | sum_total_exec_ms | top_mean_ms | top_query_preview |"
  echo "|---|---|---|---|---|---|---|"
} > "$OUT"

i=0
while IFS='|' read -r SCHEMA MV SIZE_PRETTY SIZE_BYTES HAS_UNIQUE_IDX SUM_MS TOP_MEAN_MS TOP_KIND TOP_PREVIEW; do
  i=$((i+1))
  # escape pipes in the preview for markdown
  PREVIEW_ESC=$(echo "$TOP_PREVIEW" | tr '|' '\\|' | tr -d '\n' | head -c 80)
  echo "| $i | $SCHEMA | $MV | $SIZE_PRETTY | $SUM_MS | $TOP_MEAN_MS | \`$PREVIEW_ESC...\` |" >> "$OUT"
done < "$RAW_CSV"

{
  echo
  echo "## Stage C+D: classification + strategy assignment"
  echo
  echo "| # | mv | deps_count | time_dependent | strategy_key | recipe_chain |"
  echo "|---|---|---|---|---|---|"
} >> "$OUT"

# Per-candidate classification
i=0
while IFS='|' read -r SCHEMA MV _SIZE_PRETTY _SIZE_BYTES _HAS_UNIQUE_IDX _SUM_MS _TOP_MEAN_MS _TOP_KIND _TOP_PREVIEW; do
  i=$((i+1))
  echo "  classifying ${SCHEMA}.${MV}..."

  CLASS_OUT=$(doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -A -F '|' -t \
    -v schema=${SCHEMA} -v mv=${MV} \
    -f ${SCRIPT_DIR}/01_classify_candidate.sql" 2>&1) || {
      echo "  classification ERRORED for ${SCHEMA}.${MV}; skipping"
      echo "| $i | ${SCHEMA}.${MV} | ERROR | ERROR | classify_failed | (manual review) |" >> "$OUT"
      continue
    }

  # 01_classify outputs three result sets; the third has the strategy.
  # tail -1 grabs the strategy assignment row (mv | deps_count | time_dependent | strategy_key | recipe_chain).
  STRATEGY_LINE=$(echo "$CLASS_OUT" | grep -E '^\"?[a-zA-Z_]+\"?\.' | tail -1 || true)
  if [ -z "$STRATEGY_LINE" ]; then
    echo "| $i | ${SCHEMA}.${MV} | ? | ? | parse_failed | (manual review) |" >> "$OUT"
    continue
  fi

  # parse: mv|deps|time_dep|strategy|chain
  IFS='|' read -r _MV DEPS TIMEDEP STRAT CHAIN <<< "$STRATEGY_LINE"
  echo "| $i | ${SCHEMA}.${MV} | ${DEPS} | ${TIMEDEP} | ${STRAT} | ${CHAIN} |" >> "$OUT"
done < "$RAW_CSV"

# -------------- Stage B placeholder --------------
{
  echo
  echo "## Stage B: canonical-query analysis (validator fills in)"
  echo
  echo "For each candidate above, the validator must:"
  echo "1. Read the top_query_preview from Stage A and pull the full canonical query from \`pg_stat_statements\`."
  echo "2. Select representative parameter values (target ~50K-200K rows scanned per the canonical's typical workload)."
  echo "3. Run \`EXPLAIN (ANALYZE, TIMING ON) EXECUTE qry(<bound>);\` and identify the operator that dominates execution time."
  echo "4. Compute self_time_pct = (time spent on this MV's relation node) / (total Execution Time). Drop candidates with self_time_pct < 30%."
  echo "5. Note the dominant operator (Seq Scan, Sort, Hash Join, etc.) — this informs the index_only recipe's index choice."
  echo
  echo "Append a Stage B subsection per candidate with:"
  echo "  - canonical query (full text)"
  echo "  - bound parameter values"
  echo "  - baseline p50 (5 EXPLAIN ANALYZE runs, median)"
  echo "  - self_time_pct"
  echo "  - dominant operator + suggested index (if applicable)"
  echo
  echo "## Stage E: optional empirical stability check"
  echo
  echo "For any candidate where Stage C reports time_dependent=false but you suspect"
  echo "non-determinism (e.g., volatile UDF), run:"
  echo "  bash 02_stability_check.sh <schema> <mv_name>"
  echo "If unstable, override the strategy_key to its time_dependent variant."
} >> "$OUT"

rm -f "$RAW_CSV"
echo
echo "manifest written to ${OUT}"
echo "next step: validator agent reads ${OUT}, completes Stage B per candidate, then drafts the directive."
