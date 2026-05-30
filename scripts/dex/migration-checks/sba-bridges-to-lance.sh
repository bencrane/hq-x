#!/usr/bin/env bash
# Verification harness for /scope cycle sba-bridges-to-lance.
#
# Runs the per-surface verify commands. Exits 0 iff every requested check
# passes. Accepts:
#   --surface <id>    run a single surface (e.g. --surface s1)
#   --repo <name>     filter to one repo's surfaces (only hq-all in this cycle)
#
# Sources the canonical helper library via the migration-checks shim so we
# pick up dex_psql_query / dex_psql_query_direct / dex_min_row_floor_check.
# See apps/data-engine-x/CLAUDE.md §"Helper library".

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

# --- hq-x DB helpers (HQX_DB_URL_DIRECT, distinct from DEX) -------------- #
# Doppler shell gotcha: env vars must be referenced inside bash -c '...' so
# Doppler expands them at runtime, not at compose time. See
# apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha".
_hqx_doppler() {
  doppler run --project hq-all --config prd -- bash -c "$1"
}

hqx_psql_query() {
  local sql="$1"
  _hqx_doppler "psql \"\$HQX_DB_URL_POOLED\" -tAc \"$sql\""
}

# --- CLI parsing ---------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (surface filter: ${SURFACE_FILTER:-all}; repo filter: ${REPO_FILTER:-all})"

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

# --- Lance row-count gate (shared helper) -------------------------------- #
# Usage: _lance_floor_check <lance_uri> <floor>
# Exits 0 iff Lance dataset count_rows() >= floor.
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 -c "
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
rows = ds.count_rows()
if rows >= $floor:
    print(f'PASS: $uri rows={rows:,} >= floor $floor')
    sys.exit(0)
print(f'FAIL: $uri rows={rows:,} < floor $floor')
sys.exit(1)
"
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet python apps/data-engine-x/scripts/init_polaris_lance_generic.py \
      --namespace "$ns" --table "$tbl" --check-only
}

# ── s1: SBA loans Lance ────────────────────────────────────────────────── #
run_surface "s1" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sba_loans_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/" 12000000
'

# ── s2: SBA borrowers Lance ────────────────────────────────────────────── #
run_surface "s2" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sba_borrowers_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/" 8000000
'

# ── s3: SBA lenders Lance ──────────────────────────────────────────────── #
run_surface "s3" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sba_lenders_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance/" 4000
'

# ── s4: PDL Free Companies Lance ───────────────────────────────────────── #
run_surface "s4" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_pdl_free_companies_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/" 5000000
'

# ── s5: PDL × SBA borrower bridge (rewrite to Lance) ───────────────────── #
run_surface "s5" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_pdl_sba_borrower.py" &&
  grep -q "polaris-warehouse/bridges/pdl_sba_borrower_lance" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_pdl_sba_borrower.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_sba_borrower_lance/" 1800000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'pdl_sba_borrower'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s6: SAM × SBA borrower bridge (rewrite to Lance) ───────────────────── #
run_surface "s6" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_sam_sba_borrower.py" &&
  grep -q "polaris-warehouse/bridges/sam_sba_borrower_lance" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_sam_sba_borrower.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sba_borrower_lance/" 250000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sam_sba_borrower'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s6.5: SAM monthly entities Lance ───────────────────────────────────── #
run_surface "s6.5" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sam_entities_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance/" 800000
'

# ── s7: USAspending × SBA borrower bridge (new) ────────────────────────── #
run_surface "s7" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_usaspending_sba_borrower.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sba_borrower_lance/" 50000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'usaspending_sba_borrower'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s8: ops.data_sources migration applied ─────────────────────────────── #
run_surface "s8" "hq-all" '
  ACTIVE_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE format='"'"'lance'"'"' AND owner_app='"'"'data-engine-x'"'"' AND status='"'"'active'"'"' AND display_name IN ('"'"'sba_loans_lance'"'"','"'"'sba_borrowers_lance'"'"','"'"'sba_lenders_lance'"'"','"'"'sam_entities_lance'"'"','"'"'pdl_free_companies_lance'"'"','"'"'pdl_sba_borrower_lance'"'"','"'"'sam_sba_borrower_lance'"'"','"'"'usaspending_sba_borrower_lance'"'"')") &&
  test "$ACTIVE_COUNT" = "8" &&
  RETIRED=$(dex_psql_query "SELECT status FROM ops.data_sources WHERE display_name='"'"'bridges'"'"'") &&
  test "$RETIRED" = "retired"
'

