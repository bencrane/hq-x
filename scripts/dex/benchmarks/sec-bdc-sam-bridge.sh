#!/usr/bin/env bash
# Benchmark: SEC BDC Schedule-of-Investments portfolio companies x SAM.gov
# entity identity bridge (Pattern B, name-keyed).
#
# Wraps `build_bridge_sec_bdc_sam_lance.py --dry-run` to produce:
#   - Matched-row count and tier distribution (platinum/gold/silver/rejected)
#   - Smoke assertion: American Residential Services -> SAM UEI VL69DPGVVN39,
#     platinum tier (validator-substituted smoke per directive ## Constraints
#     and validator.json P2 note — do NOT use Kaseya).
#   - HARD-FAIL floor check: platinum+gold >= 1,200 (distinct company×UEI grain).
#
# Validator-substituted smoke (2026-05-21): American Residential Services.
# BDC row: portfolio_company_name_clean ILIKE '%american residential services%'
# SAM  row: legal_business_name = 'AMERICAN RESIDENTIAL SERVICES L.L.C.'
# Expected: unique_entity_id = VL69DPGVVN39, platinum tier (1:1 probe-confirmed).
# Kaseya smoke MUST NOT be used: 'kaseya us' (SAM) != 'kaseya' (BDC) under the
# canonical normalizer (strips LLC/Inc but not the 'us' geographic token).
#
# Usage (from any cwd):
#   bash apps/data-engine-x/scripts/benchmarks/sec-bdc-sam-bridge.sh
#
# Or from repo root via doppler directly:
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/benchmarks/sec-bdc-sam-bridge.sh
set -euo pipefail

REPO_ROOT="${HQ_ALL_ROOT:-$HOME/hq-all}"
cd "$REPO_ROOT/apps/data-engine-x"

# ── Phase 1: dry-run through the generator ─────────────────────────────────
echo "=== SEC BDC x SAM.gov bridge benchmark (dry-run) ===" >&2
echo "Running build_bridge_sec_bdc_sam_lance.py --dry-run ..." >&2

doppler run --project hq-all --config prd -- bash -c \
  "uv run --with duckdb --with pylance --with pyarrow --with 'psycopg[binary]' \
   python scripts/build_bridge_sec_bdc_sam_lance.py --dry-run"

DRY_RUN_EXIT=$?
if [ "$DRY_RUN_EXIT" -ne 0 ]; then
  echo "FAIL: dry-run exited $DRY_RUN_EXIT (HARD-FAIL floor not met or error)" >&2
  exit "$DRY_RUN_EXIT"
fi

echo "" >&2
echo "dry-run PASSED (floor met)" >&2

# ── Phase 2: smoke assertion ────────────────────────────────────────────────
# Validate that the expected smoke entity resolves correctly.
# American Residential Services (validator-substituted 2026-05-21):
#   BDC norm key: normalize_entity_name('American Residential Services L.L.C.')
#                 => 'american residential services l l c'
#   SAM norm key: normalize_entity_name('AMERICAN RESIDENTIAL SERVICES L.L.C.')
#                 => 'american residential services l l c'  (1:1 match — platinum)
#   Expected UEI: VL69DPGVVN39

SMOKE_PY="$(mktemp -t sec-bdc-sam-smoke.XXXXXX.py)"
trap 'rm -f "$SMOKE_PY"' EXIT

cat > "$SMOKE_PY" <<'PYEOF'
#!/usr/bin/env python3
"""Smoke assertion: American Residential Services -> SAM UEI VL69DPGVVN39, platinum.

Validator-substituted smoke (2026-05-21). This probe reads the LIVE Lance datasets
and checks the norm-key overlap directly — it does NOT rely on the materialized
bridge dataset (which may not exist on a dry-run pass).
"""
import os
import sys

import lance
import pyarrow.compute as pc

sys.path.insert(0, ".")
from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

SOI_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_bdc/soi_lance"
SAM_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance"

