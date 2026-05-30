#!/usr/bin/env bash
# orchestrate.sh — top-level entry point for an MV optimization batch.
#
# Single-message kickoff equivalent. Behaves like a hook: parameters in, side-effects out
# (PRs opened, report written), no model in the loop for the deterministic stages.
#
# What it does:
#   1. Pre-flight (Stage A+C+D) via run.sh → manifest at /tmp/mv-opt-manifest.md
#   2. Per-candidate canonical lookup from pg_stat_statements (Stage B, automated)
#   3. For each candidate: try index_only.sh recipe (always-first-try)
#   4. Aggregate results into ~/Desktop/hq/reports/{date}-mv-optimization-batch.md
#
# What it does NOT do:
#   - Stage B for canonicals with non-trivial parameter binding (surfaced for review)
#
# index_only zero-stages fallback:
#   When index_only emits zero JSONL stages (not even a propose:SKIP), the orchestrator
#   sets NEEDS_FALLBACK=1 and routes to the strategy-mapped fallback recipe (leaf_swap or
#   subtree_drop_recreate), matching the existing propose:SKIP / gate:FAIL dispatch path.
#   A dispatch:FAIL JSONL row is emitted so the run report captures the candidate rather
#   than silently incrementing FAILED. Unknown strategies emit a dispatch:SKIP row.
#
# Usage:
#   bash orchestrate.sh \
#     --limit 10 \
#     --improvement-threshold 0.30 \
#     [--dry-run] \
#     [--candidates "entities.mv_x,entities.mv_y"]
#
# By default the orchestrator creates a temporary git worktree off
# ~/hq-all's origin/main, runs the batch inside it, and removes it on exit.
# This isolates the batch from the canonical clone's "always on main"
# invariant (ADR 0001) and from the working clone's branch state.
#
# --project-path <path> is a hidden override for debugging — when supplied,
# the orchestrator runs inside that path as-is (no worktree, no cleanup).
# Most callers should NOT pass it.
#
# Pre-reqs:
#   - ~/hq-all/ exists as a working clone of bencrane/hq-all
#   - apps/data-engine-x/doppler.yaml is tracked on main (configures doppler from cwd)
#   - jq available
#   - gh authenticated for bencrane/hq-all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_PATH=""
LIMIT="10"
THRESHOLD="0.30"
DRY_RUN=0
PINNED=""

while [ $# -gt 0 ]; do
  case "$1" in
    --project-path)          PROJECT_PATH="$2"; shift 2 ;;
    --limit)                 LIMIT="$2"; shift 2 ;;
    --improvement-threshold) THRESHOLD="$2"; shift 2 ;;
    --dry-run)               DRY_RUN=1; shift ;;
    --candidates)            PINNED="$2"; shift 2 ;;
    *) echo "orchestrate.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

HQ_ALL_CLONE="${HQ_ALL_CLONE:-$HOME/hq-all}"
AUTO_WORKTREE=0
WORKTREE_DIR=""

if [ -z "$PROJECT_PATH" ]; then
  # Auto-create a worktree off ~/hq-all/origin/main. Isolates the batch from
  # the canonical clone's "always on main" invariant and the working clone's
  # branch state. See ADR 0001.
  if [ ! -d "$HQ_ALL_CLONE/.git" ]; then
    echo "orchestrate.sh: $HQ_ALL_CLONE is not a git working clone" >&2
    echo "  set HQ_ALL_CLONE=<path> or pass --project-path explicitly" >&2
    exit 2
  fi
  echo "[worktree] fetching origin/main in $HQ_ALL_CLONE" >&2
  git -C "$HQ_ALL_CLONE" fetch origin main --quiet
  WORKTREE_DIR=$(mktemp -d /tmp/mv-opt-worktree.XXXXXX)
  rmdir "$WORKTREE_DIR"  # git worktree add wants a non-existent path
  git -C "$HQ_ALL_CLONE" worktree add --detach "$WORKTREE_DIR" origin/main >&2
  PROJECT_PATH="$WORKTREE_DIR/apps/data-engine-x"
  AUTO_WORKTREE=1
  echo "[worktree] $WORKTREE_DIR (detached at origin/main)" >&2
