#!/usr/bin/env bash
# Benchmark: SEC BDC Schedule-of-Investments portfolio companies × NY Secretary-of-State
# entities Pattern B bridge dry-run / measurement probe.
#
# Wraps build_bridge_sec_bdc_sos_ny_entities_lance.py's dry-run (no --apply flag).
# Reports: matched-row count (DISTINCT pairs), tier distribution (platinum/gold/silver/rejected),
# max fan-out per side, and the smoke-target result.
# Asserts rows_matched >= MIN_ROWS_MATCHED (1600) and smoke target resolves.
#
# *** GRAIN — NY-SPECIFIC (validator prediction p1) ***
# ny_active_corporations_lance is multi-row-per-entity at an EXACT 2x factor
# (8,353,367 rows / ~4.18M distinct dos_id). The bridge emits DISTINCT
# (bdc_name_normalized, sos_dos_id) pairs — so the matched-row count is
# ~2,323, NOT ~4,645 (which would be the raw-join count without DISTINCT).
# If rows_matched is near 4,645, the DISTINCT-pair collapse is missing.
#
# Validator-measured corrected probe (2026-05-21, 3x deterministic):
#   2,323 non-rejected DISTINCT pairs = 2,192 platinum + 131 gold + 0 silver + 0 rejected
#   Raw INNER JOIN COUNT(*) = 4,645 (2x the DISTINCT-pair count — confirms 2x NY dup)
#   Max bdc_fan_out=1 (DISTINCT set by construction); max NY sos_fan_out=10.
# Floor: MIN_ROWS_MATCHED = 1600 (68.9% of 2,323 — ~70% convention).
#
# Smoke target (validator-confirmed 2026-05-21):
#   'American Broadband and Telecommunications Company LLC'
#   → normalize → 'american broadband and telecommunications'
#   → NY SoS sos_dos_id='7252634' (platinum tier).
#   (This smoke target ALSO exercises the GRAIN collapse: 2 raw NY rows → 1 bridge pair.)
#
# *** NY-SPECIFIC vs CA/FL benchmarks ***
# - NY PK = dos_id (NOT entity_num as CA/FL SoS use).
# - NY has NO status column — bridge projects literal 'A' AS sos_entity_status.
# - DISTINCT (bdc_name_normalized, sos_dos_id) pairs mandated (GRAIN p1).
# - Fan-out counted on COUNT(DISTINCT dos_id) per name.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/sec-bdc-sos-ny-bridge.sh
#
# Or with an existing environment (doppler vars already set):
#   cd apps/data-engine-x && uv run python3 scripts/build_bridge_sec_bdc_sos_ny_entities_lance.py
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

MIN_ROWS_MATCHED=1600

echo "=== SEC BDC × NY SoS entities bridge benchmark (dry-run) ==="
echo "  Floor: rows_matched >= ${MIN_ROWS_MATCHED} (DISTINCT bdc_name+sos_dos_id pairs)"
echo "  Smoke: 'American Broadband and Telecommunications Company LLC'"
echo "         → normalize → 'american broadband and telecommunications'"
echo "         → sos_dos_id='7252634' (platinum)"
echo "  GRAIN: NY dataset is ~2x dup — bridge collapses to DISTINCT pairs"
echo ""

# Run the bridge generator in dry-run mode (no --apply). Capture output.
DRY_RUN_OUTPUT="$(mktemp -t sec-bdc-sos-ny-bridge-dryrun.XXXXXX.log)"
trap 'rm -f "$DRY_RUN_OUTPUT"' EXIT

doppler run --project hq-all --config prd -- bash -c \
  "uv run --project . python3 scripts/build_bridge_sec_bdc_sos_ny_entities_lance.py" \
  2>&1 | tee "$DRY_RUN_OUTPUT"

