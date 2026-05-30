#!/usr/bin/env bash
# Verification harness for /scope cycle fmcsa-pipeline-remediation.
#
# Authored by Stage 3.A audit subagent (2026-05-12 UTC) per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-12-fmcsa-pipeline-remediation.md.
#
# Mirrors prior cycle patterns (ucc-gleif-identity-spine.sh; sba-bridges).
# Single-quote surface bodies so $VAR / $(...) defer to the doppler-injected
# subshell. DEX checks via apps/data-engine-x/scripts/_lib/dex.sh; HQ-X checks
# via the inline hqx_psql_query helper (HQ-X uses HQX_DB_URL_POOLED).
#
# Usage:
#   ./fmcsa-pipeline-remediation.sh                              # all surfaces
#   ./fmcsa-pipeline-remediation.sh --surface s3                 # single surface
#   ./fmcsa-pipeline-remediation.sh --repo bencrane/hq-all       # repo filter
#   MERGE_SHA=<sha> ./fmcsa-pipeline-remediation.sh              # include s7+s7b deploy gates

set -uo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
# If HQ_ALL_ROOT is pre-set (e.g. for worktree runs), honor it; otherwise search.
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
else
  for _root in "$HOME/hq-all/.claude/worktrees/serene-chaplygin-0a15d2" "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"
# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"

# --- HQ-X DB helper (HQX_DB_URL_POOLED — distinct from DEX) -------------- #
# Per validator P5: doppler run -- bash -c '...' to defer $VAR expansion.
_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}
hqx_psql_query() {
  local sql="$1"
  _hqx_doppler "psql \"\$HQX_DB_URL_POOLED\" -tAc \"$sql\""
}

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying fmcsa-pipeline-remediation surfaces (surface=${SURFACE_FILTER:-all}, repo=${REPO_FILTER:-all})"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# All 12 fmcsa.* tables — used by s1 / s6 verifies.
FMCSA_TABLES=(
  carrier_authority_event_records
  carrier_authority_records
  carrier_crash_records
  carrier_inspection_location_records
  carrier_inspection_records
  carrier_insurance_active_policy_records
  carrier_insurance_event_records
  carrier_insurance_policy_records
  carrier_officer_records
  carrier_records
  carrier_registration_records
  carrier_safety_basic_records
)

# ── s1: NEW backfill script exists + references full PK tuple per table ── #
# Files: apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py
# Validator P1 + P2: script must (a) hard-code snapshot=2026-05-12 single-date
# glob with a multi-snapshot assertion, (b) reference the per-table PK map.
run_surface "s1" "bencrane/hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py" &&
  grep -q "snapshot=2026-05-12" "$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py" &&
  grep -q "carrier_authority_event_records" "$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py" &&
  grep -q "ON CONFLICT" "$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py" &&
  grep -qE "event_kind.*docket_number.*sub_number_pk.*authority_type_pk.*event_date.*event_subtype" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/fmcsa/canonical_backfill_from_r2.py"
'

# ── s2a: DEX migration applied — ops.data_sources.health_status column ── #
# Target DB: DEX ($DEX_DB_URL_DIRECT applied; verify via POOLED read).
# Validator Q4: column must have CHECK constraint covering (live|dormant|retired).
run_surface "s2a" "bencrane/hq-all" '
  COL=$(dex_psql_query "SELECT column_name FROM information_schema.columns WHERE table_schema='\''ops'\'' AND table_name='\''data_sources'\'' AND column_name='\''health_status'\''") &&
  test "$COL" = "health_status" &&
  CHK=$(dex_psql_query "SELECT 1 FROM information_schema.check_constraints WHERE check_clause LIKE '\''%live%'\'' AND check_clause LIKE '\''%dormant%'\'' AND check_clause LIKE '\''%retired%'\''") &&
  test "$CHK" = "1"
'

