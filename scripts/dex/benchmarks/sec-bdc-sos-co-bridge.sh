#!/usr/bin/env bash
# Benchmark: SEC BDC Schedule-of-Investments portfolio companies × CO Secretary-of-State
# entities Pattern B bridge dry-run / measurement probe.
#
# Wraps build_bridge_sec_bdc_sos_co_entities_lance.py's dry-run (no --apply flag).
# Reports: matched-row count (DISTINCT pairs), tier distribution (platinum/gold/silver/rejected),
# max fan-out per side, and the smoke-target result.
# Asserts rows_matched >= MIN_ROWS_MATCHED (2400) and smoke target resolves.
#
# *** GRAIN — CO-SPECIFIC (validator prediction p3) ***
# co_entities_lance is ONE-ROW-PER-ENTITY (3,049,389 rows / 3,049,389 distinct entityid;
# ratio 1.00000; zero null entityid — validator-confirmed 2026-05-21). Unlike NY (exact 2x
# duplicated dump), CO has NO source row duplication. The bridge still emits DISTINCT
# (bdc_name_normalized, sos_entity_id) pairs as a forward-safe contract (no-op on today's
# CO data, but mandatory per directive GRAIN section).
# If rows_matched differs from the raw INNER JOIN count, that is a regression indicator.
#
# Validator-measured floor-calibration probe (2026-05-21, 3x deterministic):
#   3,534 non-rejected DISTINCT pairs = 1,867 platinum + 1,667 gold + 0 silver + 0 rejected
#   Raw INNER JOIN COUNT(*) = 3,534 (equal to DISTINCT-pair count — confirms one-row-per-entity)
#   Max bdc_fan_out=1 (DISTINCT set by construction); max CO sos_fan_out=17.
# Floor: MIN_ROWS_MATCHED = 2400 (67.9% of 3,534 — ~70% convention).
#
# Smoke target (validator-confirmed 2026-05-21):
#   'Iron Mountain Information Management, LLC'
#   → normalize → 'iron mountain information management'
#   → CO SoS sos_entity_id='19971169145' (entitystatus 'Good Standing', platinum tier).
#
# *** CO-SPECIFIC vs NY benchmark ***
# - CO PK = entityid (NOT dos_id as NY SoS uses; NOT entity_num as CA/FL SoS use).
# - CO HAS a real entitystatus column (free-text: 'Good Standing', 'Delinquent', etc.).
#   DO NOT assert sos_entity_status='A' — that was an NY-only hack. CO has real statuses.
# - CO is one-row-per-entity (no 2x dup). Floor = 2400 (not 1600).
# - DISTINCT (bdc_name_normalized, sos_entity_id) pairs mandated (GRAIN p3 — no-op on CO).
# - Fan-out counted on COUNT(DISTINCT entityid) per name.
#
# *** NORMALIZER PARITY — validator p4 ***
# co_entities_lance.entity_name_normalized IS _lib-normalized (with CO-ingest pre-step
# that strips trailing registration-status suffixes from raw entityname before normalizing).
# ~75% of CO rows carry such a suffix. The bridge joins entity_name_normalized DIRECTLY —
# do NOT re-normalize from raw entityname. If the bridge re-normalizes the CO side,
# rows_matched will collapse far below 3,534 (status-suffix rows like
# 'denver gas, dissolved november 30, 1874' no longer match). If this benchmark reports
# rows_matched far below 3,534, p4 is the most likely cause.
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/sec-bdc-sos-co-bridge.sh
#
# Or with an existing environment (doppler vars already set):
#   cd apps/data-engine-x && uv run python3 scripts/build_bridge_sec_bdc_sos_co_entities_lance.py
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

MIN_ROWS_MATCHED=2400