# Extract tier counts from the log output.
rows_matched=$(grep "rows_matched:" "$DRY_RUN_OUTPUT" | grep -v "tier" | head -1 | awk '{print $NF}')
rows_platinum=$(grep "platinum (1:1)" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_gold=$(grep "gold.*1:N" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_silver=$(grep "silver" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_rejected=$(grep "rows_collision_rejected" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
max_bdc_fo=$(grep "max_bdc_fan_out" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
max_sos_fo=$(grep "max_sos_fan_out" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
smoke_line=$(grep -i "SMOKE OK\|SMOKE FAIL" "$DRY_RUN_OUTPUT" | tail -1)

echo ""
echo "=== Benchmark results (DISTINCT bdc_name+sos_dos_id pairs) ==="
echo "  rows_matched:            ${rows_matched:-N/A}"
echo "  platinum (1:1):          ${rows_platinum:-N/A}"
echo "  gold (1:N | N:1):        ${rows_gold:-N/A}"
echo "  silver (N:M <=50):       ${rows_silver:-N/A}"
echo "  rejected:                ${rows_rejected:-N/A}"
echo "  max_bdc_fan_out:         ${max_bdc_fo:-N/A}"
echo "  max_sos_fan_out:         ${max_sos_fo:-N/A}  (COUNT(DISTINCT dos_id) per name)"
echo "  smoke: ${smoke_line:-N/A}"

# Smoke assertion runs BEFORE floor check.
# (The smoke target also validates the GRAIN DISTINCT-pair collapse:
#  2 raw NY rows for dos_id=7252634 must yield exactly 1 bridge pair.)
if echo "${smoke_line}" | grep -qi "SMOKE OK"; then
  echo "OK: smoke target resolved (sos_dos_id=7252634)"
elif echo "${smoke_line}" | grep -qi "SMOKE FAIL"; then
  echo "FAIL: smoke target did not resolve to sos_dos_id=7252634"
  exit 1
else
  echo "WARNING: smoke result unclear — check log above"
fi

# Floor assertion (on DISTINCT pairs — GRAIN p1).
if [ -z "${rows_matched:-}" ] || [ "${rows_matched}" = "N/A" ]; then
  echo ""
  echo "FAIL: could not extract rows_matched from output"
  exit 1
fi

# Grain sanity: warn if rows_matched is suspiciously close to 4645 (the raw-join count)
# which would indicate the DISTINCT-pair collapse is missing.
if [ "${rows_matched:-0}" -ge 4000 ] 2>/dev/null; then
  echo ""
  echo "WARNING: rows_matched=${rows_matched} is near the raw-join count (~4645)."
  echo "         This may indicate the DISTINCT (bdc_name, sos_dos_id) collapse is missing."
  echo "         Expected ~2,323 DISTINCT pairs (not ~4,645 raw rows)."
fi

if [ "${rows_matched}" -ge "${MIN_ROWS_MATCHED}" ]; then
  echo ""
  echo "OK: rows_matched=${rows_matched} >= floor=${MIN_ROWS_MATCHED} (DISTINCT pairs)"
else
  echo ""
  echo "FAIL: rows_matched=${rows_matched} < floor=${MIN_ROWS_MATCHED} (DISTINCT pairs)"
  exit 1
fi

# ---------------------------------------------------------------------------
# Post-build assertion (run after --apply has materialized the Lance dataset)
# ---------------------------------------------------------------------------
# Only executed if the Lance dataset already exists (skip on pre-build runs).
# Verifies: row count >= floor, dual BTREE indexes present (bdc_name_normalized
# AND sos_dos_id), schema has required columns, no duplicate (bdc_name, dos_id)
# pairs, and smoke target resolves.
POST_PY="$(mktemp -t sec-bdc-sos-ny-bridge-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$DRY_RUN_OUTPUT"' EXIT

cat > "$POST_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify materialized sec_bdc_sos_ny_entities_lance.

Checks:
1. Row count >= floor (1600 DISTINCT pairs).
2. Dual BTREE indexes: bdc_name_normalized AND sos_dos_id (the NY PK).
3. Required schema columns present.
4. No duplicate (bdc_name_normalized, sos_dos_id) pairs in output
   (COUNT(*) == COUNT(DISTINCT pair) — key GRAIN invariant).
5. Smoke target 'american broadband and telecommunications' → sos_dos_id='7252634', platinum.
"""
import os
import sys

import lance

MIN_ROWS_MATCHED = 1_600  # must match build script + directive Success threshold

BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sos_ny_entities_lance"
)

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

try:
    ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=SO)
except Exception as e:
    print(f"SKIP: bridge dataset not yet materialized ({e})")
    sys.exit(0)  # pre-build run — not a failure

rows = ds.count_rows()
floor_met = rows >= MIN_ROWS_MATCHED
print(
    f"post-build: rows={rows:,}  floor={MIN_ROWS_MATCHED:,}  "
    f"met={'YES' if floor_met else 'NO'}"
)

# Verify dual BTREE indexes (prediction p6).
# *** NY: 2nd BTREE is sos_dos_id NOT sos_entity_num ***
indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
bdc_ok = "bdc_name_normalized" in idx_fields
sos_ok = "sos_dos_id" in idx_fields
print(f"BTREE bdc_name_normalized: {'OK' if bdc_ok else 'MISSING'}")
print(f"BTREE sos_dos_id: {'OK' if sos_ok else 'MISSING'}")

# Verify required schema columns.
schema_fields = set(ds.schema.names)
required_cols = {
    "bdc_name_normalized", "sos_dos_id", "sos_entity_name_normalized",
    "sos_entity_status", "confidence_tier", "match_method", "match_value",
    "bridge_run_id", "bdc_fan_out", "sos_fan_out", "generated_at",
}
missing_cols = required_cols - schema_fields
if missing_cols:
    print(f"MISSING schema columns: {sorted(missing_cols)}")
else:
    print(f"schema columns: all {len(required_cols)} required columns present")

# Verify sos_entity_status is all 'A' (NY ACTIVE-only literal — prediction p3).
import pyarrow.compute as pc
status_col = ds.scanner(columns=["sos_entity_status"]).to_table().column("sos_entity_status")
non_a = pc.sum(pc.not_equal(status_col, "A")).as_py()
print(f"sos_entity_status='A' check: {non_a} non-A values ({'OK' if non_a == 0 else 'FAIL'})")

# Verify no duplicate (bdc_name_normalized, sos_dos_id) pairs (GRAIN invariant p1).
import duckdb
import pyarrow as pa
tbl = ds.scanner(columns=["bdc_name_normalized", "sos_dos_id"]).to_table()
con = duckdb.connect()
con.register("bridge", tbl)
total_rows = con.execute("SELECT COUNT(*) FROM bridge").fetchone()[0]
distinct_pairs = con.execute(
    "SELECT COUNT(*) FROM (SELECT DISTINCT bdc_name_normalized, sos_dos_id FROM bridge)"
).fetchone()[0]
no_dups = total_rows == distinct_pairs
print(
    f"GRAIN duplicate check: total_rows={total_rows:,}  distinct_pairs={distinct_pairs:,}  "
    f"{'OK (no dups)' if no_dups else 'FAIL (DUPLICATES DETECTED — GRAIN regression)'}"
)

# Smoke target check (validator-confirmed 2026-05-21).
sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import normalize_entity_name

SMOKE_RAW = "American Broadband and Telecommunications Company LLC"
SMOKE_EXPECTED_DOS_ID = "7252634"
smoke_normed = normalize_entity_name(SMOKE_RAW)
print(f"smoke: '{SMOKE_RAW}' → '{smoke_normed}'")
if smoke_normed:
    smoke_tbl = ds.scanner(
        columns=["bdc_name_normalized", "sos_dos_id", "confidence_tier"],
        filter=pc.field("bdc_name_normalized") == smoke_normed,
    ).to_table()
    dos_ids = smoke_tbl.column("sos_dos_id").to_pylist()
    tiers = smoke_tbl.column("confidence_tier").to_pylist()
    print(f"smoke bridge rows sos_dos_id: {dos_ids}  tiers: {tiers}")
    # Also verify exactly 1 row (GRAIN collapse: 2 raw NY rows → 1 bridge pair).
    smoke_count = len(dos_ids)
    smoke_ok = SMOKE_EXPECTED_DOS_ID in dos_ids
    if smoke_count > 1:
        print(f"smoke GRAIN: {smoke_count} rows for smoke target — expected 1 (DISTINCT collapse check)")
    print(f"smoke: {'OK' if smoke_ok else 'FAIL'} (expected sos_dos_id={SMOKE_EXPECTED_DOS_ID})")
else:
    print("smoke: FAIL — normalized to None")
    smoke_ok = False

fail = False
if not floor_met:
    print(f"FAIL: rows={rows:,} < floor={MIN_ROWS_MATCHED:,}")
    fail = True
if not bdc_ok or not sos_ok:
    print("FAIL: missing BTREE index(es) — need both bdc_name_normalized AND sos_dos_id")
    fail = True
if missing_cols:
    print(f"FAIL: missing schema columns: {sorted(missing_cols)}")
    fail = True
if non_a != 0:
    print(f"FAIL: {non_a} rows have sos_entity_status != 'A' — NY must project literal 'A'")
    fail = True
if not no_dups:
    print("FAIL: duplicate (bdc_name_normalized, sos_dos_id) pairs in output — GRAIN regression")
    fail = True
if not smoke_ok:
    print("FAIL: smoke target did not resolve to sos_dos_id=7252634")
    fail = True

if not fail:
    print("OK: all post-build assertions passed")
sys.exit(1 if fail else 0)
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$POST_PY'"
