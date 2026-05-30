#!/usr/bin/env bash
# s4: apps/data-engine-x/scripts/build_bridge_fl_cilb_sunbiz_lance.py exists in
# hq-all working tree with load-bearing Pattern B REUSER pattern + Modal-host
# wrapper.
#
# REUSER of legal_name_state_exact_fl v1.0.0 (same as s3; publisher PR #467).
# L21: must NOT call register_match_method or register_match_method_version.
#
# Mirror PR #482 build_bridge_cslb_sos_ca_owner_lance.py shape (the direct
# analog: license × state-entity Pattern B); REUSER of the FL-variant method.
# RIGHT-side input is sos/fl_entities_lance (PR #467, 12.6M rows).
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

TARGET="/Users/benjamincrane/hq-all/apps/data-engine-x/scripts/build_bridge_fl_cilb_sunbiz_lance.py"

if [[ ! -f "$TARGET" ]]; then
  echo "FAIL: $TARGET missing" >&2
  exit 1
fi

# --- Constants (load-bearing) ---
grep -q 'BRIDGE_NAME = "fl_cilb_sunbiz"' "$TARGET" || {
  echo "FAIL: BRIDGE_NAME != \"fl_cilb_sunbiz\" in $TARGET — must be naked, no _lance suffix (ops.bridges convention)" >&2
  exit 1
}
grep -q 'METHOD_NAME = "legal_name_state_exact_fl"' "$TARGET" || {
  echo "FAIL: METHOD_NAME != \"legal_name_state_exact_fl\" in $TARGET — REUSE publisher (PR #467 sba_sos_fl_owner)" >&2
  exit 1
}
grep -qE 'METHOD_SEMVER = "1\.0\.0"' "$TARGET" || {
  echo "FAIL: METHOD_SEMVER != \"1.0.0\" in $TARGET — REUSE publisher v1.0.0" >&2
  exit 1
}
grep -q 'COLLISION_THRESHOLD = 50' "$TARGET" || {
  echo "FAIL: COLLISION_THRESHOLD != 50 in $TARGET (precedent value)" >&2
  exit 1
}
# MIN_ROWS_MATCHED = 100000 per validator-refined calibration in directive volume floors
grep -qE 'MIN_ROWS_MATCHED = 100_?000([^0-9]|$)' "$TARGET" || {
  echo "FAIL: MIN_ROWS_MATCHED != 100000 in $TARGET — directive §Volume floors validator-refined calibration (~47% of expected ~212K; CSLB precedent rows_matched/rows_left=82.7%)" >&2
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

# REUSER imports allowlist
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

# --- REUSER discipline (L21) ---
if grep -qE "register_match_method[^_]|register_match_method$" "$TARGET"; then
  echo "FAIL: register_match_method present in $TARGET — L21 violation; REUSER of legal_name_state_exact_fl v1.0.0 from PR #467 must omit" >&2
  exit 1
fi
if grep -q "register_match_method_version" "$TARGET"; then
  echo "FAIL: register_match_method_version present in $TARGET — L21 violation; would corrupt publisher's input_columns_left={legal_name_normalized,borrstate} config" >&2
  exit 1
fi

# --- LEFT input: FL CILB Lance dataset (s2 output) ---
grep -qE "polaris-warehouse/licensure/fl_cilb_lance" "$TARGET" || {
  echo "FAIL: LEFT input URI 'polaris-warehouse/licensure/fl_cilb_lance' missing in $TARGET — s2 output dataset" >&2
  exit 1
}
grep -q "licensee_name_normalized" "$TARGET" || {
  echo "FAIL: licensee_name_normalized column reference missing in $TARGET — LEFT-side join key (computed in s2)" >&2
  exit 1
}

# --- RIGHT input: FL Sunbiz entities Lance (PR #467) ---
grep -qE "polaris-warehouse/sos/fl_entities_lance" "$TARGET" || {
  echo "FAIL: RIGHT input URI 'polaris-warehouse/sos/fl_entities_lance' missing in $TARGET — required FL Sunbiz dataset from PR #467 (12.6M rows)" >&2
  exit 1
}
grep -q "entity_name_normalized" "$TARGET" || {
  echo "FAIL: entity_name_normalized column reference missing in $TARGET — RIGHT-side join key from PR #467 emit" >&2
  exit 1
}

# --- Output: bridges/fl_cilb_sunbiz_lance ---
grep -qE "polaris-warehouse/bridges/fl_cilb_sunbiz_lance" "$TARGET" || {
  echo "FAIL: output URI 'polaris-warehouse/bridges/fl_cilb_sunbiz_lance' missing in $TARGET" >&2
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

# --- BTREE on licensee_name_normalized (directive s4) ---
grep -q "create_scalar_index" "$TARGET" || {
  echo "FAIL: create_scalar_index call missing in $TARGET — required for BTREE creation" >&2
  exit 1
}

# --- Per-row provenance ---
grep -q "bridge_run_id" "$TARGET" || {
  echo "FAIL: bridge_run_id column missing in $TARGET — L17 requires per-row UUID provenance stamping" >&2
  exit 1
}
grep -q "confidence_tier" "$TARGET" || {
  echo "FAIL: confidence_tier column missing in $TARGET — required for platinum/gold/silver/rejected tiering" >&2
  exit 1
}

# --- Modal-host wrapper ---
grep -qE "^import modal|^from modal" "$TARGET" || {
  echo "FAIL: 'import modal' missing in $TARGET — directive s4 is Modal-hosted bridge generator" >&2
  exit 1
}
grep -qF 'modal.App("data-engine-x-fl-cilb-sunbiz-lance")' "$TARGET" || {
  echo "FAIL: modal.App(\"data-engine-x-fl-cilb-sunbiz-lance\") missing in $TARGET — unique app name required" >&2
  exit 1
}
grep -q "@app.function" "$TARGET" || {
  echo "FAIL: @app.function decorator missing in $TARGET" >&2
  exit 1
}

echo "s4 OK: $TARGET present; REUSER pattern verified (legal_name_state_exact_fl v1.0.0, MIN_ROWS_MATCHED=100000, no register_match_method*, FL CILB left + FL Sunbiz entities right, BTREE, bridge_run_id per row, Modal-host wrapper)"
