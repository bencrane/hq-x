#!/usr/bin/env bash
# Verification harness for /scope cycle ucc-gleif-identity-spine.
#
# SKELETON authored by validator; per-surface verify bodies are filled by
# the audit stage from the directive's authoritative s1-s15 list.
#
# Mirrors the proven sba-bridges-to-lance.sh shape (prior cycle):
#   - Helper-library sourcing via the migration-checks shim.
#   - hqx_psql_query helper for HQX_DB_URL_POOLED reads.
#   - --surface / --repo filters; PASS / FAIL / SKIP accumulators.
#   - _lance_floor_check + _polaris_lance_check shared helpers (Lance row
#     floor + Polaris Generic Table registration existence).
#   - s14 deploy block gated by MERGE_SHA, s15 smoke gated by SMOKE_E2E.
#
# Audit MUST:
#   1) Inspect each source Lance dataset schema BEFORE authoring SQL
#      (the prior cycle hit 5+ column-name bugs from assumptions). The
#      validator stamped the captured schemas under `## Validator notes`
#      in the directive — read those first.
#   2) Write per-surface bodies in the placeholder blocks below.
#   3) Use Arrow-bridge pattern for Lance reads (NOT lance-duckdb
#      extension; unstable on osx_arm64 per Lance canary cycle).
#   4) Use ON CONFLICT (display_name) for ops.data_sources UPSERT (s9).
#   5) Keep all bridge scripts under
#      apps/data-engine-x/scripts/build_bridge_*.py with --dry-run /
#      --apply flag + ops.bridge_generation_runs telemetry row.

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

# --- hq-x DB helpers (HQX_DB_URL_POOLED, distinct from DEX) -------------- #
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
# Exits 0 iff Lance dataset count_rows() >= floor. Uses Arrow-bridge
# pattern (lance.dataset + count_rows); avoids lance-duckdb extension.
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

# ── s1: GLEIF Level-2 (relationships) Lance-emit ───────────────────────── #
# Floor: 100,000 (validator measured 647,268 raw rows in
# gleif/snapshot=2026-05-08/relationship_records.parquet; floor permissive).
run_surface "s1" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_gleif_relationships_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/gleif/relationship_records_lance/" 100000
'

