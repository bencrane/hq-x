#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-ucc-ca-lender-sos-ca-owner-bridge.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-ucc-ca-lender-sos-ca-owner-bridge.md
# and the validator's BLOCKING constraints (p1 normalizer choice, p2 Modal
# hosting, p3 floor 14,929, p4 match method REUSE per L21).
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Pattern mirrors the just-shipped sibling at
#   apps/data-engine-x/scripts/migration-checks/hq-all-ca-sos-master-unload-ingest.sh
#
# Surface coverage (5 surfaces, single PR):
#   s1   code      scripts/build_bridge_ucc_ca_lender_sos_ca_owner_lance.py (NEW, Modal app, Pattern B)
#   s2   migration supabase/migrations/{ts}_ucc_ca_lender_sos_ca_owner_data_source.sql (NEW)
#   s3   code      app/services/lance_views.py (EDIT — 1 new LanceView, register_at_boot=False)
#   s4   config    Polaris Generic Table registration (1 namespace=bridges, table=ucc_ca_lender_sos_ca_owner_lance)
#   s5   deploy    Railway data-engine-x auto-redeploy
#
# Usage:
#   ./hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh                                # pre-deploy file-shape gates
#   ./hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh --surface s1                   # one surface
#   ./hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh --repo hq-all                  # repo filter
#   POPULATED=1 ./hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh                    # incl. Lance + ops post-state gates
#   MERGE_SHA=<sha> ./hq-all-ucc-ca-lender-sos-ca-owner-bridge.sh                # incl. Railway deploy + runtime probe

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

DEX_APP="$HQ_ALL_ROOT/apps/data-engine-x"

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
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
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
\"
  "
}

# --- Lance BTREE index existence check (shared helper) ------------------ #
# Usage: _lance_btree_check <lance_uri> <column_name>
_lance_btree_check() {
  local uri="$1" col="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
indices = ds.list_indices()
btree_cols = []
for idx in indices:
    fields = idx.get('fields') if isinstance(idx, dict) else getattr(idx, 'fields', [])
    itype = idx.get('type') if isinstance(idx, dict) else getattr(idx, 'index_type', '')
    if 'BTREE' in str(itype).upper() or 'BTREE' in str(idx).upper():
        for f in (fields or []):
            btree_cols.append(str(f))
if '$col' in btree_cols:
    print(f'PASS: $uri has BTREE on $col')
    sys.exit(0)
print(f'FAIL: $uri missing BTREE on $col (saw indices: {indices})')
sys.exit(1)
\"
  "
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with requests python3 \"$HQ_ALL_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py\" \
      --namespace $ns --table $tbl --check-only
  "
}

# ── PR-target sanity gate ──────────────────────────────────────────────── #
run_surface "p-remote" "hq-all" '
  if [[ ! -d "$HQ_ALL_ROOT/.git" ]]; then
    echo "   (no local hq-all checkout — skipping PR-target gate)"
    true
  else
    actual=$(cd "$HQ_ALL_ROOT" && git remote get-url origin)
    [[ "$actual" =~ bencrane/hq-all ]]
  fi
'

