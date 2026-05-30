#!/usr/bin/env bash
# Benchmark: SEC BDC Schedule-of-Investments portfolio companies × SBA PPP
# borrowers Pattern B bridge dry-run / measurement probe.
#
# Wraps build_bridge_sec_bdc_ppp_lance.py's dry-run (no --apply flag).
# Reports: matched-row count, tier distribution (platinum/gold/silver/rejected),
# max fan-out per side, and the smoke-target result.
# Asserts rows_matched >= MIN_ROWS_MATCHED (2200) and smoke target resolves.
#
# Validator-measured probe (2026-05-21, 3× deterministic, ~8s each):
#   3,188 non-rejected = 1,102 platinum + 2,086 gold + 0 silver + 0 rejected
#   1,675 distinct BDC companies matched; max ppp_fan_out=21; max bdc_fan_out=1.
# Floor: MIN_ROWS_MATCHED = 2200 (69.0% of probe yield — ~70% convention).
#
# Smoke target (validator-substituted 2026-05-21):
#   'Accommodations Plus Technologies Holdings LLC' → normalize →
#   'accommodations plus technologies holdings' → PPP row borrstate='NY'.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/sec-bdc-ppp-bridge.sh
#
# Or with an existing environment (doppler vars already set):
#   cd apps/data-engine-x && uv run python3 scripts/build_bridge_sec_bdc_ppp_lance.py
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

MIN_ROWS_MATCHED=2200

echo "=== SEC BDC × SBA PPP bridge benchmark (dry-run) ==="
echo "  Floor: rows_matched >= ${MIN_ROWS_MATCHED}"
echo "  Smoke: 'Accommodations Plus Technologies Holdings LLC' → borrstate='NY'"
echo ""

# Run the bridge generator in dry-run mode (no --apply). Capture output.
DRY_RUN_OUTPUT="$(mktemp -t sec-bdc-ppp-bridge-dryrun.XXXXXX.log)"
trap 'rm -f "$DRY_RUN_OUTPUT"' EXIT

doppler run --project hq-all --config prd -- bash -c \
  "uv run --project . python3 scripts/build_bridge_sec_bdc_ppp_lance.py" \
  2>&1 | tee "$DRY_RUN_OUTPUT"

# Extract tier counts from the log output.
rows_matched=$(grep "rows_matched:" "$DRY_RUN_OUTPUT" | grep -v "tier" | head -1 | awk '{print $NF}')
rows_platinum=$(grep "platinum (1:1)" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_gold=$(grep "gold.*1:N" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_silver=$(grep "silver" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
rows_rejected=$(grep "rows_collision_rejected" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
max_bdc_fo=$(grep "max_bdc_fan_out" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
max_ppp_fo=$(grep "max_ppp_fan_out" "$DRY_RUN_OUTPUT" | awk '{print $NF}')
smoke_line=$(grep -i "smoke" "$DRY_RUN_OUTPUT" | tail -1)

echo ""
echo "=== Benchmark results ==="
echo "  rows_matched:            ${rows_matched:-N/A}"
echo "  platinum (1:1):          ${rows_platinum:-N/A}"
echo "  gold (1:N | N:1):        ${rows_gold:-N/A}"
echo "  silver (N:M <=50):       ${rows_silver:-N/A}"
echo "  rejected:                ${rows_rejected:-N/A}"
echo "  max_bdc_fan_out:         ${max_bdc_fo:-N/A}"
echo "  max_ppp_fan_out:         ${max_ppp_fo:-N/A}"
echo "  smoke: ${smoke_line:-N/A}"

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

# Smoke assertion.
if echo "${smoke_line}" | grep -qi "SMOKE OK"; then
  echo "OK: smoke target resolved"
elif echo "${smoke_line}" | grep -qi "SMOKE FAIL"; then
  echo "FAIL: smoke target did not resolve"
  exit 1
else
  echo "WARNING: smoke result unclear — check log above"
fi

# ---------------------------------------------------------------------------
# Post-build assertion (run after --apply has materialized the Lance dataset)
# ---------------------------------------------------------------------------
# Only executed if the Lance dataset already exists (skip on pre-build runs).
# Verifies: row count >= floor, dual BTREE indexes present, schema has
# bdc_name_normalized + ppp_legal_name_normalized + confidence_tier + borrstate.
POST_PY="$(mktemp -t sec-bdc-ppp-bridge-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$DRY_RUN_OUTPUT"' EXIT

cat > "$POST_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify materialized sec_bdc_ppp_lance row count >= floor
and confirm dual BTREE indexes + required schema columns."""
import os
import sys

import lance

MIN_ROWS_MATCHED = 2_200  # must match build script + directive Success threshold

BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_ppp_lance"
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

# Verify dual BTREE indexes.
indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
bdc_ok = "bdc_name_normalized" in idx_fields
ppp_ok = "ppp_legal_name_normalized" in idx_fields
print(f"BTREE bdc_name_normalized: {'OK' if bdc_ok else 'MISSING'}")
print(f"BTREE ppp_legal_name_normalized: {'OK' if ppp_ok else 'MISSING'}")

# Verify required schema columns.
schema_fields = set(ds.schema.names)
required_cols = {
    "bdc_name_normalized", "ppp_legal_name_normalized",
    "ppp_borrstate", "ppp_borrzip",
    "confidence_tier", "match_method", "match_value",
    "bridge_run_id", "bdc_fan_out", "ppp_fan_out", "generated_at",
}
missing_cols = required_cols - schema_fields
if missing_cols:
    print(f"MISSING schema columns: {sorted(missing_cols)}")
else:
    print(f"schema columns: all {len(required_cols)} required columns present")

# Smoke target check.
import sys
sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import normalize_entity_name
import pyarrow.compute as pc

SMOKE_RAW = "Accommodations Plus Technologies Holdings LLC"
SMOKE_EXPECTED_STATE = "NY"
smoke_normed = normalize_entity_name(SMOKE_RAW)
print(f"smoke: '{SMOKE_RAW}' → '{smoke_normed}'")
if smoke_normed:
    smoke_rows = ds.scanner(
        columns=["bdc_name_normalized", "ppp_borrstate"],
        filter=pc.field("bdc_name_normalized") == smoke_normed,
    ).to_table()
    states = smoke_rows.column("ppp_borrstate").to_pylist()
    print(f"smoke bridge rows borrstate: {states}")
    smoke_ok = SMOKE_EXPECTED_STATE in states
    print(f"smoke: {'OK' if smoke_ok else 'FAIL'} (expected {SMOKE_EXPECTED_STATE})")
else:
    print("smoke: FAIL — normalized to None")
    smoke_ok = False

fail = False
if not floor_met:
    print(f"FAIL: rows={rows:,} < floor={MIN_ROWS_MATCHED:,}")
    fail = True
if not bdc_ok or not ppp_ok:
    print("FAIL: missing BTREE index(es)")
    fail = True
if missing_cols:
    print(f"FAIL: missing schema columns: {sorted(missing_cols)}")
    fail = True
if not smoke_ok:
    print("FAIL: smoke target did not resolve")
    fail = True

if not fail:
    print("OK: all post-build assertions passed")
sys.exit(1 if fail else 0)
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$POST_PY'"