fi

if [ ! -d "$PROJECT_PATH" ]; then
  echo "orchestrate.sh: project path does not exist: $PROJECT_PATH" >&2
  exit 2
fi

# Always operate inside the project worktree so doppler resolves correctly.
cd "$PROJECT_PATH"

# Verify doppler config in this worktree.
DOPP_PROJECT=$(doppler configure get project --plain 2>/dev/null || true)
DOPP_CONFIG=$(doppler configure get config --plain 2>/dev/null || true)
if [ -z "$DOPP_PROJECT" ] || [ -z "$DOPP_CONFIG" ]; then
  echo "orchestrate.sh: doppler not configured in $PROJECT_PATH" >&2
  echo "  Run: cd $PROJECT_PATH && doppler setup --no-interactive" >&2
  exit 2
fi

# Verify gh auth (skipped on dry-run since no PR is opened)
if [ "$DRY_RUN" -eq 0 ]; then
  if ! gh auth status >/dev/null 2>&1; then
    echo "orchestrate.sh: gh not authenticated" >&2
    exit 2
  fi
fi

DATE=$(date -u +%Y-%m-%d)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RESULTS_LOG="/tmp/mv-opt-results-${DATE}.jsonl"
REPORT_PATH="${HOME}/Desktop/hq/reports/${DATE}-mv-optimization-batch.md"
MANIFEST_PATH="/tmp/mv-opt-manifest-${DATE}.md"

: > "$RESULTS_LOG"  # truncate

