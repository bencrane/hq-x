#!/usr/bin/env bash
# s2: apps/data-engine-x/scripts/run_fl_cilb_lance_emit.py exists in hq-all
# working tree with load-bearing Pattern A inline lance.write_dataset shape
# (NOT via LanceEmitConfig — validator p1 from CSLB cycle). Dual BTREE on
# license_number + licensee_name_normalized.
#
# Code-presence + grep-shape check. Actual Lance dataset state is verified
# in s6 (operator bootstrap).
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

TARGET="/Users/benjamincrane/hq-all/apps/data-engine-x/scripts/run_fl_cilb_lance_emit.py"

if [[ ! -f "$TARGET" ]]; then
  echo "FAIL: $TARGET missing" >&2
  exit 1
fi

# --- Canonical imports (mirror PR #480 run_cslb_licensees_lance_emit.py) ---
grep -q "from scripts._lib.entity_name_normalize import" "$TARGET" || {
  echo "FAIL: scripts._lib.entity_name_normalize import missing in $TARGET — directive s1/L34 requires canonical normalizer for licensee_name_normalized computed column" >&2
  exit 1
}
grep -q "normalize_entity_name" "$TARGET" || {
  echo "FAIL: normalize_entity_name reference missing in $TARGET" >&2
  exit 1
}
grep -q "from scripts._lib.lance_commit_lock import" "$TARGET" || {
  echo "FAIL: scripts._lib.lance_commit_lock import missing in $TARGET — directive s2 requires write inside commit lock" >&2
  exit 1
}

# --- Per CSLB validator p1: MUST NOT use LanceEmitConfig (only supports one btree) ---
if grep -qE "LanceEmitConfig|from scripts\._lib\.lance_emit" "$TARGET"; then
  echo "FAIL: $TARGET uses LanceEmitConfig — forbidden per validator p1 from CSLB cycle (helper supports only one btree_column; this cycle needs dual BTREE)" >&2
  exit 1
fi

# --- Inline lance.write_dataset + dual BTREE ---
grep -q "lance.write_dataset" "$TARGET" || {
  echo "FAIL: lance.write_dataset call missing in $TARGET" >&2
  exit 1
}
grep -q "lance_commit_lock(" "$TARGET" || {
  echo "FAIL: lance_commit_lock(...) context manager not used in $TARGET" >&2
  exit 1
}
# Two consecutive create_scalar_index calls (dual BTREE per directive s2)
scalar_count=$(grep -c "create_scalar_index" "$TARGET" || true)
if [[ "$scalar_count" -lt 2 ]]; then
  echo "FAIL: expected >=2 create_scalar_index calls in $TARGET (dual BTREE on license_number + licensee_name_normalized); got $scalar_count" >&2
  exit 1
fi

# --- BTREE column targets ---
grep -q "license_number" "$TARGET" || {
  echo "FAIL: license_number column reference missing in $TARGET — required BTREE column (col 13 in FL DBPR CSV; canonical PK)" >&2
  exit 1
}
grep -q "licensee_name_normalized" "$TARGET" || {
  echo "FAIL: licensee_name_normalized column reference missing in $TARGET — required BTREE column (computed via normalize_entity_name)" >&2
  exit 1
}

# --- Lance dataset URI (polaris-warehouse/licensure/fl_cilb_lance per directive §Goal restated) ---
grep -qE "polaris-warehouse/licensure/fl_cilb_lance" "$TARGET" || {
  echo "FAIL: Lance dataset URI 'polaris-warehouse/licensure/fl_cilb_lance' missing in $TARGET" >&2
  exit 1
}

# --- LANCE_BYPASS_SPILLING=true env var per directive s2 / CSLB precedent ---
grep -q "LANCE_BYPASS_SPILLING" "$TARGET" || {
  echo "FAIL: LANCE_BYPASS_SPILLING env var missing in $TARGET — CSLB engineering-notes precedent requires this for the Lance writer" >&2
  exit 1
}

# --- DuckDB-on-R2 httpfs read (all_varchar=TRUE per L9) ---
grep -q "all_varchar" "$TARGET" || {
  echo "FAIL: all_varchar reference missing in $TARGET — L9 requires DuckDB-on-R2 read with all_varchar=TRUE" >&2
  exit 1
}
grep -qE "read_parquet|read_csv" "$TARGET" || {
  echo "FAIL: DuckDB read function missing in $TARGET — directive s2 requires DuckDB-on-R2 httpfs Parquet read" >&2
  exit 1
}

echo "s2 OK: $TARGET present; inline lance.write_dataset + dual create_scalar_index (license_number + licensee_name_normalized) + lance_commit_lock + canonical normalizer + LANCE_BYPASS_SPILLING verified; no LanceEmitConfig"
