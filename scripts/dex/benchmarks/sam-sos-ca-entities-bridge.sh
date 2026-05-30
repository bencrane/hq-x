#!/usr/bin/env bash
# Pre-build sanity probe for sam_sos_ca_entities_lance bridge.
#
# Reads sam_gov/entities_lance (CA-state filter on physical OR mailing) and
# sos/ca_entities_lance via PyLance + DuckDB Arrow bridge, applies
# `scripts._lib.entity_name_normalize.normalize_entity_name` Python-side on the
# SAM `legal_business_name` (the SAM-supplied legal_business_name_normalized
# column DIVERGES from _lib at ~8% — see validator notes), runs the bridge JOIN
# logic, and emits a single JSON object on stdout.
#
# Deterministic (3-run stdev verified 0.00% by validator 2026-05-19).
# Wall-clock <30s on M-series Mac via Doppler from the local worktree.
#
# Exit 0 + JSON on stdout = success. Any non-zero = harness break.
#
# Use:
#   bash apps/data-engine-x/scripts/benchmarks/sam-sos-ca-entities-bridge.sh
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
SOS_LANCE_URI = 's3://dex-raw-landing-zone/polaris-warehouse/sos/ca_entities_lance'

storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}

t0 = time.time()

# SAM left: CA-state filter on physical OR mailing
sam_ds = lance.dataset(SAM_LANCE_URI, storage_options=storage_options)
sam_filter = (
    (pc.field('physical_address_state_normalized') == 'CA')
    | (pc.field('mailing_address_state_or_province') == 'CA')
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
sos_ds = lance.dataset(SOS_LANCE_URI, storage_options=storage_options)
sos_schema_names = [f.name for f in sos_ds.schema][:25]

# Normalize Python-side (PRECEDENT: USAspending bridge — corpus is small enough)
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

# CA SoS
sos_tbl = sos_ds.scanner(
    columns=['entity_num', 'entity_name_normalized', 'entity_status'],
    filter=pc.field('entity_name_normalized').is_valid(),
).to_table()
sos_active = pc.sum(pc.equal(sos_tbl.column('entity_status'), 'Active')).as_py()

# DuckDB JOIN + tier
con = duckdb.connect()
con.execute("SET threads=4")
con.execute("SET memory_limit='8GB'")
con.register('sam', sam_arrow)
con.register('sos', sos_tbl)
con.execute("""
CREATE TEMP TABLE bridge_raw AS
SELECT l.sam_uei, l.sam_legal_name_normalized, s.entity_num AS sos_entity_num
FROM sam l JOIN sos s ON l.sam_legal_name_normalized = s.entity_name_normalized
""")
con.execute("""
CREATE TEMP TABLE sam_fanout AS
SELECT sam_legal_name_normalized, COUNT(*) AS sam_fan_out FROM bridge_raw GROUP BY 1
""")
con.execute("""
CREATE TEMP TABLE sos_fanout AS
SELECT sam_legal_name_normalized, COUNT(DISTINCT sos_entity_num) AS sos_fan_out FROM bridge_raw GROUP BY 1
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
    'sam_ca_state_rows': len(sam_tbl),
    'sam_distinct_uei_normalized_pairs': len(distinct),
    'sos_rows_normalized_valid': len(sos_tbl),
    'sos_active_rows': sos_active,
    'rows_pre_tier': con.execute("SELECT COUNT(*) FROM bridge_raw").fetchone()[0],
    'rows_matched_non_rejected': matched,
    'tier_distribution': tier_dist,
    'max_sam_fan_out': maxsam,
    'max_sos_fan_out': maxsos,
    'rows_collision_rejected': rejected,
    'elapsed_seconds': round(time.time() - t0, 2),
    'sam_schema_sample': sam_schema_names,
    'sos_schema_sample': sos_schema_names,
}
print(json.dumps(result, indent=2))
PYEOF