# ── s1: UCC CA × SoS CA owner bridge (Pattern B) — Modal app ──────────── #
# Validator BLOCKING constraints enforced via grep:
#   p1 normalizer choice — entity_name_normalize ONLY on both sides; NO
#                          ucc_normalize, NO normalize_party_name (PR #459/#460
#                          root cause; canonical sibling PR #464).
#   p2 Modal hosting    — @app.function with memory>=16384 and timeout>=7200.
#   p3 floor 14,929     — MIN_ROWS_MATCHED literal present; fail_bridge_run
#                          on shortfall.
#   p4 match method REUSE per L21 — ONLY register_bridge + start/complete/fail
#                          _bridge_run; do NOT call register_match_method or
#                          register_match_method_version (these were registered
#                          by PR #464 with SBA-shape input_columns; UPSERT would
#                          overwrite shared config). Reference: build_bridge_
#                          sam_pdl_domain_lance.py:369-397.
# Bridge constants: BRIDGE_NAME="ucc_ca_lender_sos_ca_owner",
#                   METHOD_NAME="legal_name_state_exact_ca", METHOD_SEMVER="1.0.0",
#                   BRIDGE_VERSION="1.0.0", COLLISION_THRESHOLD=50.
# Inputs:  polaris-warehouse/ucc_ca/secured_parties_lance (SECURED_PARTY_TYPE='Organization')
#          polaris-warehouse/sos/ca_entities_lance        (entity_name_normalized is_valid)
# Output:  polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance
#          BTREE on secured_party_name_normalized + entity_num.
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_ucc_ca_lender_sos_ca_owner_lance.py"
  test -f "$f" &&
  # Modal app shape (p2)
  grep -qE "@app\.function\(" "$f" &&
  grep -qE "modal\.App\(" "$f" &&
  # Memory >= 16384 (validator p2 floor) and timeout >= 7200
  grep -qE "memory[[:space:]]*=[[:space:]]*(16384|32768|65536)" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*(7200|10800|14400)" "$f" &&
  # Bridge constants
  grep -qE "BRIDGE_NAME[[:space:]]*=[[:space:]]*\"ucc_ca_lender_sos_ca_owner\"" "$f" &&
  grep -qE "METHOD_NAME[[:space:]]*=[[:space:]]*\"legal_name_state_exact_ca\"" "$f" &&
  grep -qE "METHOD_SEMVER[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "BRIDGE_VERSION[[:space:]]*=[[:space:]]*\"1\.0\.0\"" "$f" &&
  grep -qE "COLLISION_THRESHOLD[[:space:]]*=[[:space:]]*50" "$f" &&
  # Floor 14,929 (validator p3); accept 14_929 or 14,929 or 14929
  grep -qE "MIN_ROWS_MATCHED[[:space:]]*=[[:space:]]*14[_,]?929" "$f" &&
  # Inputs
  grep -qE "polaris-warehouse/ucc_ca/secured_parties_lance" "$f" &&
  grep -qE "polaris-warehouse/sos/ca_entities_lance" "$f" &&
  # UCC Organization filter
  grep -qE "SECURED_PARTY_TYPE.*Organization" "$f" &&
  # Output
  grep -qE "polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance" "$f" &&
  # Pattern B discipline
  grep -qE "lance_commit_lock" "$f" &&
  # The slug passed to lance_commit_lock matches the dataset name
  grep -qE "ucc_ca_lender_sos_ca_owner_lance" "$f" &&
  # BTREE on both keys per directive verify column
  grep -qE "create_scalar_index.*secured_party_name_normalized.*BTREE" "$f" &&
  grep -qE "create_scalar_index.*entity_num.*BTREE" "$f" &&
  # Compact + cleanup
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # Registry helpers (validator p4 — L21 REUSE).
  # register_bridge + start/complete/fail_bridge_run MUST be present.
  grep -qE "register_bridge" "$f" &&
  grep -qE "start_bridge_run" "$f" &&
  grep -qE "complete_bridge_run" "$f" &&
  grep -qE "fail_bridge_run" "$f" &&
  # CRITICAL p4 GREP-ASSERT: register_match_method + register_match_method_version
  # MUST be ABSENT. These rows are SHARED with sba_sos_ca_owner (PR #464) and the
  # idempotent UPSERT would overwrite the SBA-shape input_columns config.
  # Precedent: build_bridge_sam_pdl_domain_lance.py:369-397 (only register_bridge).
  ! grep -qE "register_match_method_version" "$f" &&
  ! grep -qE "register_match_method\b" "$f" &&
  # NORMALIZER GREP-ASSERT (CRITICAL — PR #459/#460 root cause; validator p1).
  # entity_name_normalize MUST be present, ucc_normalize + normalize_party_name
  # MUST both be absent on BOTH sides.
  grep -qE "from scripts\._lib\.entity_name_normalize import" "$f" &&
  grep -qE "normalize_entity_name" "$f" &&
  ! grep -qE "ucc_normalize" "$f" &&
  ! grep -qE "normalize_party_name" "$f"
'

# ── s2: ops.data_sources migration (1 INSERT, ON CONFLICT (display_name)) ── #
# Migration filename: {YYYYMMDDHHMMSS}_ucc_ca_lender_sos_ca_owner_data_source.sql
# Same shape as 20260516035310_ca_sos_data_sources.sql (UNIQUE column is
# display_name, not source_name). Uses ON CONFLICT (display_name) DO UPDATE
# (or DO NOTHING — both idempotent and accepted).
run_surface "s2" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_ucc_ca_lender_sos_ca_owner_data_source.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # display_name row for the new bridge
  grep -qE "ucc_ca_lender_sos_ca_owner_lance" "$mig" &&
  # Idempotency on display_name UNIQUE
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # Format=lance, status=active
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_ucc_ca_lender_sos_ca_owner_data_source\.sql$"
'

# ── s2 (POPULATED): post-apply row count ────────────────────────────── #
# After migration applies, ops.data_sources MUST contain the row.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s2-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name='"'"'ucc_ca_lender_sos_ca_owner_lance'"'"'")
    test "$COUNT" = "1"
  '
fi