write_final_report() {
  # Overwrites on every call — each run regenerates the dated report from the current RESULTS_LOG.
  # The EXIT trap ensures this fires even on early abort; calling it explicitly at end of run
  # is also safe (trap fires too but file is already written — RESULTS_LOG is complete by then).
  local _applied="${APPLIED:-0}"
  local _skipped_perf="${SKIPPED_PERF:-0}"
  local _skipped_recipe="${SKIPPED_RECIPE:-0}"
  local _skipped_other="${SKIPPED_OTHER:-0}"
  local _failed="${FAILED:-0}"
  local _candidate_count="${CANDIDATE_COUNT:-0}"
  {
    echo "# Report: MV Optimization Batch — ${DATE}"
    echo
    echo "**Status:** completed ${TIMESTAMP}"
    echo "**Driver:** \`apps/data-engine-x/scripts/mv-optimization/orchestrate.sh\` (no model in loop)"
    echo "**Project:** ${DOPP_PROJECT:-unknown}/${DOPP_CONFIG:-unknown} (\`${PROJECT_PATH}\`)"
    echo "**Dry run:** ${DRY_RUN}"
    echo
    echo "## Summary"
    echo
    echo "- candidates_processed: ${_candidate_count}"
    echo "- applied: ${_applied}"
    echo "- skipped (performance below threshold): ${_skipped_perf}"
    echo "- skipped (recipe not yet automated): ${_skipped_recipe}"
    echo "- skipped (other): ${_skipped_other}"
    echo "- failed: ${_failed}"
    echo
    if [ ${#PR_URLS[@]:-0} -gt 0 ]; then
      echo "## PRs opened (auto-merge enabled)"
      echo
      for url in "${PR_URLS[@]}"; do
        echo "- ${url}"
      done
      echo
    fi
    echo "## Per-candidate results (JSONL)"
    echo
    echo "Full machine-readable log: \`${RESULTS_LOG}\`"
    echo
    echo "\`\`\`jsonl"
    cat "$RESULTS_LOG" 2>/dev/null || true
    echo "\`\`\`"
    echo
    echo "## Manifest"
    echo
    echo "Stage A+C+D pre-flight manifest: \`${MANIFEST_PATH}\`"
  } > "$REPORT_PATH"
}
cleanup_worktree() {
  if [ "${AUTO_WORKTREE:-0}" -eq 1 ] && [ -n "${WORKTREE_DIR:-}" ]; then
    git -C "$HQ_ALL_CLONE" worktree remove --force "$WORKTREE_DIR" 2>/dev/null || true
    rm -rf "$WORKTREE_DIR" 2>/dev/null || true
  fi
}
trap 'write_final_report; cleanup_worktree' EXIT

echo "=== MV Optimization Batch ===" >&2
echo "  project: ${DOPP_PROJECT}/${DOPP_CONFIG} ($PROJECT_PATH)" >&2
echo "  limit: ${LIMIT}" >&2
echo "  threshold: ${THRESHOLD}" >&2
echo "  dry-run: ${DRY_RUN}" >&2
echo "  pinned: ${PINNED:-(auto-select)}" >&2
echo "  results log: ${RESULTS_LOG}" >&2
echo "  final report: ${REPORT_PATH}" >&2
echo >&2

# ---- Stage A+C+D via existing run.sh ----
echo "== Stage A+C+D: pre-flight pipeline ==" >&2
bash "$SCRIPT_DIR/run.sh" --limit "$LIMIT" --out "$MANIFEST_PATH" >&2

if [ ! -f "$MANIFEST_PATH" ]; then
  echo "orchestrate.sh: run.sh did not produce a manifest" >&2
  exit 1
fi

# ---- Build candidate list ----
# Parse the strategy table out of the manifest.
# Lines that match:  | N | entities.mv_x | deps | tdep | strategy_key | recipe_chain |
ALL_CANDIDATES=$(awk '
  /^## Stage C\+D:/ {flag=1; next}
  /^## / && flag {flag=0}
  flag && /^\| [0-9]+ \|/ { print }
' "$MANIFEST_PATH")

if [ -z "$ALL_CANDIDATES" ]; then
  echo "orchestrate.sh: no candidates parsed from manifest" >&2
  exit 1
fi

# If --candidates given, filter
if [ -n "$PINNED" ]; then
  IFS=',' read -ra PIN_ARR <<< "$PINNED"
  FILTERED=""
  while IFS= read -r line; do
    for pin in "${PIN_ARR[@]}"; do
      if echo "$line" | grep -qF "$pin"; then
        FILTERED+="$line"$'\n'
        break
      fi
    done
  done <<< "$ALL_CANDIDATES"
  CANDIDATES="$FILTERED"
else
  CANDIDATES="$ALL_CANDIDATES"
fi

CANDIDATE_COUNT=$(echo "$CANDIDATES" | grep -c '^|')
echo "  → ${CANDIDATE_COUNT} candidates to process" >&2
echo >&2

# ---- Per-candidate processing ----
APPLIED=0
SKIPPED_PERF=0
SKIPPED_RECIPE=0
SKIPPED_OTHER=0
FAILED=0
PR_URLS=()

# run_recipe: invoke a recipe script with shared args; appends JSONL to RESULTS_LOG; echos last-stage JSON
run_recipe() {
  local recipe_script="$1"; local mv_full="$2"; local strategy="$3"
  local schema="$4"; local mv="$5"; local canonical="$6"; local params="$7"
  local out
  if [ "$DRY_RUN" -eq 1 ]; then
    out=$(doppler run -- bash "$recipe_script" \
      --schema "$schema" --mv "$mv" \
      --canonical "$canonical" --params "$params" \
      --project-path "$PROJECT_PATH" \
      --improvement-threshold "$THRESHOLD" \
      --dry-run 2>&1) || true
  else
    out=$(doppler run -- bash "$recipe_script" \
      --schema "$schema" --mv "$mv" \
      --canonical "$canonical" --params "$params" \
      --project-path "$PROJECT_PATH" \
      --improvement-threshold "$THRESHOLD" 2>&1) || true
  fi
  while IFS= read -r recipe_line; do
    if echo "$recipe_line" | grep -qE '^\{'; then
      local enriched
      enriched=$(echo "$recipe_line" | jq -c --arg c "$mv_full" --arg s "$strategy" '. + {candidate:$c, strategy:$s}')
      echo "$enriched" >> "$RESULTS_LOG"
    fi
  done <<< "$out"
  echo "$out" | grep -E '^\{' | tail -1
}

process_one_candidate() {
  set +e  # Do NOT propagate errexit through the function; caller uses ||
  local LINE="$1"
  local MV_FULL STRATEGY SCHEMA MV

  # Parse: | N | entities.mv_x | deps | tdep | strategy | chain |
  MV_FULL=$(echo "$LINE" | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
  STRATEGY=$(echo "$LINE" | awk -F'|' '{gsub(/^ +| +$/,"",$6); print $6}')
  SCHEMA=$(echo "$MV_FULL" | cut -d. -f1)
  MV=$(echo "$MV_FULL" | cut -d. -f2)

  echo "  [processing] ${MV_FULL} (strategy=${STRATEGY})" >&2

  # ---- Stage B: pull canonical query, pick default params ----
  local TMP_SQL
  TMP_SQL=$(mktemp)
  cat > "$TMP_SQL" <<EOF
SELECT regexp_replace(query, E'[\n\r\t]+', ' ', 'g')
FROM pg_stat_statements
WHERE query LIKE '%${MV}%'
  AND query ~* '^\s*SELECT'
ORDER BY total_exec_time DESC
LIMIT 1;
EOF
  local CANONICAL
  CANONICAL=$(doppler run -- bash -c "psql \"\$DEX_DB_URL_DIRECT\" -X -t -A -f $TMP_SQL" 2>&1 | head -1 || true)
  rm -f "$TMP_SQL"

  if [ -n "$CANONICAL" ]; then
    CANONICAL=$(echo "$CANONICAL" | perl -pe '
      s/\binterval\s+\$(\d+)/(\$$1)::interval/g;
      s/\bnumeric\s+\$(\d+)/(\$$1)::numeric/g;
      s/\btimestamp\s+\$(\d+)/(\$$1)::timestamp/g;
      s/\bdate\s+\$(\d+)/(\$$1)::date/g;
      s/\btime\s+\$(\d+)/(\$$1)::time/g;
    ')
  fi

  if [ -z "$CANONICAL" ] || echo "$CANONICAL" | grep -qE '^ERROR'; then
    local LOG_ENTRY="{\"candidate\":\"${MV_FULL}\",\"stage\":\"stage_b_canonical\",\"verdict\":\"SKIP\",\"reason\":\"no_canonical_found\"}"
    echo "$LOG_ENTRY" >> "$RESULTS_LOG"
    echo "  → SKIP (no canonical)" >&2
    SKIPPED_OTHER=$((SKIPPED_OTHER + 1))
    return 0
  fi

  local PARAM_COUNT
  PARAM_COUNT=$(echo "$CANONICAL" | grep -oE '\$[0-9]+' | sort -u | wc -l | tr -d ' ')
  local PARAMS=""
  if [ "$PARAM_COUNT" -gt 0 ]; then
    local DEFAULTS=()
    for i in $(seq 1 "$PARAM_COUNT"); do DEFAULTS+=("NULL"); done
    PARAMS=$(IFS=','; echo "${DEFAULTS[*]}")
  fi

  # ---- Apply index_only recipe (always first) ----
  local LAST_STAGE
  LAST_STAGE=$(run_recipe "$SCRIPT_DIR/recipes/index_only.sh" "$MV_FULL" "$STRATEGY" "$SCHEMA" "$MV" "$CANONICAL" "$PARAMS")

  local STAGE VERDICT REASON
  # ---- Dispatch: if index_only ends at propose:SKIP or gate:FAIL, try fallback recipe ----
  # Also handles zero-stages (no JSONL output from index_only) — emits a dispatch:FAIL row
  # and routes to the strategy-mapped fallback rather than silently dropping the candidate.
  local NEEDS_FALLBACK=0
  if [ -z "$LAST_STAGE" ]; then
    echo "  → index_only emitted no stages; routing to fallback" >&2
    local DISPATCH_ZERO_LINE="{\"candidate\":\"${MV_FULL}\",\"stage\":\"dispatch\",\"verdict\":\"FAIL\",\"reason\":\"index_only_zero_stages\",\"recipe\":\"dispatch\",\"strategy\":\"${STRATEGY}\"}"
    echo "$DISPATCH_ZERO_LINE" >> "$RESULTS_LOG"
    NEEDS_FALLBACK=1
  else
    STAGE=$(echo "$LAST_STAGE" | jq -r '.stage')
    VERDICT=$(echo "$LAST_STAGE" | jq -r '.verdict // "OK"')
    if [ "$STAGE" = "propose" ] && [ "$VERDICT" = "SKIP" ]; then
      NEEDS_FALLBACK=1
    elif [ "$STAGE" = "gate" ] && [ "$VERDICT" = "FAIL" ]; then
      NEEDS_FALLBACK=1
    fi
  fi

  if [ "$NEEDS_FALLBACK" -eq 1 ]; then
    local FALLBACK_RECIPE=""
    local TIME_DEP_FLAG="false"
    case "$STRATEGY" in
      leaf_deterministic)
        FALLBACK_RECIPE="$SCRIPT_DIR/recipes/leaf_swap.sh"
        TIME_DEP_FLAG="false"
        ;;
      leaf_time_dependent)
        FALLBACK_RECIPE="$SCRIPT_DIR/recipes/leaf_swap.sh"
        TIME_DEP_FLAG="true"
        ;;
      has_deps_deterministic)
        FALLBACK_RECIPE="$SCRIPT_DIR/recipes/subtree_drop_recreate.sh"
        ;;
      has_deps_time_dependent)
        local DISPATCH_LINE="{\"candidate\":\"${MV_FULL}\",\"stage\":\"dispatch\",\"verdict\":\"SKIP\",\"reason\":\"needs_human_review_time_dependent_with_deps\",\"recipe\":\"dispatch\",\"strategy\":\"${STRATEGY}\"}"
        echo "$DISPATCH_LINE" >> "$RESULTS_LOG"
        echo "  → DISPATCH_SKIP (has_deps_time_dependent requires human review)" >&2
        SKIPPED_RECIPE=$((SKIPPED_RECIPE + 1))
        return 0
        ;;
      *)
        local DISPATCH_UNKNOWN="{\"candidate\":\"${MV_FULL}\",\"stage\":\"dispatch\",\"verdict\":\"SKIP\",\"reason\":\"unknown_strategy\",\"recipe\":\"dispatch\",\"strategy\":\"${STRATEGY}\"}"
        echo "$DISPATCH_UNKNOWN" >> "$RESULTS_LOG"
        echo "  → SKIP_RECIPE (unknown strategy: ${STRATEGY})" >&2
        SKIPPED_RECIPE=$((SKIPPED_RECIPE + 1))
        return 0
        ;;
    esac

    echo "  → fallback: $(basename $FALLBACK_RECIPE)" >&2
    # Pass --time-dependent only for leaf_swap
    local EXTRA_ARGS=""
    if [[ "$FALLBACK_RECIPE" == *"leaf_swap"* ]]; then
      EXTRA_ARGS="--time-dependent $TIME_DEP_FLAG"
    fi

    local fallback_out
    if [ "$DRY_RUN" -eq 1 ]; then
      fallback_out=$(doppler run -- bash "$FALLBACK_RECIPE" \
        --schema "$SCHEMA" --mv "$MV" \
        --canonical "$CANONICAL" --params "$PARAMS" \
        --project-path "$PROJECT_PATH" \
        --improvement-threshold "$THRESHOLD" \
        $EXTRA_ARGS \
        --dry-run 2>&1) || true
    else
      fallback_out=$(doppler run -- bash "$FALLBACK_RECIPE" \
        --schema "$SCHEMA" --mv "$MV" \
        --canonical "$CANONICAL" --params "$PARAMS" \
        --project-path "$PROJECT_PATH" \
        --improvement-threshold "$THRESHOLD" \
        $EXTRA_ARGS 2>&1) || true
    fi

    while IFS= read -r recipe_line; do
      if echo "$recipe_line" | grep -qE '^\{'; then
        local enriched
        enriched=$(echo "$recipe_line" | jq -c --arg c "$MV_FULL" --arg s "$STRATEGY" '. + {candidate:$c, strategy:$s}')
        echo "$enriched" >> "$RESULTS_LOG"
      fi
    done <<< "$fallback_out"

    LAST_STAGE=$(echo "$fallback_out" | grep -E '^\{' | tail -1)
    if [ -z "$LAST_STAGE" ]; then
      echo "  → FAIL (fallback emitted no stages)" >&2
      FAILED=$((FAILED + 1))
      return 0
    fi
    STAGE=$(echo "$LAST_STAGE" | jq -r '.stage')
    VERDICT=$(echo "$LAST_STAGE" | jq -r '.verdict // "OK"')
  fi

  # ---- Tally final verdict ----
  case "$STAGE" in
    ship)
      local PR_URL
      PR_URL=$(echo "$LAST_STAGE" | jq -r '.pr_url')
      echo "  → APPLIED (PR: ${PR_URL})" >&2
      APPLIED=$((APPLIED + 1))
      PR_URLS+=("$PR_URL")
      ;;
    gate)
      if [ "$VERDICT" = "FAIL" ]; then
        local IMP
        IMP=$(echo "$LAST_STAGE" | jq -r '.improvement_pct')
        echo "  → SKIP_PERF (improvement ${IMP} below threshold ${THRESHOLD})" >&2
        SKIPPED_PERF=$((SKIPPED_PERF + 1))
      else
        echo "  → ANOMALY (gate PASS but no ship — recipe stopped early)" >&2
        FAILED=$((FAILED + 1))
      fi
      ;;
    propose)
      if [ "$VERDICT" = "SKIP" ]; then
        REASON=$(echo "$LAST_STAGE" | jq -r '.reason')
        echo "  → SKIP_RECIPE (propose: ${REASON})" >&2
        SKIPPED_RECIPE=$((SKIPPED_RECIPE + 1))
      else
        echo "  → FAIL at propose stage" >&2
        FAILED=$((FAILED + 1))
      fi
      ;;
    baseline|build|rebenchmark|shadow_build|equality_gate|plan_node|swap|audit_gate)
      REASON=$(echo "$LAST_STAGE" | jq -r '.reason // "unknown"')
      echo "  → FAIL at ${STAGE}: ${REASON}" >&2
      FAILED=$((FAILED + 1))
      ;;
    *)
      echo "  → unknown verdict: ${LAST_STAGE}" >&2
      FAILED=$((FAILED + 1))
      ;;
  esac
  return 0
}