# ── s9: Polaris Generic Table API registrations (all 8) ────────────────── #
run_surface "s9" "hq-all" '
  _polaris_lance_check "sba"     "loans_lance"                   &&
  _polaris_lance_check "sba"     "borrowers_lance"               &&
  _polaris_lance_check "sba"     "lenders_lance"                 &&
  _polaris_lance_check "sam_gov" "entities_lance"                &&
  _polaris_lance_check "pdl"     "free_companies_lance"          &&
  _polaris_lance_check "bridges" "pdl_sba_borrower_lance"        &&
  _polaris_lance_check "bridges" "sam_sba_borrower_lance"        &&
  _polaris_lance_check "bridges" "usaspending_sba_borrower_lance"
'

# ── s10: backfill (composite — re-run s1-s7 row-floor checks) ──────────── #
# Backfill is a runbook step, not a separate artifact. Its verify is the
# union of s1-s7 row-count floors passing AFTER --apply has been run.
run_surface "s10" "hq-all" '
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/"                              12000000 &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"                          8000000  &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance/"                            4000     &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance/"                       800000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/"                     5000000  &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/pdl_sba_borrower_lance/"               1800000  &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_sba_borrower_lance/"               250000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/usaspending_sba_borrower_lance/"       50000
'

# ── s11: Trigger.dev daily cron (hq-x project — reviewer-corrected) ────── #
# Cron lives in hq-x (canonical Trigger.dev project) per memory
# app_responsibilities.md + trigger_dev_project_keys.md. The DEX trigger
# project is DEPRECATED. Reviewer-corrected 2026-05-12. Also verifies the
# companion hq-x + DEX internal endpoint files exist.
run_surface "s11" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "schedules.task" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "0 9 \* \* \*" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "sba-bridges-daily" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  test -f "$HQ_ALL_ROOT/apps/hq-x/app/routers/internal/sba_bridges.py" &&
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  # If TRIGGER_DEPLOYED=1 is set, also verify Trigger.dev sees the schedule
  # in hq-x'"'"'s project (TRIGGER_SECRET_KEY scopes to hq-x per memory).
  if [[ -n "${TRIGGER_DEPLOYED:-}" ]]; then
    doppler run --project hq-all --config prd -- bash -c "
      curl -fsS https://api.trigger.dev/api/v1/schedules \
        -H \"Authorization: Bearer \$TRIGGER_SECRET_KEY\" |
      jq -e \".data[] | select(.task==\\\"sba-bridges-daily\\\")\" > /dev/null
    "
  fi
'

# ── s12: business.matching_relationships seed (hq-x migration) ─────────── #
run_surface "s12" "hq-all" '
  ROW=$(hqx_psql_query "SELECT COUNT(*) FROM business.matching_relationships WHERE name='"'"'capital_partner_bridge_match_v1'"'"' AND enabled=true AND intent_source='"'"'paid_specs'"'"'") &&
  test "$ROW" = "1"
'

# ── s13: hq-x AND data-engine-x Railway deploy + runtime probes ────────── #
# Skip in pre-deploy mode (no MERGE_SHA env); deploy-verifier sets MERGE_SHA.
# Both hq-x AND data-engine-x redeploy on merge (reviewer-corrected: DEX is
# deployed since the new /api/internal/sba-bridges/run-daily endpoint is added).
if [[ -n "${MERGE_SHA:-}" ]]; then
  # Each service redeploys only when its OWN files change (Railway watchPatterns
  # filter per-service). In a multi-PR fix-forward sequence, services without
  # changes since an earlier merge stay on that earlier SHA — which is correct.
  # So instead of requiring deployed SHA == MERGE_SHA (latest), we require:
  # latest deploy SUCCESS + runtime probe non-5xx. The runtime probe is the
  # actual "is the deployed code working" gate.
  run_surface "s13-deploy-hqx" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"hq-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s13-runtime-probe-hqx" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime hq-x "https://api.opsengine.run"
  '
  run_surface "s13-deploy-dex" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"data-engine-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s13-runtime-probe-dex" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s13 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify for both hq-x and data-engine-x)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# --- s14 (matching engine smoke test — end-to-end completion criterion) -- #
# Per directive `## Goal restated`: "≥1 row lands in business.matches end-to-end
# (smoke; can be cleaned after)". Only meaningful AFTER s13 deploys + the next
# daily matching-engine-daily cron has run, OR after a manual invocation of
# /api/v1/internal/matching-engine/evaluate-all. Gated by SMOKE_E2E=1.
if [[ -n "${SMOKE_E2E:-}" ]]; then
  run_surface "s14-end-to-end-smoke" "hq-all" '
    MATCHES=$(hqx_psql_query "SELECT COUNT(*) FROM business.matches m JOIN business.matching_relationships r ON r.relationship_id=m.relationship_id WHERE r.name='"'"'capital_partner_bridge_match_v1'"'"'") &&
    test "$MATCHES" -gt "0"
  '
else
  echo "-- s14-end-to-end-smoke (hq-all): SKIPPED (set SMOKE_E2E=1 to run)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
