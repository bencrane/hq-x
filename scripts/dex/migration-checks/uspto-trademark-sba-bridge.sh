#!/usr/bin/env bash
# Verification harness for /scope cycle
#   uspto-trademark-lance-and-sba-capital-matching-bridge.
#
# Runs the per-surface verify commands. Exits 0 iff every requested check
# passes. Accepts:
#   --surface <id>    run a single surface (e.g. --surface s4)
#   --repo <name>     filter to one repo's surfaces (only hq-all in this cycle)
#
# Sources the canonical helper library via the migration-checks shim so we
# pick up dex_psql_query / dex_psql_query_direct / dex_min_row_floor_check.
# See apps/data-engine-x/CLAUDE.md §"Helper library".
#
# Pattern mirrors apps/data-engine-x/scripts/migration-checks/sba-bridges-to-lance.sh
# from the 2026-05-12 SBA-bridges-to-Lance cycle (predecessor). The two
# helpers _lance_floor_check and _polaris_lance_check are kept inline here
# rather than added to _lib/dex.sh — they are not part of the canonical
# helper-library surface yet (the predecessor decided the same).

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

# --- Name-normalizer version gate (cross-source HARD CONSTRAINT) --------- #
# Audit gate: USPTO emit scripts MUST import the SAME _lib/entity_name_normalize
# module version as the SBA path. Drift collapses (legal_name_normalized,
# state) join keys and the bridge row count floor fails.
# See validator notes §"_lib/entity_name_normalize.py version (HARD CONSTRAINT)".
_normalizer_version_check() {
  local expected="$1"
  doppler run --project hq-all --config prd -- \
    uv run --quiet python3 -c "
import sys
sys.path.insert(0, '$HQ_ALL_ROOT/apps/data-engine-x')
from scripts._lib import entity_name_normalize
if entity_name_normalize.__version__ == '$expected':
    print(f'PASS: entity_name_normalize.__version__ = {entity_name_normalize.__version__}')
    sys.exit(0)
print(f'FAIL: entity_name_normalize.__version__ = {entity_name_normalize.__version__} != $expected')
sys.exit(1)
"
}

# ── s1: USPTO case_file Lance emit ────────────────────────────────────── #
# Script exists + imports the locked normalizer version + Lance dataset
# row count >= 11M floor (per §Volume floors; TCFD bulk ~11.5M).
run_surface "s1" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_case_file_lance.py" &&
  grep -q "from scripts._lib.entity_name_normalize" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_case_file_lance.py" &&
  _normalizer_version_check "1.0.0" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance/" 11000000
'

# ── s2: USPTO case_file_owner Lance emit (own_seq=1 primary applicants) ── #
# Script exists + imports the locked normalizer + Lance dataset >= 11M floor.
# own_seq=1 covers ~98.5% of marks (one row per primary applicant); per
# directive §Volume floors, the floor here matches case_file (1:1 on the
# primary-applicant slice).
run_surface "s2" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_case_file_owner_lance.py" &&
  grep -q "from scripts._lib.entity_name_normalize" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_case_file_owner_lance.py" &&
  grep -q "own_seq" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_case_file_owner_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance/" 11000000
'

# ── s3: USPTO correspondent_domrep_attorney Lance emit ─────────────────── #
# Script exists + is_pro_se computed column logic present + row count >= 11M.
# is_pro_se = (attorney_name_normalized IS NULL OR empty) — this is the
# is_pro_se → expected_recipient_kind feeder for s4.
run_surface "s3" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_correspondent_lance.py" &&
  grep -q "is_pro_se" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_uspto_correspondent_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/correspondent_domrep_attorney_lance/" 11000000
'

# ── s4: USPTO × SBA capital-matching bridge ────────────────────────────── #
# Script exists + reads borrowers_lance filtered for has_pending_commit=TRUE +
# Lance dataset >= 1,500 floor (audit tightening per validator §Volume floors,
# from directive's 800 sanity floor). Bridge run audit row in
# ops.bridge_generation_runs has status='completed'.
# Normalizer-parity gate (legal_name_normalized) is enforced at the EMIT
# scripts (s1/s2 grep for the canonical import + s1 calls
# _normalizer_version_check) since the bridge consumes pre-normalized columns.
run_surface "s4" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py" &&
  grep -q "polaris-warehouse/bridges/uspto_sba_capital_matching_lance" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py" &&
  grep -q "has_pending_commit" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py" &&
  grep -q "expected_recipient_kind" \
    "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/uspto_sba_capital_matching_lance/" 1500 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'uspto_sba_capital_matching'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s5: ops.data_sources migration applied ─────────────────────────────── #
