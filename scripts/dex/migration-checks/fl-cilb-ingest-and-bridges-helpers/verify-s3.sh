#!/usr/bin/env bash
# s3: apps/data-engine-x/scripts/build_bridge_sba_fl_cilb_lance.py exists in
# hq-all working tree with load-bearing Pattern B REUSER pattern + Modal-host
# wrapper.
#
# REUSER of legal_name_state_exact_fl v1.0.0 (publisher PR #467 sba_sos_fl_owner).
# L21: must NOT call register_match_method or register_match_method_version
# (would corrupt publisher's input_columns_left={legal_name_normalized,borrstate}
# config and trash the SBA × FL Sunbiz bridge's provenance trail).
#
# Mirror PR #467 build_bridge_sba_sos_fl_owner_lance.py shape EXCEPT for the
# REUSER stripping per L21 + the swapped RIGHT-side input (fl_cilb_lance instead
# of fl_entities_lance).
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

TARGET="/Users/benjamincrane/hq-all/apps/data-engine-x/scripts/build_bridge_sba_fl_cilb_lance.py"

if [[ ! -f "$TARGET" ]]; then
  echo "FAIL: $TARGET missing" >&2
  exit 1
fi

# --- Constants (load-bearing) ---
grep -q 'BRIDGE_NAME = "sba_fl_cilb"' "$TARGET" || {
  echo "FAIL: BRIDGE_NAME != \"sba_fl_cilb\" in $TARGET — must be naked, no _lance suffix (ops.bridges convention; see PR #466/PR #482/PR #487/PR #490 precedent)" >&2
  exit 1
}
grep -q 'METHOD_NAME = "legal_name_state_exact_fl"' "$TARGET" || {
  echo "FAIL: METHOD_NAME != \"legal_name_state_exact_fl\" in $TARGET — REUSE publisher (PR #467 sba_sos_fl_owner)" >&2
  exit 1
}
grep -qE 'METHOD_SEMVER = "1\.0\.0"' "$TARGET" || {
  echo "FAIL: METHOD_SEMVER != \"1.0.0\" in $TARGET — REUSE publisher (PR #467 v1.0.0)" >&2
  exit 1
}
grep -q 'COLLISION_THRESHOLD = 50' "$TARGET" || {
  echo "FAIL: COLLISION_THRESHOLD != 50 in $TARGET (precedent value)" >&2
  exit 1
}
# MIN_ROWS_MATCHED = 15000 per validator-refined calibration in directive volume floors
grep -qE 'MIN_ROWS_MATCHED = 15_?000([^0-9]|$)' "$TARGET" || {
  echo "FAIL: MIN_ROWS_MATCHED != 15000 in $TARGET — directive §Volume floors validator-refined calibration (50% of low-end expected; FAIL = canonical normalizer-drift signal)" >&2
  exit 1
}

# --- Canonical imports ---
grep -q "from scripts._lib.entity_name_normalize import" "$TARGET" || {
  echo "FAIL: canonical entity_name_normalize import missing in $TARGET (validator p1 — PR #459/#460 root cause)" >&2
  exit 1
}
grep -q "from scripts._lib.lance_commit_lock import" "$TARGET" || {
  echo "FAIL: lance_commit_lock import missing in $TARGET" >&2
  exit 1
}
grep -q "from scripts._lib.match_method_registry import" "$TARGET" || {
  echo "FAIL: match_method_registry import missing in $TARGET" >&2
  exit 1
}

# REUSER imports allowlist: must call register_bridge / start_bridge_run /
# complete_bridge_run / fail_bridge_run, must NOT call register_match_method*
grep -q "register_bridge" "$TARGET" || {
  echo "FAIL: register_bridge call/import missing in $TARGET" >&2
  exit 1
}
grep -q "start_bridge_run" "$TARGET" || {
  echo "FAIL: start_bridge_run call/import missing in $TARGET" >&2
  exit 1
}
grep -q "complete_bridge_run" "$TARGET" || {
  echo "FAIL: complete_bridge_run call/import missing in $TARGET" >&2
  exit 1
}
grep -q "fail_bridge_run" "$TARGET" || {
  echo "FAIL: fail_bridge_run call/import missing in $TARGET" >&2
  exit 1
}

