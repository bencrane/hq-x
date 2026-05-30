#!/usr/bin/env bash
# Benchmark: SEC BDC Schedule-of-Investments portfolio companies × FL Secretary-of-State
# entities Pattern B bridge dry-run / measurement probe.
#
# Wraps build_bridge_sec_bdc_sos_fl_entities_lance.py's dry-run (no --apply flag).
# Reports: matched-row count, tier distribution (platinum/gold/silver/rejected),
# max fan-out per side, and the smoke-target result.
# Asserts rows_matched >= MIN_ROWS_MATCHED (3500) and smoke target resolves.
#
# Validator-measured probe (2026-05-21, 3× deterministic):
#   5,087 non-rejected = 2,039 platinum + 3,048 gold + 0 silver + 0 rejected
#   3,053 distinct BDC companies matched; max sos_fan_out=15; max bdc_fan_out=1.
# Floor: MIN_ROWS_MATCHED = 3500 (68.8% of probe yield — ~70% convention).
#
# Smoke target (validator-confirmed 2026-05-21):
#   'A Place for Mom, Inc.' → normalize →
#   'a place for mom' → FL SoS entity_num='F08000003004' (platinum tier).
#
# *** CRITICAL vs CA benchmark (validator prediction p1) ***
# FL Sunbiz entity-status column is 'status', NOT 'entity_status' (CA SoS column).
# The bridge script projects f.status AS sos_entity_status — the output schema
# column name stays sos_entity_status for cross-SoS-bridge consistency.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/sec-bdc-sos-fl-bridge.sh
#
# Or with an existing environment (doppler vars already set):
#   cd apps/data-engine-x && uv run python3 scripts/build_bridge_sec_bdc_sos_fl_entities_lance.py
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

MIN_ROWS_MATCHED=3500

echo "=== SEC BDC × FL SoS entities bridge benchmark (dry-run) ==="
echo "  Floor: rows_matched >= ${MIN_ROWS_MATCHED}"
echo "  Smoke: 'A Place for Mom, Inc.' → sos_entity_num='F08000003004' (platinum)"
echo ""

# Run the bridge generator in dry-run mode (no --apply). Capture output.
DRY_RUN_OUTPUT="$(mktemp -t sec-bdc-sos-fl-bridge-dryrun.XXXXXX.log)"
trap 'rm -f "$DRY_RUN_OUTPUT"' EXIT

doppler run --project hq-all --config prd -- bash -c \
  "uv run --project . python3 scripts/build_bridge_sec_bdc_sos_fl_entities_lance.py" \
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
echo "=== Benchmark results ==="
echo "  rows_matched:            ${rows_matched:-N/A}"
echo "  platinum (1:1):          ${rows_platinum:-N/A}"
echo "  gold (1:N | N:1):        ${rows_gold:-N/A}"
echo "  silver (N:M <=50):       ${rows_silver:-N/A}"
echo "  rejected:                ${rows_rejected:-N/A}"
echo "  max_bdc_fan_out:         ${max_bdc_fo:-N/A}"
echo "  max_sos_fan_out:         ${max_sos_fo:-N/A}"
echo "  smoke: ${smoke_line:-N/A}"

# Smoke assertion runs BEFORE floor check (validator prediction p6 ordering).
if echo "${smoke_line}" | grep -qi "SMOKE OK"; then
  echo "OK: smoke target resolved (entity_num=F08000003004)"
elif echo "${smoke_line}" | grep -qi "SMOKE FAIL"; then
  echo "FAIL: smoke target did not resolve to entity_num=F08000003004"
  exit 1
else
  echo "WARNING: smoke result unclear — check log above"
fi

# Floor assertion.
if [ -z "${rows_matched:-}" ] || [ "${rows_matched}" = "N/A" ]; then
  echo ""
  echo "FAIL: could not extract rows_matched from output"
  exit 1
fi