# ── s2b: HQ-X migration applied — business.audience_spec_signings.freshness_status ── #
# Target DB: HQ-X ($HQX_DB_URL_DIRECT applied; verify via POOLED read).
# Validator P3: column does NOT exist in DEX; cross-DB scope confusion guard.
# Validator P8: HQ-X has no auto-apply hook; column-existence check is
# load-bearing — failure here means executor must run
#   cd apps/hq-x && doppler run --project hq-all --config prd -- bash -c "uv run python -m scripts.migrate"
# post-merge.
run_surface "s2b" "bencrane/hq-all" '
  COL=$(hqx_psql_query "SELECT column_name FROM information_schema.columns WHERE table_schema='\''business'\'' AND table_name='\''audience_spec_signings'\'' AND column_name='\''freshness_status'\''") &&
  test "$COL" = "freshness_status" &&
  CHK=$(hqx_psql_query "SELECT 1 FROM information_schema.check_constraints WHERE check_clause LIKE '\''%fresh%'\'' AND check_clause LIKE '\''%stale-do-not-surface%'\'' AND check_clause LIKE '\''%archived%'\''") &&
  test "$CHK" = "1"
'

# ── s3: DEX UPDATE — ops.data_sources.health_status flag distribution ── #
# Target DB: DEX. Live-DB-anchored cardinality (Stage 3.B reviewer re-queried
# 2026-05-12):
#   - 5 iceberg_fmcsa_* (dormant)
#   - 14 fmcsa_derived_* (dormant)
#   - 1 fmcsa_carrier_essentials parquet (dormant)
#   - 1 bare `fmcsa` legacy parent (dormant if executor includes; else NULL)
#   - 4 fmcsa_*_lance suffixed (live) — Lance layer IS current per audit Thread 7
# Total FMCSA-prefixed rows = 25.
# Expected dormant = 20 (without bare fmcsa) OR 21 (with). Expected live = EXACTLY 4.
# (Original audit-plan claim "5 lance" was off-by-one; reviewer confirmed via
# `SELECT display_name FROM ops.data_sources WHERE display_name LIKE 'fmcsa%_lance'`
# returns exactly 4 rows: fmcsa_authhist_essentials_lance,
# fmcsa_carrier_essentials_embeddings_lance, fmcsa_carrier_essentials_lance,
# fmcsa_crash_essentials_lance.)
run_surface "s3" "bencrane/hq-all" '
  DORMANT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE health_status='\''dormant'\'' AND (display_name LIKE '\''fmcsa%'\'' OR display_name LIKE '\''iceberg_fmcsa_%'\'' OR display_name = '\''fmcsa'\'')") &&
  LIVE=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE health_status='\''live'\'' AND display_name LIKE '\''fmcsa%_lance'\''") &&
  test "$DORMANT" -ge "20" && test "$DORMANT" -le "21" &&
  test "$LIVE" = "4"
'

# ── s4: HQ-X UPDATE — business.audience_spec_signings.freshness_status ── #
# Target DB: HQ-X. Exactly 4 TX-FMCSA signings (count_at_signing=12338 anchor;
# content filter PHY_STATE=TX + SAFETY_RATING=S over fmcsa.company_census_file).
# FAIL if != 4.
run_surface "s4" "bencrane/hq-all" '
  STALE=$(hqx_psql_query "SELECT COUNT(*) FROM business.audience_spec_signings WHERE freshness_status='\''stale-do-not-surface'\'' AND count_at_signing = 12338") &&
  test "$STALE" = "4"
'

# ── s5: DEX docs — apps/data-engine-x/CLAUDE.md FMCSA pipeline status ─── #
run_surface "s5" "bencrane/hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/CLAUDE.md" &&
  grep -q "^## FMCSA pipeline status" "$HQ_ALL_ROOT/apps/data-engine-x/CLAUDE.md"
'