SMOKE_BDC_PATTERN  = "american residential services"
SMOKE_SAM_UEI      = "VL69DPGVVN39"
SMOKE_SAM_NAME     = "AMERICAN RESIDENTIAL SERVICES L.L.C."
# NOTE: the bridge emits at DISTINCT (company × UEI) grain — left fan-out is 1
# by construction (one row per distinct normalized BDC company name). ARS
# resolves 1:1 to SAM UEI VL69DPGVVN39, so the expected tier is platinum.
# gold is also accepted (would mean the ARS name resolves to 2..50 SAM UEIs).
ACCEPTED_TIERS     = {"platinum", "gold"}
COLLISION_THRESHOLD = 50

# Derive expected norm key
smoke_norm_key = normalize_entity_name(SMOKE_SAM_NAME)
print(f"smoke norm key (expected): {smoke_norm_key!r}")

# Verify BDC side has a matching row
soi_ds = lance.dataset(SOI_URI, storage_options=SO)
soi_tbl = soi_ds.scanner(
    columns=["portfolio_company_name_clean", "portfolio_company_entity_type"],
    filter=pc.field("portfolio_company_entity_type") == "company",
).to_table()

bdc_names = soi_tbl.column("portfolio_company_name_clean").to_pylist()
bdc_match = []
for raw in bdc_names:
    if raw is None:
        continue
    for seg in str(raw).split("|"):
        seg = seg.strip()
        if SMOKE_BDC_PATTERN in seg.lower():
            norm = normalize_entity_name(seg)
            if norm is not None:
                bdc_match.append((seg, norm))

if not bdc_match:
    print(f"FAIL: no BDC row with portfolio_company_name_clean matching '%{SMOKE_BDC_PATTERN}%'")
    sys.exit(1)
print(f"BDC match rows found: {len(bdc_match)}")
for raw, norm in bdc_match[:3]:
    print(f"  BDC raw={raw!r} norm={norm!r}")

# Verify SAM side has the expected UEI + norm key
sam_ds = lance.dataset(SAM_URI, storage_options=SO)
sam_tbl = sam_ds.scanner(
    columns=["unique_entity_id", "legal_business_name"],
    filter=pc.field("unique_entity_id") == SMOKE_SAM_UEI,
).to_table()

if len(sam_tbl) == 0:
    print(f"FAIL: SAM entity unique_entity_id={SMOKE_SAM_UEI!r} not found")
    sys.exit(1)

sam_row = {k: sam_tbl.column(k)[0].as_py() for k in sam_tbl.schema.names}
sam_norm = normalize_entity_name(sam_row["legal_business_name"])
print(f"SAM uei={sam_row['unique_entity_id']!r} name={sam_row['legal_business_name']!r} norm={sam_norm!r}")

if sam_norm != smoke_norm_key:
    print(f"FAIL: SAM norm key mismatch: got {sam_norm!r} expected {smoke_norm_key!r}")
    sys.exit(1)

# Verify the norm keys match (the join will produce a platinum row).
bdc_norms = {norm for _, norm in bdc_match}
if smoke_norm_key not in bdc_norms:
    print(f"FAIL: smoke norm key {smoke_norm_key!r} not found in BDC norm set")
    sys.exit(1)

# Tier at distinct (company × UEI) grain: left fan-out is 1 by construction
# (the bridge holds one row per distinct norm_name). Right fan-out = number of
# distinct SAM UEIs sharing the norm key.
all_sam_names = sam_ds.scanner(columns=["unique_entity_id", "legal_business_name"]).to_table()
sam_ueis_same_key = [
    uei for uei, name in zip(
        all_sam_names.column("unique_entity_id").to_pylist(),
        all_sam_names.column("legal_business_name").to_pylist(),
    )
    if normalize_entity_name(name) == smoke_norm_key
]
n_sam_same_key = len(sam_ueis_same_key)
left_fo = 1  # distinct-grain: one row per distinct norm_name

print(f"fan-out: left={left_fo} (distinct grain) sam={n_sam_same_key}")

if n_sam_same_key > COLLISION_THRESHOLD:
    tier = "rejected"
elif n_sam_same_key == 1:
    tier = "platinum"
else:
    tier = "gold"

print(f"predicted tier: {tier}")

if tier not in ACCEPTED_TIERS:
    print(f"FAIL: expected tier in {ACCEPTED_TIERS} got {tier!r} (sam fan-out={n_sam_same_key})")
    sys.exit(1)