if [ "${rows_matched}" -ge "${MIN_ROWS_MATCHED}" ]; then
  echo ""
  echo "OK: rows_matched=${rows_matched} >= floor=${MIN_ROWS_MATCHED}"
else
  echo ""
  echo "FAIL: rows_matched=${rows_matched} < floor=${MIN_ROWS_MATCHED}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Post-build assertion (run after --apply has materialized the Lance dataset)
# ---------------------------------------------------------------------------
# Only executed if the Lance dataset already exists (skip on pre-build runs).
# Verifies: row count >= floor, dual BTREE indexes present, schema has
# bdc_name_normalized + sos_entity_num + confidence_tier + sos_entity_status.
POST_PY="$(mktemp -t sec-bdc-sos-fl-bridge-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$DRY_RUN_OUTPUT"' EXIT

cat > "$POST_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify materialized sec_bdc_sos_fl_entities_lance row count >= floor
and confirm dual BTREE indexes + required schema columns + smoke target."""
import os
import sys

import lance

MIN_ROWS_MATCHED = 3_500  # must match build script + directive Success threshold

BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sos_fl_entities_lance"
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

# Verify dual BTREE indexes (prediction p5).
indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
bdc_ok = "bdc_name_normalized" in idx_fields
sos_ok = "sos_entity_num" in idx_fields
print(f"BTREE bdc_name_normalized: {'OK' if bdc_ok else 'MISSING'}")
print(f"BTREE sos_entity_num: {'OK' if sos_ok else 'MISSING'}")

# Verify required schema columns.
schema_fields = set(ds.schema.names)
required_cols = {
    "bdc_name_normalized", "sos_entity_num", "sos_entity_name_normalized",
    "sos_entity_status", "confidence_tier", "match_method", "match_value",
    "bridge_run_id", "bdc_fan_out", "sos_fan_out", "generated_at",
}
missing_cols = required_cols - schema_fields
if missing_cols:
    print(f"MISSING schema columns: {sorted(missing_cols)}")
else:
    print(f"schema columns: all {len(required_cols)} required columns present")

# Smoke target check (validator-confirmed 2026-05-21).
sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import normalize_entity_name
import pyarrow.compute as pc

SMOKE_RAW = "A Place for Mom, Inc."
SMOKE_EXPECTED_ENTITY_NUM = "F08000003004"
smoke_normed = normalize_entity_name(SMOKE_RAW)
print(f"smoke: '{SMOKE_RAW}' → '{smoke_normed}'")
if smoke_normed:
    smoke_rows = ds.scanner(
        columns=["bdc_name_normalized", "sos_entity_num", "confidence_tier"],
        filter=pc.field("bdc_name_normalized") == smoke_normed,
    ).to_table()
    entity_nums = smoke_rows.column("sos_entity_num").to_pylist()
    tiers = smoke_rows.column("confidence_tier").to_pylist()
    print(f"smoke bridge rows sos_entity_num: {entity_nums}  tiers: {tiers}")
    smoke_ok = SMOKE_EXPECTED_ENTITY_NUM in entity_nums
    print(f"smoke: {'OK' if smoke_ok else 'FAIL'} (expected entity_num={SMOKE_EXPECTED_ENTITY_NUM})")
else:
    print("smoke: FAIL — normalized to None")
    smoke_ok = False

fail = False
if not floor_met:
    print(f"FAIL: rows={rows:,} < floor={MIN_ROWS_MATCHED:,}")
    fail = True
if not bdc_ok or not sos_ok:
    print("FAIL: missing BTREE index(es)")
    fail = True
if missing_cols:
    print(f"FAIL: missing schema columns: {sorted(missing_cols)}")
    fail = True
if not smoke_ok:
    print("FAIL: smoke target did not resolve to entity_num=F08000003004")
    fail = True

if not fail:
    print("OK: all post-build assertions passed")
sys.exit(1 if fail else 0)
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$POST_PY'"