CANDIDATE_INDEX=0
while IFS= read -r LINE; do
  [ -z "$LINE" ] && continue
  CANDIDATE_INDEX=$((CANDIDATE_INDEX + 1))
  if [ "$AUTO_WORKTREE" -eq 1 ]; then
    # Reset worktree HEAD to origin/main between candidates so each ship_pr
    # branches off main cleanly (instead of stacking on the previous PR's branch).
    git checkout --force --detach origin/main >/dev/null 2>&1
  fi
  echo "[${CANDIDATE_INDEX}/${CANDIDATE_COUNT}] $(echo "$LINE" | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')" >&2
  MV_FULL=$(echo "$LINE" | awk -F'|' '{gsub(/^ +| +$/,"",$3); print $3}')
  process_one_candidate "$LINE" || {
    LOG_ENTRY="{\"candidate\":\"${MV_FULL:-unknown}\",\"stage\":\"orchestrator\",\"verdict\":\"FAIL\",\"reason\":\"unhandled_exception:exit_$?\"}"
    echo "$LOG_ENTRY" >> "$RESULTS_LOG"
    FAILED=$((FAILED + 1))
  }
done <<< "$CANDIDATES"

# ---- Final report (also fires via EXIT trap) ----
write_final_report

echo >&2
echo "=== Done ===" >&2
echo "  applied: ${APPLIED}/${CANDIDATE_COUNT}" >&2
echo "  report: ${REPORT_PATH}" >&2
echo "  results jsonl: ${RESULTS_LOG}" >&2

# Exit code conveys high-level success: 0 if at least one candidate applied, 1 otherwise.
if [ "$APPLIED" -gt 0 ]; then
  exit 0
else
  exit 1
fi