# --- REUSER discipline (L21) — NO register_match_method or register_match_method_version ---
if grep -qE "register_match_method[^_]|register_match_method$" "$TARGET"; then
  echo "FAIL: register_match_method present in $TARGET — L21 violation; REUSER of legal_name_state_exact_fl v1.0.0 from PR #467 must omit (would corrupt publisher's ops.match_methods row)" >&2
  exit 1
fi
if grep -q "register_match_method_version" "$TARGET"; then
  echo "FAIL: register_match_method_version present in $TARGET — L21 violation; would corrupt publisher's input_columns_left={legal_name_normalized,borrstate} config from PR #467" >&2
  exit 1
fi

# --- LEFT input: SBA borrowers with borrstate='FL' filter (mirror PR #467 sba_sos_fl_owner) ---
grep -qE "polaris-warehouse/sba/borrowers_lance" "$TARGET" || {
  echo "FAIL: LEFT input URI 'polaris-warehouse/sba/borrowers_lance' missing in $TARGET" >&2
  exit 1
}
grep -q "borrstate" "$TARGET" || {
  echo "FAIL: borrstate filter missing in $TARGET — must filter SBA borrowers to borrstate='FL' (mirror PR #467 sba_sos_fl_owner shape)" >&2
  exit 1
}
grep -qE "['\"]FL['\"]" "$TARGET" || {
  echo "FAIL: 'FL' literal missing in $TARGET — required for borrstate='FL' filter" >&2
  exit 1
}

# --- RIGHT input: FL CILB Lance dataset (s2 output) ---
grep -qE "polaris-warehouse/licensure/fl_cilb_lance" "$TARGET" || {
  echo "FAIL: RIGHT input URI 'polaris-warehouse/licensure/fl_cilb_lance' missing in $TARGET — s2 output dataset" >&2
  exit 1
}

# --- Output: bridges/sba_fl_cilb_lance ---
grep -qE "polaris-warehouse/bridges/sba_fl_cilb_lance" "$TARGET" || {
  echo "FAIL: output URI 'polaris-warehouse/bridges/sba_fl_cilb_lance' missing in $TARGET" >&2
  exit 1
}

# --- Lance write inside commit lock ---
grep -q "lance.write_dataset" "$TARGET" || {
  echo "FAIL: lance.write_dataset call missing in $TARGET" >&2
  exit 1
}
grep -q "lance_commit_lock(" "$TARGET" || {
  echo "FAIL: lance_commit_lock(...) context manager not used in $TARGET" >&2
  exit 1
}

# --- BTREE on borrower_name_normalized (directive s3) ---
grep -q "borrower_name_normalized\|sba_legal_name_normalized" "$TARGET" || {
  echo "FAIL: borrower_name_normalized / sba_legal_name_normalized column reference missing in $TARGET — directive s3 requires BTREE on this join key" >&2
  exit 1
}
grep -q "create_scalar_index" "$TARGET" || {
  echo "FAIL: create_scalar_index call missing in $TARGET — required for BTREE creation" >&2
  exit 1
}

# --- Per-row provenance (L17: bridge_run_id UUID per row) ---
grep -q "bridge_run_id" "$TARGET" || {
  echo "FAIL: bridge_run_id column missing in $TARGET — L17 requires per-row UUID provenance stamping" >&2
  exit 1
}
grep -q "confidence_tier" "$TARGET" || {
  echo "FAIL: confidence_tier column missing in $TARGET — required for platinum/gold/silver/rejected tiering" >&2
  exit 1
}

# --- Modal-host wrapper (PR #467 + PR #490 precedent) ---
grep -qE "^import modal|^from modal" "$TARGET" || {
  echo "FAIL: 'import modal' missing in $TARGET — directive s3 is Modal-hosted bridge generator" >&2
  exit 1
}
grep -qF 'modal.App("data-engine-x-sba-fl-cilb-lance")' "$TARGET" || {
  echo "FAIL: modal.App(\"data-engine-x-sba-fl-cilb-lance\") missing in $TARGET — unique app name required" >&2
  exit 1
}
grep -q "@app.function" "$TARGET" || {
  echo "FAIL: @app.function decorator missing in $TARGET" >&2
  exit 1
}

echo "s3 OK: $TARGET present; REUSER pattern verified (legal_name_state_exact_fl v1.0.0, MIN_ROWS_MATCHED=15000, no register_match_method*, SBA borrstate=FL filter, FL CILB right side, dual BTREE, bridge_run_id per row, Modal-host wrapper)"