# ── s6: backfill executed — all 12 fmcsa.* tables refreshed from R2 ─────── #
# Validator P1: no mixed-vintage. Primary feeds (Carrier, Company Census File,
# AuthHist, InsHist, Insurance, ActPendInsur, Crash File, Inspections and Citations,
# Revocation) have snapshot=2026-05-12 → MAX=2026-05-12.
# SMS feeds (SMS AB/C PassProperty, SMS Input - Inspection/Motor Carrier Census)
# only have snapshot=2026-05-10 on R2 → affected tables get MAX=2026-05-10.
# Executor-confirmed: carrier_safety_basic_records sources from SMS AB/C PassProperty
# (latest=2026-05-10). All other tables source from primary feeds (2026-05-12).
# Floor check: MAX(source_feed_date) >= '2026-05-10' AND >= pre-backfill stale date.
# No-mixed-vintage check: MIN == MAX per table (single feed_date per table post-backfill).
#
# [Stage 3.C executor update 2026-05-13: relaxed MAX='2026-05-12' to MAX>='2026-05-10'
# for SMS-sourced tables. carrier_safety_basic_records is the only affected table;
# all others still assert MAX='2026-05-12'.]
run_surface "s6" "bencrane/hq-all" '
  ALL_PASS=1
  # Tables that source from SMS feeds (latest R2 snapshot = 2026-05-10)
  SMS_TABLES="carrier_safety_basic_records"
  for T in '"${FMCSA_TABLES[*]}"'; do
    MAX=$(dex_psql_query "SELECT MAX(source_feed_date)::text FROM fmcsa.$T")
    MIN=$(dex_psql_query "SELECT MIN(source_feed_date)::text FROM fmcsa.$T")
    # Floor: must be >= 2026-05-10 (advancement from pre-backfill 2026-04-25)
    if [[ "$MAX" < "2026-05-10" ]]; then
      echo "  FAIL s6: fmcsa.$T MAX(source_feed_date)=$MAX (expected >= 2026-05-10)" >&2
      ALL_PASS=0
    fi
    # Primary feeds: require 2026-05-12 exactly
    if [[ "$SMS_TABLES" != *"$T"* ]] && [[ "$MAX" != "2026-05-12" ]]; then
      echo "  FAIL s6: fmcsa.$T MAX(source_feed_date)=$MAX (expected 2026-05-12 for primary feed table)" >&2
      ALL_PASS=0
    fi
    # No-mixed-vintage: MIN must equal MAX (single snapshot date per table)
    if [[ "$MIN" != "$MAX" ]]; then
      echo "  FAIL s6: fmcsa.$T MIN=$MIN MAX=$MAX (mixed-vintage)" >&2
      ALL_PASS=0
    fi
  done
  [[ "$ALL_PASS" = "1" ]]
'

# ── s7: DEX Railway deploy — deployed SHA == merge SHA ─────────────────── #
# Gated by MERGE_SHA env. Railway CLI v4.33.0 lacks --service on `status`;
# cd into app subdir per validator P7.
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s7" "bencrane/hq-all" '
    DEPLOYED=$(cd "$HQ_ALL_ROOT/apps/data-engine-x" && doppler run --project hq-all --config prd -- bash -c "railway status --json" | \
      jq -r ".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\"data-engine-x\") | .latestDeployment | select(.status==\"SUCCESS\") | .meta.commitHash" | head -1) &&
    test "$DEPLOYED" = "$MERGE_SHA"
  '
else
  echo "-- s7 (bencrane/hq-all): SKIPPED (set MERGE_SHA to run DEX deploy gate)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s7b: HQ-X Railway deploy — deployed SHA == merge SHA ───────────────── #
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s7b" "bencrane/hq-all" '
    DEPLOYED=$(cd "$HQ_ALL_ROOT/apps/hq-x" && doppler run --project hq-all --config prd -- bash -c "railway status --json" | \
      jq -r ".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\"hq-x\") | .latestDeployment | select(.status==\"SUCCESS\") | .meta.commitHash" | head -1) &&
    test "$DEPLOYED" = "$MERGE_SHA"
  '
else
  echo "-- s7b (bencrane/hq-all): SKIPPED (set MERGE_SHA to run HQ-X deploy gate)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s8: DEX runtime probe — non-trivial route reachable ──────────────── #
# Uses deploy_verify.sh::verify_service_runtime (default route is
# /api/v1/internal/observability/sources; auth-rejected 401 = PASS).
run_surface "s8" "bencrane/hq-all" '
  verify_service_runtime data-engine-x "https://api.dataengine.run"
'

# ── s8b: HQ-X runtime probe — /api/v1/signings reachable ─────────────── #
# Cycle-specific probe: /api/v1/signings exercises business.audience_spec_signings
# router code path. Auth-rejected 401/403 = PASS (route registered).
run_surface "s8b" "bencrane/hq-all" '
  verify_service_with_runtime_probes hq-x "https://api.opsengine.run" "/api/v1/signings"
'

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