# 4 new rows visible: 3 USPTO sources + 1 bridge. All format='lance',
# status='active', owner_app='data-engine-x'. Migration file present at the
# next-available YYYYMMDDHHMMSS_uspto_lance_data_sources.sql timestamp
# (next slot is 20260527000000 per /supabase/migrations/ inspection).
run_surface "s5" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/supabase/migrations/20260527000000_uspto_lance_data_sources.sql" &&
  ACTIVE_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE format='"'"'lance'"'"' AND owner_app='"'"'data-engine-x'"'"' AND status='"'"'active'"'"' AND display_name IN ('"'"'uspto_case_file_lance'"'"','"'"'uspto_case_file_owner_lance'"'"','"'"'uspto_correspondent_domrep_attorney_lance'"'"','"'"'uspto_sba_capital_matching_lance'"'"')") &&
  test "$ACTIVE_COUNT" = "4"
'

# ── s6: Polaris Generic Table API registrations (all 4) ────────────────── #
# GET each generic-table; verify format=lance per response payload.
# Namespaces: uspto (3 raw sources) + bridges (1 capital-matching bridge).
run_surface "s6" "hq-all" '
  _polaris_lance_check "uspto"   "case_file_lance"                       &&
  _polaris_lance_check "uspto"   "case_file_owner_lance"                 &&
  _polaris_lance_check "uspto"   "correspondent_domrep_attorney_lance"   &&
  _polaris_lance_check "bridges" "uspto_sba_capital_matching_lance"
'

# ── s7: One-time backfill (composite — re-run s1-s4 row-floor checks) ──── #
# Backfill is a runbook step, not a separate artifact. Verify is the union
# of s1-s4 row-count floors passing AFTER --apply has been run.
run_surface "s7" "hq-all" '
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance/"                       11000000 &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance/"                 11000000 &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/uspto/correspondent_domrep_attorney_lance/"   11000000 &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/uspto_sba_capital_matching_lance/"    1500
'

# ── s8: Trigger.dev cron extension (hq-x project) ──────────────────────── #
# The DEX-side script list in apps/data-engine-x/app/routers/sba_bridges_internal_v1.py
# MUST include build_bridge_uspto_sba_capital_matching_lance.py as a final
# entry. The hq-x Trigger task file itself (sba-bridges-daily.ts) needs no
# behavioural edits — it already forwards to DEX's /api/internal/sba-bridges/run-daily,
# which iterates the SCRIPTS list. Verify both: (a) DEX list contains the
# USPTO bridge script entry, and (b) the hq-x cron task still exists with
# the daily-09-UTC schedule.
run_surface "s8" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_uspto_sba_capital_matching_lance.py" \
    "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  test -f "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "0 9 \* \* \*" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  # If TRIGGER_DEPLOYED=1 is set, also verify Trigger.dev sees the schedule.
  if [[ -n "${TRIGGER_DEPLOYED:-}" ]]; then
    doppler run --project hq-all --config prd -- bash -c "
      curl -fsS https://api.trigger.dev/api/v1/schedules \
        -H \"Authorization: Bearer \$TRIGGER_SECRET_KEY\" |
      jq -e \".data[] | select(.task==\\\"sba-bridges-daily\\\")\" > /dev/null
    "
  fi
'

# ── s9: hq-x AND data-engine-x Railway deploy + runtime probes ─────────── #
# Skip in pre-deploy mode (no MERGE_SHA env); deploy-verifier sets MERGE_SHA.
# Both hq-x AND data-engine-x redeploy on merge (DEX is deployed since the
# build_bridge_uspto_sba_capital_matching_lance.py is added to the SCRIPTS
# list in apps/data-engine-x/app/routers/sba_bridges_internal_v1.py — the
# DEX FastAPI service is the script-runner for the cron).
#
# Railway CLI v4.33.0 gotcha (per apps/data-engine-x/CLAUDE.md §"Deploy targets"):
# `railway status` does NOT accept `--service <name>`; service enumeration
# is via JSON parsing of environments.edges[].node.serviceInstances.edges[].
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s9-deploy-hqx" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"hq-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s9-runtime-probe-hqx" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime hq-x "https://api.opsengine.run"
  '
  run_surface "s9-deploy-dex" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"data-engine-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s9-runtime-probe-dex" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s9 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify for both hq-x and data-engine-x)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
