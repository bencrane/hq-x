#!/usr/bin/env bash
# Pre-build sanity probe for sam_sos_ny_entities_lance bridge (Stage 2 validator).
#
# Reads sam_gov/entities_lance (NY-state filter on physical OR mailing) and
# sos/ny_active_corporations_lance via PyLance + DuckDB Arrow bridge, applies
# `scripts._lib.entity_name_normalize.normalize_entity_name` Python-side on the
# SAM `legal_business_name` (the SAM-supplied legal_business_name_normalized
# column DIVERGES from _lib at ~8% — see CA/FL precedent validator notes),
# runs the bridge JOIN logic, and emits a single JSON object on stdout.
#
# Mirror of scripts/benchmarks/sam-sos-ca-entities-bridge.sh with NY substitutions:
#   - State filter value: 'NY' instead of 'CA'
#   - Right-side dataset: sos/ny_active_corporations_lance (4.2M rows)
#   - Right-side PK column: dos_id (NOT entity_num)
#   - Right-side has NO status column — Active-only by source design
#
# REUSER pattern (validator-corrected 2026-05-19):
#   The directive's PUBLISHER framing was WRONG. legal_name_state_exact_ny v1.0.0
#   was published by PR #513 (2026-05-18) and serves 6 sibling bridges. This
#   probe confirms the recipe shape; the build script (c1) must use REUSER
#   pattern (only register_bridge + start_bridge_run + complete/fail_bridge_run).
#
# Deterministic. Wall-clock <30s from local Mac via Doppler.
# Exit 0 + JSON on stdout = success. Any non-zero = harness break.
#
# Use:
#   bash apps/data-engine-x/scripts/migration-checks/sam-sos-ny-entities-bridge-probe.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)/apps/data-engine-x"

doppler run --project hq-all --config prd -- uv run --project . python <<'PYEOF'
import json, os, sys, time
import lance
import pyarrow as pa
import pyarrow.compute as pc
import duckdb
sys.path.insert(0, 'scripts')
from _lib.entity_name_normalize import normalize_entity_name, __version__ as NORMALIZER_VERSION

COLLISION_THRESHOLD = 50
SAM_LANCE_URI = 's3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance'
NY_LANCE_URI  = 's3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance'

storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}

t0 = time.time()

# SAM left: NY-state filter on physical OR mailing
sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
sam_filter = (
    (pc.field('physical_address_state_normalized') == 'NY')
    | (pc.field('mailing_address_state_or_province') == 'NY')
)
sam_tbl = sam_ds.scanner(
    columns=[
        'unique_entity_id',
        'legal_business_name',
        'physical_address_state_normalized',
        'mailing_address_state_or_province',
    ],
    filter=sam_filter,
).to_table()

# Schema dump (top-level only)
sam_schema_names = [f.name for f in sam_ds.schema][:25]
ny_ds = lance.dataset(NY_LANCE_URI, storage_options=storage_options)
ny_schema_names = [f.name for f in ny_ds.schema][:25]

# Normalize Python-side
names = sam_tbl.column('legal_business_name').to_pylist()
ueis = sam_tbl.column('unique_entity_id').to_pylist()
normalized = [normalize_entity_name(n) for n in names]
distinct = set()
for uei, nm in zip(ueis, normalized):
    if uei and nm:
        distinct.add((uei, nm))
sam_arrow = pa.table({
    'sam_uei': pa.array([r[0] for r in distinct], type=pa.string()),
    'sam_legal_name_normalized': pa.array([r[1] for r in distinct], type=pa.string()),
})

# NY SoS — pre-normalized entity_name_normalized; PK is dos_id; NO status column
ny_tbl = ny_ds.scanner(
    columns=['dos_id', 'entity_name_normalized'],
    filter=pc.field('entity_name_normalized').is_valid(),
).to_table()

# DuckDB JOIN + symmetric tier
con = duckdb.connect()
con.execute("SET threads=4")
con.execute("SET memory_limit='8GB'")
con.register('sam', sam_arrow)
con.register('ny', ny_tbl)
con.execute("""
CREATE TEMP TABLE bridge_raw AS
SELECT l.sam_uei, l.sam_legal_name_normalized, n.dos_id AS sos_dos_id
FROM sam l JOIN ny n ON l.sam_legal_name_normalized = n.entity_name_normalized
""")
con.execute("""
CREATE TEMP TABLE sam_fanout AS
SELECT sam_legal_name_normalized, COUNT(*) AS sam_fan_out FROM bridge_raw GROUP BY 1
""")
con.execute("""
CREATE TEMP TABLE sos_fanout AS
SELECT sam_legal_name_normalized, COUNT(DISTINCT sos_dos_id) AS sos_fan_out FROM bridge_raw GROUP BY 1
""")
con.execute(f"""
CREATE TEMP TABLE bridge_all AS
SELECT b.*, sf_sam.sam_fan_out, sf_sos.sos_fan_out,
  CASE
    WHEN sf_sam.sam_fan_out > {COLLISION_THRESHOLD} OR sf_sos.sos_fan_out > {COLLISION_THRESHOLD} THEN 'rejected'
    WHEN sf_sam.sam_fan_out = 1 AND sf_sos.sos_fan_out = 1 THEN 'platinum'
    WHEN sf_sam.sam_fan_out = 1 OR sf_sos.sos_fan_out = 1 THEN 'gold'
    ELSE 'silver'
  END AS confidence_tier
FROM bridge_raw b
JOIN sam_fanout sf_sam USING (sam_legal_name_normalized)
JOIN sos_fanout sf_sos USING (sam_legal_name_normalized)
""")

tier_rows = con.execute("""
SELECT confidence_tier, COUNT(*) FROM bridge_all GROUP BY 1
""").fetchall()
tier_dist = {t: c for t, c in tier_rows}
matched = sum(c for t, c in tier_dist.items() if t != 'rejected')
maxsam, maxsos = con.execute("SELECT MAX(sam_fan_out), MAX(sos_fan_out) FROM bridge_all").fetchone()
rejected = tier_dist.get('rejected', 0)

result = {
    'normalizer_version': NORMALIZER_VERSION,
    'sam_ny_state_rows': len(sam_tbl),
    'sam_distinct_uei_normalized_pairs': len(distinct),
    'ny_rows_normalized_valid': len(ny_tbl),
    'ny_total_rows': ny_ds.count_rows(),
    'rows_pre_tier': con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0],
    'rows_matched_non_rejected': matched,
    'tier_distribution': tier_dist,
    'max_sam_fan_out': maxsam,
    'max_sos_fan_out': maxsos,
    'rows_collision_rejected': rejected,
    'elapsed_seconds': round(time.time() - t0, 2),
    'sam_schema_sample': sam_schema_names,
    'ny_schema_sample': ny_schema_names,
}
print(json.dumps(result, indent=2))
PYEOF