# ── s3: lance_views.py — 1 new LanceView entry (register_at_boot=False) ── #
# Per-key lookups use lance.dataset(uri).scanner() directly; the DuckDB view
# at boot would materialize the bridge fully — wasteful for an on-demand path.
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  # New view name (accept either of two reasonable spellings)
  grep -qE "ucc_ca_lender_sos_ca_owner_lance_raw|bridges_ucc_ca_lender_sos_ca_owner_lance_raw" "$f" &&
  # New Lance URI
  grep -qE "polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance" "$f" &&
  # register_at_boot=False adjacent to the new entry (large multi-million-row
  # dataset, hot path goes through scanner(filter=...) not the boot Arrow bridge)
  awk "/ucc_ca_lender_sos_ca_owner_lance/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s4: Polaris Generic Table registration — 1 (idempotent GET-first) ─── #
# Operator runs once post-Lance-build:
#   doppler run --project hq-all --config prd -- python3 \
#     apps/data-engine-x/scripts/init_polaris_lance_generic.py \
#     --namespace bridges --table ucc_ca_lender_sos_ca_owner_lance \
#     --doc "UCC CA lender × CA SoS owner bridge"
# POPULATED: GET via init_polaris_lance_generic.py --check-only (returns HTTP 200).
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s4-polaris" "hq-all" '_polaris_lance_check "bridges" "ucc_ca_lender_sos_ca_owner_lance"'
else
  echo "-- s4 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET check)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s5: Railway data-engine-x deploy status + runtime probe ───────────── #
# This cycle does NOT add new FastAPI endpoints (only a register_at_boot=False
# LanceView entry). The default runtime probe is sufficient per
# apps/data-engine-x/CLAUDE.md §"Deploy verification". Probe URL:
# https://api.dataengine.run (NOT api.opsengine.run — that's hq-x; PR #465 fixed
# the harness URL typo in the predecessor cycle).
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s5-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-ucc-sos-sha.$$
      actual_sha=\$(cat /tmp/dex-ucc-sos-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-ucc-sos-sha.$$
      [[ -n \"\$actual_sha\" ]] || { echo \"FAIL: Railway returned no SUCCESS deployment SHA\" >&2; exit 1; }
      case \"\$actual_sha\" in
        $MERGE_SHA*) exit 0 ;;
        *)
          case \"$MERGE_SHA\" in
            \$actual_sha*) exit 0 ;;
          esac
          echo \"FAIL: Railway latest .meta.commitHash=\$actual_sha != \$MERGE_SHA\" >&2
          exit 1
          ;;
      esac
    "
  '
  run_surface "s5-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s5 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# ── POPULATED block: Lance row-count floor + BTREE checks + ops row ──── #
# Floor from the directive's `## Volume floors` table: 14,929 (validator-
# calibrated full-corpus distinct-name overlap × 0.5 safety factor).
# Set POPULATED=1 AFTER s1 Modal one-shot run has produced the Lance dataset.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "floor-bridge" "hq-all" '
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance" 14929
  '
  run_surface "btree-bridge-secured-party-name" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance" "secured_party_name_normalized"
  '
  run_surface "btree-bridge-entity-num" "hq-all" '
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_lender_sos_ca_owner_lance" "entity_num"
  '
  # ops.bridge_generation_runs: one completed run with rows_matched >= floor
  run_surface "bridge-ops-row" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridge_generation_runs WHERE bridge_name='"'"'ucc_ca_lender_sos_ca_owner'"'"' AND status='"'"'completed'"'"' AND rows_matched >= 14929 LIMIT 1")
    test "$R" = "1"
  '
  # ops.bridges entry registered
  run_surface "bridge-ops-bridge-row" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.bridges WHERE bridge_name='"'"'ucc_ca_lender_sos_ca_owner'"'"' LIMIT 1")
    test "$R" = "1"
  '
  # CRITICAL p4 guard: the shared legal_name_state_exact_ca v1.0.0 method
  # version MUST still have its SBA-shape input_columns_left (legal_name_
  # normalized + borrstate). If the executor accidentally called
  # register_match_method_version with UCC-shape args (ORG_NAME), this row
  # would have been overwritten — and we need to know.
  run_surface "method-version-not-overwritten" "hq-all" '
    R=$(dex_psql_query "SELECT 1 FROM ops.match_method_versions v JOIN ops.match_methods m USING (match_method_id) WHERE m.method_name='"'"'legal_name_state_exact_ca'"'"' AND v.semver='"'"'1.0.0'"'"' AND '"'"'legal_name_normalized'"'"' = ANY(v.input_columns_left) AND '"'"'borrstate'"'"' = ANY(v.input_columns_left) LIMIT 1")
    test "$R" = "1"
  '
else
  echo "-- Lance row-count + BTREE + bridge-ops + method-version gates: SKIPPED (set POPULATED=1 after Modal one-shot run)"
  SKIP_COUNT=$((SKIP_COUNT+6))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