echo "=== SEC BDC × CO SoS entities bridge benchmark (dry-run) ==="
echo "  Floor: rows_matched >= ${MIN_ROWS_MATCHED} (DISTINCT bdc_name+sos_entity_id pairs)"
echo "  Smoke: 'Iron Mountain Information Management, LLC'"
echo "         → normalize → 'iron mountain information management'"
echo "         → sos_entity_id='19971169145' (entitystatus='Good Standing', platinum)"
echo "  GRAIN: CO dataset is one-row-per-entity (raw-join == DISTINCT-pair count)"
echo ""

# Run the bridge generator in dry-run mode (no --apply). Capture output.
DRY_RUN_OUTPUT="$(mktemp -t sec-bdc-sos-co-bridge-dryrun.XXXXXX.log)"
trap 'rm -f "$DRY_RUN_OUTPUT"' EXIT

doppler run --project hq-all --config prd -- bash -c \
  "uv run --project . python3 scripts/build_bridge_sec_bdc_sos_co_entities_lance.py" \
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
echo "=== Benchmark results (DISTINCT bdc_name+sos_entity_id pairs) ==="
echo "  rows_matched:            ${rows_matched:-N/A}"
echo "  platinum (1:1):          ${rows_platinum:-N/A}"
echo "  gold (1:N | N:1):        ${rows_gold:-N/A}"
echo "  silver (N:M <=50):       ${rows_silver:-N/A}"
echo "  rejected:                ${rows_rejected:-N/A}"
echo "  max_bdc_fan_out:         ${max_bdc_fo:-N/A}"
echo "  max_sos_fan_out:         ${max_sos_fo:-N/A}  (COUNT(DISTINCT entityid) per name)"
echo "  smoke: ${smoke_line:-N/A}"

# Smoke assertion runs BEFORE floor check.
if echo "${smoke_line}" | grep -qi "SMOKE OK"; then
  echo "OK: smoke target resolved (sos_entity_id=19971169145)"
elif echo "${smoke_line}" | grep -qi "SMOKE FAIL"; then
  echo "FAIL: smoke target did not resolve to sos_entity_id=19971169145"
  exit 1
else
  echo "WARNING: smoke result unclear — check log above"
fi

# Floor assertion (on DISTINCT pairs — GRAIN p3).
if [ -z "${rows_matched:-}" ] || [ "${rows_matched}" = "N/A" ]; then
  echo ""
  echo "FAIL: could not extract rows_matched from output"
  exit 1
fi

# Grain sanity: CO is one-row-per-entity — warn if rows_matched != raw-join count.
# On CO, they should be equal (both ~3,534). A divergence would indicate either:
# (a) a DISTINCT-step regression causing double-counting, or
# (b) a future CO re-ingest that introduced source duplication.
# Since we can't easily compare against the raw-join count here (it's in the DuckDB
# session), we flag if rows_matched is suspiciously large compared to the floor.
# A raw-join count much larger than DISTINCT-pair count signals a dup regression.
# The generator logs: "CO one-row-per-entity so raw JOIN count == DISTINCT-pair count"
# — if that message disagrees with the tier counts, investigate.
if [ "${rows_matched:-0}" -gt 5000 ] 2>/dev/null; then
  echo ""
  echo "WARNING: rows_matched=${rows_matched} is larger than expected (~3,534)."
  echo "         This may indicate a regression in the DISTINCT-pair collapse or"
  echo "         a CO SoS re-ingest that introduced source row duplication."
  echo "         Verify raw INNER JOIN count == DISTINCT-pair count."
fi

if [ "${rows_matched}" -ge "${MIN_ROWS_MATCHED}" ]; then
  echo ""
  echo "OK: rows_matched=${rows_matched} >= floor=${MIN_ROWS_MATCHED} (DISTINCT pairs)"
else
  echo ""
  echo "FAIL: rows_matched=${rows_matched} < floor=${MIN_ROWS_MATCHED} (DISTINCT pairs)"
  echo "  Most likely cause if rows_matched is very low: CO-side re-normalization"
  echo "  (validator p4) — bridge must join entity_name_normalized directly, not"
  echo "  re-normalize from raw entityname (which carries ~75% status-suffix rows)."
  exit 1