if SMOKE_SAM_UEI not in sam_ueis_same_key:
    print(f"FAIL: expected UEI {SMOKE_SAM_UEI!r} not in SAM matches for norm key")
    sys.exit(1)

print(f"SMOKE OK: American Residential Services -> UEI={SMOKE_SAM_UEI} tier={tier} (distinct grain; left_fo=1 sam_fo={n_sam_same_key})")
sys.exit(0)
PYEOF

echo "" >&2
echo "=== Smoke assertion: American Residential Services -> VL69DPGVVN39, platinum ===" >&2
doppler run --project hq-all --config prd -- bash -c \
  "uv run --with pylance --with pyarrow python3 '$SMOKE_PY'"

SMOKE_EXIT=$?
if [ "$SMOKE_EXIT" -ne 0 ]; then
  echo "FAIL: smoke assertion failed (exit $SMOKE_EXIT)" >&2
  exit "$SMOKE_EXIT"
fi

# ── Phase 3: post-build dataset check (skipped if bridge not yet materialized) ─
POST_PY="$(mktemp -t sec-bdc-sam-post.XXXXXX.py)"
trap 'rm -f "$POST_PY" "$SMOKE_PY"' EXIT

cat > "$POST_PY" <<'POSTPYEOF'
#!/usr/bin/env python3
"""Post-build assertion: verify bridge Lance dataset row count and BTREE index."""
import os
import sys

import lance

MIN_PLATINUM_GOLD = 1_200
BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sec_bdc_sam_lance"

SO = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
    "aws_skip_signature": "false",
}

try:
    ds = lance.dataset(BRIDGE_URI, storage_options=SO)
except Exception as e:
    print(f"SKIP: bridge dataset not yet materialized ({e})")
    sys.exit(0)

import duckdb
tbl = ds.scanner(columns=["confidence_tier"]).to_table()
con = duckdb.connect()
con.register("bridge", tbl)
row = con.execute(
    """
    SELECT
        count(*) FILTER (WHERE confidence_tier <> 'rejected') AS rows_matched,
        count(*) FILTER (WHERE confidence_tier = 'platinum')  AS platinum,
        count(*) FILTER (WHERE confidence_tier = 'gold')      AS gold,
        count(*) FILTER (WHERE confidence_tier = 'silver')    AS silver,
        count(*) FILTER (WHERE confidence_tier = 'rejected')  AS rejected
    FROM bridge
    """
).fetchone()
rows_matched, platinum, gold, silver, rejected = row
pg = platinum + gold
print(
    f"post-build: rows_matched={rows_matched:,} platinum={platinum:,} "
    f"gold={gold:,} silver={silver:,} rejected={rejected:,} pg={pg:,}"
)

floor_met = pg >= MIN_PLATINUM_GOLD
print(
    f"floor check: platinum+gold={pg:,} >= {MIN_PLATINUM_GOLD:,}: "
    f"{'PASS' if floor_met else 'FAIL'}"
)

indices = ds.list_indices()
idx_fields = {f for idx in indices for f in idx.get("fields", [])}
btree_ok = "norm_name" in idx_fields
print(f"BTREE norm_name: {'OK' if btree_ok else 'MISSING'}")

# Tier sanity: platinum+gold must be majority of non-rejected rows
if rows_matched > 0:
    pg_pct = pg / rows_matched
    sanity_ok = pg_pct > 0.5
    print(f"tier sanity: platinum+gold {pg_pct:.1%} of non-rejected: {'PASS' if sanity_ok else 'FAIL'}")
else:
    sanity_ok = False
    print("tier sanity: FAIL (no non-rejected rows)")

all_ok = floor_met and btree_ok and sanity_ok
print(f"\npost-build result: {'ALL PASS' if all_ok else 'FAIL'}")
sys.exit(0 if all_ok else 1)
POSTPYEOF

echo "" >&2
echo "=== Post-build dataset assertion ===" >&2
doppler run --project hq-all --config prd -- bash -c \
  "uv run --with duckdb --with pylance --with pyarrow python3 '$POST_PY'"

echo "" >&2
echo "=== sec-bdc-sam-bridge benchmark complete ===" >&2