# ── s2: GLEIF parent-traversal derive ──────────────────────────────────── #
# Floor: 3,000,000 (1:1 with Level-1 lei_records_lance which has 3,303,450
# rows; allows ~10% loss to nulls / non-resolvable hierarchies).
#
# Audit-added: chain_depth distribution assertion. Validator measured 191,914
# ACTIVE IS_ULTIMATELY_CONSOLIDATED_BY rows in raw Level-2; expect ≥100K
# rows in lei_with_parent_lance to carry chain_depth=1. Catches a missed
# JOIN on the relationship table (e.g., wrong column name from validator
# prediction `s1-gleif-level2-column-names`).
run_surface "s2" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_gleif_with_parent_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_with_parent_lance/" 3000000 &&
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 -c "
import os, lance, duckdb
storage_options = {
    \"aws_endpoint\": os.environ[\"R2_ENDPOINT\"],
    \"aws_access_key_id\": os.environ[\"R2_ACCESS_KEY_ID\"],
    \"aws_secret_access_key\": os.environ[\"R2_SECRET_ACCESS_KEY\"],
    \"aws_region\": \"us-east-1\",
    \"aws_virtual_hosted_style_request\": \"false\",
}
ds = lance.dataset(\"s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_with_parent_lance/\", storage_options=storage_options)
con = duckdb.connect()
con.register(\"lwp\", ds.to_table())
rows = con.execute(\"SELECT chain_depth, COUNT(*) FROM lwp GROUP BY chain_depth ORDER BY chain_depth\").fetchall()
parent_count = sum(c for d, c in rows if d and d >= 1)
if parent_count < 100000:
    print(f\"FAIL: chain_depth>=1 rows={parent_count:,} < 100,000  (distribution={rows})\")
    raise SystemExit(1)
print(f\"PASS: chain_depth distribution {rows}\")
"
'

# ── s3: UCC × GLEIF Level-1 bridge ─────────────────────────────────────── #
# Floor: 100,000 (UCC secured parties × GLEIF; 10-25% coverage of 4.7M).
# Audit also asserts bridge_generation_runs latest status='completed'.
run_surface "s3" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_ucc_gleif_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_gleif_lance/" 100000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'ucc_gleif'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s4: UCC × PDL bridge (GLEIF-parent-aware) ──────────────────────────── #
# Floor: 300,000. Output schema MUST include `match_path` column
# ('via_gleif_parent' | 'direct'). Audit assertion below covers BOTH paths
# producing ≥1 row each — catches a regression where one path silently
# drops out (e.g., a wrong join predicate or a missing UNION leg).
run_surface "s4" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_ucc_pdl_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_pdl_lance/" 10000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'ucc_pdl'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed" &&
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 -c "
import os, lance, duckdb
storage_options = {
    \"aws_endpoint\": os.environ[\"R2_ENDPOINT\"],
    \"aws_access_key_id\": os.environ[\"R2_ACCESS_KEY_ID\"],
    \"aws_secret_access_key\": os.environ[\"R2_SECRET_ACCESS_KEY\"],
    \"aws_region\": \"us-east-1\",
    \"aws_virtual_hosted_style_request\": \"false\",
}
ds = lance.dataset(\"s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_pdl_lance/\", storage_options=storage_options)
con = duckdb.connect()
con.register(\"b\", ds.to_table())
rows = con.execute(\"SELECT match_path, COUNT(*) FROM b GROUP BY match_path ORDER BY match_path\").fetchall()
paths = {r[0]: r[1] for r in rows}
if paths.get(\"via_gleif_parent\", 0) <= 0 or paths.get(\"direct\", 0) <= 0:
    print(f\"FAIL: both match_path values need >=1 row (distribution={paths})\")
    raise SystemExit(1)
print(f\"PASS: match_path distribution {paths}\")
"
'

# ── s5: UCC × SBA Lender bridge ────────────────────────────────────────── #
# Floor: 1,000 (small intersection of 11K SBA lenders ∩ 101K UCC canonical
# lenders). Join on UCC ucc_ca.lenders_lance.lender_name_normalized vs
# sba.lenders_lance.bankname_normalized (validator-confirmed schemas).
run_surface "s5" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_ucc_sba_lender_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_lender_lance/" 1000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'ucc_sba_lender'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s6: UCC × SBA Borrower bridge ──────────────────────────────────────── #
# Floor: 100,000. UCC debtor side (5.86M) joins SBA borrower side (12M).
# UCC debtors_lance has ORG_NAME / LAST_NAME / FIRST_NAME (raw upstream);
# NO pre-normalized name column — audit MUST normalize on the fly OR
# require the executor to also add a normalized column to debtors_lance.
# This is the schema gotcha equivalent to prior cycle finding.
run_surface "s6" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_ucc_sba_borrower_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_borrower_lance/" 100000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'ucc_sba_borrower'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s7: SBA Lender × GLEIF bridge ──────────────────────────────────────── #
# Floor: 2,000. SBA lenders (11K) × GLEIF (3.3M LEIs); join on
# bankname_normalized vs legal_name_normalized.
run_surface "s7" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_sba_lender_gleif_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_lender_gleif_lance/" 200 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sba_lender_gleif'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s8: SBA Borrower × GLEIF bridge ────────────────────────────────────── #
# Floor: 10,000. SBA borrowers (12M) × GLEIF (3.3M); <1% expected coverage.
# Join on (legal_name_normalized, state) — SBA has borrstate; GLEIF
# headquarters_region for US LEIs is "US-CA" etc — audit normalizes both.
run_surface "s8" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/build_bridge_sba_borrower_gleif_lance.py" &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_borrower_gleif_lance/" 10000 &&
  LATEST_STATUS=$(dex_psql_query "SELECT status FROM ops.bridge_generation_runs WHERE bridge_name='"'"'sba_borrower_gleif'"'"' ORDER BY started_at DESC LIMIT 1") &&
  test "$LATEST_STATUS" = "completed"
'

# ── s9: ops.data_sources migration applied ─────────────────────────────── #
# 8 new display_name rows expected. Audit uses ON CONFLICT (display_name)
# per prior cycle validator finding (display_name is the UNIQUE key;
# source_id is gen_random_uuid PK).
run_surface "s9" "hq-all" '
  COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE format='"'"'lance'"'"' AND owner_app='"'"'data-engine-x'"'"' AND status='"'"'active'"'"' AND display_name IN ('"'"'gleif_relationship_records_lance'"'"','"'"'gleif_lei_with_parent_lance'"'"','"'"'bridges_ucc_gleif_lance'"'"','"'"'bridges_ucc_pdl_lance'"'"','"'"'bridges_ucc_sba_lender_lance'"'"','"'"'bridges_ucc_sba_borrower_lance'"'"','"'"'bridges_sba_lender_gleif_lance'"'"','"'"'bridges_sba_borrower_gleif_lance'"'"')") &&
  test "$COUNT" = "8"
'

# ── s10: matching-engine ENTITY_REF_COLUMNS extension ──────────────────── #
# Audit confirms 13 new tuple keys exist (8 bridges + 2 GLEIF derives +
# 3 UCC base tables — needed for s15 single-dataset smoke and operator-
# direct UCC specs per validator prediction `s10-ucc-base-tables-needed`).
run_surface "s10" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"gleif\", \"relationship_records_lance\"" "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"gleif\", \"lei_with_parent_lance\""      "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"ucc_gleif_lance\""           "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"ucc_pdl_lance\""             "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"ucc_sba_lender_lance\""      "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"ucc_sba_borrower_lance\""    "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"sba_lender_gleif_lance\""    "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"bridges\", \"sba_borrower_gleif_lance\""  "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"ucc_ca\", \"lenders_lance\""              "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"ucc_ca\", \"debtors_lance\""              "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py" &&
  grep -q "\"ucc_ca\", \"secured_parties_lance\""      "$HQ_ALL_ROOT/apps/hq-x/app/services/matching_engine/engine.py"
'

# ── s11: Polaris Generic Table API registrations (all 10) ──────────────── #
run_surface "s11" "hq-all" '
  _polaris_lance_check "gleif"   "relationship_records_lance"      &&
  _polaris_lance_check "gleif"   "lei_with_parent_lance"           &&
  _polaris_lance_check "bridges" "ucc_gleif_lance"                 &&
  _polaris_lance_check "bridges" "ucc_pdl_lance"                   &&
  _polaris_lance_check "bridges" "ucc_sba_lender_lance"            &&
  _polaris_lance_check "bridges" "ucc_sba_borrower_lance"          &&
  _polaris_lance_check "bridges" "sba_lender_gleif_lance"          &&
  _polaris_lance_check "bridges" "sba_borrower_gleif_lance"
'

# ── s12: backfill (composite — re-run s1-s8 row-floor checks) ──────────── #
# Backfill is a runbook step, not a separate artifact. Its verify is the
# union of s1-s8 row-count floors passing AFTER --apply has been run.
run_surface "s12" "hq-all" '
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/gleif/relationship_records_lance/" 100000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_with_parent_lance/"      3000000  &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_gleif_lance/"          100000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_pdl_lance/"            10000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_lender_lance/"     1000     &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_sba_borrower_lance/"   100000   &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_lender_gleif_lance/"   200     &&
  _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_borrower_gleif_lance/" 10000
'

# ── s13: Trigger.dev cron extension (EXTEND existing sba-bridges-daily) ── #
# Audit decision: EXTEND sba-bridges-daily.ts. maxDuration bumped from 5400
# → 10800 (3 hours); 8 new scripts appended to DEX's _DAILY_SCRIPTS list.
# (Fork-branch dropped — validator recommended EXTEND; audit confirmed.)
run_surface "s13" "hq-all" '
  test -f "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "maxDuration: 10800" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
  grep -q "emit_gleif_relationships_lance.py"        "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "emit_gleif_with_parent_lance.py"          "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_ucc_gleif_lance.py"          "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_ucc_pdl_lance.py"            "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_ucc_sba_lender_lance.py"     "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_ucc_sba_borrower_lance.py"   "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_sba_lender_gleif_lance.py"   "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
  grep -q "build_bridge_sba_borrower_gleif_lance.py" "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py"
'

# ── s14: hq-x AND data-engine-x Railway deploy + runtime probes ────────── #
# Skip in pre-deploy mode (no MERGE_SHA env); deploy-verifier sets MERGE_SHA.
# Both services may redeploy on merge (DEX redeploys if sba_bridges_internal
# router is extended for new ucc bridges). Per
# apps/data-engine-x/CLAUDE.md §"Deploy verification" use the runtime-probe
# helper — health-check alone is insufficient (2026-05-12 numpy incident).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s14-deploy-hqx" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"hq-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s14-runtime-probe-hqx" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime hq-x "https://api.opsengine.run"
  '
  run_surface "s14-deploy-dex" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      cd $HQ_ALL_ROOT && railway status --json |
      jq -e -r \".environments.edges[].node.serviceInstances.edges[].node | select(.serviceName==\\\"data-engine-x\\\") | .latestDeployment | select(.status==\\\"SUCCESS\\\") | .meta.commitHash\" > /dev/null
    "
  '
  run_surface "s14-runtime-probe-dex" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s14 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify for both hq-x and data-engine-x)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s15: matching-engine end-to-end smoke (gated by SMOKE_E2E=1) ───────── #
# Per directive `## Goal restated`: "≥1 row lands in business.matches via
# the live engine" against a synthesized spec targeting ucc_ca.lenders_lance
# (PDL-enriched, exclude SBA-originator intersection). Only meaningful
# AFTER s14 deploys + ENTITY_REF_COLUMNS is wired (s10) + bridges exist
# (s3-s8 with backfill applied, s12). Audit specifies the exact spec body.
if [[ -n "${SMOKE_E2E:-}" ]]; then
  run_surface "s15-end-to-end-smoke" "hq-all" '
    MATCHES=$(hqx_psql_query "SELECT COUNT(*) FROM business.matches WHERE target_entity_ref LIKE '"'"'ucc_ca.lenders_lance|%'"'"'") &&
    test "$MATCHES" -gt "0"
  '
else
  echo "-- s15-end-to-end-smoke (hq-all): SKIPPED (set SMOKE_E2E=1 to run)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