fi

# ---------------------------------------------------------------------------
# Post-build assertion (run after --apply has materialized the Lance dataset)
# ---------------------------------------------------------------------------
# Only executed if the Lance dataset already exists (skip on pre-build runs).
# Verifies: row count >= floor, dual BTREE indexes present (bdc_name_normalized
# AND sos_entity_id), schema has required columns, no duplicate (bdc_name, entity_id)
# pairs, real CO entitystatus (not all-'A'), and smoke target resolves.
POST_PY="$(mktemp -t sec-bdc-sos-co-bridge-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$DRY_RUN_OUTPUT"' EXIT

cat > "$POST_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify materialized sec_bdc_sos_co_entities_lance.

Checks:
1. Row count >= floor (2400 DISTINCT pairs).
2. Dual BTREE indexes: bdc_name_normalized AND sos_entity_id (the CO PK).
3. Required schema columns present.
4. No duplicate (bdc_name_normalized, sos_entity_id) pairs in output
   (COUNT(*) == COUNT(DISTINCT pair) — key GRAIN invariant).
5. sos_entity_status is NOT all-'A' — CO has real free-text status values
   (Good Standing, Delinquent, Voluntarily Dissolved, etc.).
   If all rows are 'A', the NY literal was accidentally copied (p2 regression).
6. Smoke target 'iron mountain information management' → sos_entity_id='19971169145', platinum.
"""
import os
import sys

import lance

MIN_ROWS_MATCHED = 2_400  # must match build script + directive Success threshold

BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sos_co_entities_lance"
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

# Verify dual BTREE indexes (prediction p7).
# *** CO: 2nd BTREE is sos_entity_id NOT sos_dos_id (NY) / sos_entity_num (CA/FL) ***
indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
bdc_ok = "bdc_name_normalized" in idx_fields
sos_ok = "sos_entity_id" in idx_fields
print(f"BTREE bdc_name_normalized: {'OK' if bdc_ok else 'MISSING'}")
print(f"BTREE sos_entity_id: {'OK' if sos_ok else 'MISSING'}")

# Verify required schema columns (prediction p8: CO schema columns).
schema_fields = set(ds.schema.names)
required_cols = {
    "bdc_name_normalized", "sos_entity_id", "sos_entity_name_normalized",
    "sos_entity_status", "confidence_tier", "match_method", "match_value",
    "bridge_run_id", "bdc_fan_out", "sos_fan_out", "generated_at",
}
missing_cols = required_cols - schema_fields
if missing_cols:
    print(f"MISSING schema columns: {sorted(missing_cols)}")
else:
    print(f"schema columns: all {len(required_cols)} required columns present")

# Verify sos_entity_status is NOT all-'A'.
# *** CO-SPECIFIC (prediction p2): CO has real free-text entitystatus values. ***
# If all rows are 'A', the NY literal was accidentally copied — that is a p2 regression.
# We check that at least some rows have a non-null, non-'A' value.
import pyarrow.compute as pc
status_col = ds.scanner(columns=["sos_entity_status"]).to_table().column("sos_entity_status")
null_count = pc.sum(pc.is_null(status_col)).as_py() or 0
all_a_count = pc.sum(pc.equal(status_col, "A")).as_py() or 0
total_non_null = rows - null_count
all_a_ratio = all_a_count / rows if rows > 0 else 0
# Fail if ALL rows are 'A' (NY hack copied) or if sos_entity_status is all-null.
all_a_regression = (all_a_count == rows)
all_null_regression = (null_count == rows)
print(
    f"sos_entity_status: null={null_count:,}  all-A={all_a_count:,} ({all_a_ratio:.1%})  "
    f"real-CO-status={'OK' if not all_a_regression and not all_null_regression else 'FAIL (p2 regression)'}"
)
if all_a_regression:
    print("  NOTE: all sos_entity_status='A' — NY literal 'A' was accidentally copied (p2 regression)")
if all_null_regression:
    print("  NOTE: all sos_entity_status is null — entitystatus was not scanned from CO source")

# Verify no duplicate (bdc_name_normalized, sos_entity_id) pairs (GRAIN invariant p3).
import duckdb
tbl = ds.scanner(columns=["bdc_name_normalized", "sos_entity_id"]).to_table()
con = duckdb.connect()
con.register("bridge", tbl)
total_rows = con.execute("SELECT COUNT(*) FROM bridge").fetchone()[0]
distinct_pairs = con.execute(
    "SELECT COUNT(*) FROM (SELECT DISTINCT bdc_name_normalized, sos_entity_id FROM bridge)"
).fetchone()[0]
no_dups = total_rows == distinct_pairs
print(
    f"GRAIN duplicate check: total_rows={total_rows:,}  distinct_pairs={distinct_pairs:,}  "
    f"{'OK (no dups)' if no_dups else 'FAIL (DUPLICATES DETECTED — GRAIN regression)'}"
)

# Smoke target check (validator-confirmed 2026-05-21).
sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import normalize_entity_name

SMOKE_RAW = "Iron Mountain Information Management, LLC"
SMOKE_EXPECTED_ENTITY_ID = "19971169145"
smoke_normed = normalize_entity_name(SMOKE_RAW)
print(f"smoke: '{SMOKE_RAW}' → '{smoke_normed}'")
if smoke_normed:
    smoke_tbl = ds.scanner(
        columns=["bdc_name_normalized", "sos_entity_id", "sos_entity_status", "confidence_tier"],
        filter=pc.field("bdc_name_normalized") == smoke_normed,
    ).to_table()
    entity_ids = smoke_tbl.column("sos_entity_id").to_pylist()
    statuses = smoke_tbl.column("sos_entity_status").to_pylist()
    tiers = smoke_tbl.column("confidence_tier").to_pylist()
    print(f"smoke bridge rows sos_entity_id: {entity_ids}  statuses: {statuses}  tiers: {tiers}")
    smoke_ok = SMOKE_EXPECTED_ENTITY_ID in entity_ids
    print(f"smoke: {'OK' if smoke_ok else 'FAIL'} (expected sos_entity_id={SMOKE_EXPECTED_ENTITY_ID})")
    if smoke_ok:
        # Verify the matched row's sos_entity_status is the real CO value.
        idx = entity_ids.index(SMOKE_EXPECTED_ENTITY_ID)
        smoke_status = statuses[idx]
        smoke_tier = tiers[idx]
        print(f"  smoke sos_entity_status='{smoke_status}'  tier='{smoke_tier}'")
        if smoke_status == "A":
            print("  WARNING: smoke sos_entity_status='A' — CO should have real status")
else:
    print("smoke: FAIL — normalized to None")
    smoke_ok = False

fail = False
if not floor_met:
    print(f"FAIL: rows={rows:,} < floor={MIN_ROWS_MATCHED:,}")
    fail = True
if not bdc_ok or not sos_ok:
    print("FAIL: missing BTREE index(es) — need both bdc_name_normalized AND sos_entity_id")
    fail = True
if missing_cols:
    print(f"FAIL: missing schema columns: {sorted(missing_cols)}")
    fail = True
if all_a_regression:
    print("FAIL: all sos_entity_status='A' — p2 regression (NY literal copied to CO script)")
    fail = True
if all_null_regression:
    print("FAIL: all sos_entity_status is null — entitystatus not scanned from CO source")
    fail = True
if not no_dups:
    print("FAIL: duplicate (bdc_name_normalized, sos_entity_id) pairs in output — GRAIN regression")
    fail = True
if not smoke_ok:
    print("FAIL: smoke target did not resolve to sos_entity_id=19971169145")
    fail = True

if not fail:
    print("OK: all post-build assertions passed")
sys.exit(1 if fail else 0)
PYEOF

doppler run --project hq-all --config prd -- bash -c "uv run --project . python3 '$POST_PY'"
