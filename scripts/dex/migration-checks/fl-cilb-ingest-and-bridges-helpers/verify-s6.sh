#!/usr/bin/env bash
# s6: operator post-merge bootstrap verify. Asserts the data state landed after
# the operator runs the 7-step bootstrap from directive surface s6:
#   (a) modal deploy modal/fl_cilb_daily_app.py
#   (b) modal run --detach modal/fl_cilb_daily_app.py  (one-shot ingest to R2)
#   (c) modal run --detach scripts/run_fl_cilb_lance_emit.py
#   (d) init_polaris_lance_generic.py --namespace licensure --table fl_cilb_lance
#   (e) modal run --detach scripts/build_bridge_sba_fl_cilb_lance.py
#   (f) init_polaris_lance_generic.py --namespace bridges --table sba_fl_cilb_lance
#   (g) modal run --detach scripts/build_bridge_fl_cilb_sunbiz_lance.py
#   (h) init_polaris_lance_generic.py --namespace bridges --table fl_cilb_sunbiz_lance
#
# Asserts (per directive surface s6 "REQUIRES_AUDIT" spec):
#   1. Modal app data-engine-x-fl-cilb-daily deployed.
#   2. R2 release Parquet exists at fl-cilb/release=*/data.parquet with ContentType
#      application/x-parquet + no ContentEncoding (L42) + ContentLength > 5 MB (ZSTD Parquet).
#   3. Lance dataset licensure/fl_cilb_lance: count_rows >= 250,000;
#      schema includes licensee_name_normalized; BTREEs on license_number +
#      licensee_name_normalized.
#   4. Lance dataset bridges/sba_fl_cilb_lance: count_rows >= 15,000;
#      BTREE on borrower_name_normalized OR sba_legal_name_normalized;
#      per-row provenance (bridge_run_id, match_method, confidence_tier).
#   5. Lance dataset bridges/fl_cilb_sunbiz_lance: count_rows >= 100,000;
#      BTREE on licensee_name_normalized; per-row provenance.
#   6. ops.fl_cilb_r2_ingest_runs has >=1 row with status='completed'.
#   7. ops.bridge_generation_runs has >=2 rows with status='completed' (one per
#      new bridge: sba_fl_cilb + fl_cilb_sunbiz).
#   8. Polaris check-only exits 0 for all 3 generic-table registrations.
#
# Invocation: Lance + boto3 are NOT in system python3 — must run via
# `cd apps/data-engine-x && doppler run -- uv run python -` so deps resolve
# from apps/data-engine-x/pyproject.toml. Polaris check-only stays bare
# (uses requests only).
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="/Users/benjamincrane/hq-all"

# --- (1) Modal app deployed ---
if ! command -v modal >/dev/null 2>&1; then
  echo "FAIL: modal CLI not on PATH — cannot verify deploy state" >&2
  exit 1
fi
if ! modal app list --json 2>/dev/null | python3 -c "import json,sys; apps=json.load(sys.stdin); names=[a.get('Description') or a.get('name','') for a in apps if isinstance(a,dict)]; sys.exit(0 if 'data-engine-x-fl-cilb-daily' in names else 1)"; then
  echo "FAIL: modal app 'data-engine-x-fl-cilb-daily' NOT visible in 'modal app list --json' — directive s6 step (a) not completed" >&2
  exit 1
fi

# --- (6) DB: ops.fl_cilb_r2_ingest_runs has >=1 completed row ---
ingest_completed=$(dex_psql_query "SELECT count(*) FROM ops.fl_cilb_r2_ingest_runs WHERE status='completed'" | tr -d '[:space:]')
if [[ -z "$ingest_completed" || "$ingest_completed" == "0" ]]; then
  echo "FAIL: expected >=1 ops.fl_cilb_r2_ingest_runs row with status='completed'; got '$ingest_completed' — directive s6 step (b) not completed" >&2
  exit 1
fi

# --- (7) DB: ops.bridge_generation_runs has >=1 completed per new bridge ---
for bname in sba_fl_cilb fl_cilb_sunbiz; do
  runs_completed=$(dex_psql_query "SELECT count(*) FROM ops.bridge_generation_runs WHERE bridge_name = '$bname' AND status = 'completed'" | tr -d '[:space:]')
  if [[ -z "$runs_completed" || "$runs_completed" == "0" ]]; then
    echo "FAIL: expected >=1 ops.bridge_generation_runs row with status='completed' for bridge_name='$bname'; got '$runs_completed' — directive s6 bridge bootstrap not completed" >&2
    exit 1
  fi
done

# rows_matched per bridge >= floor (directive volume floors)
rows_sba=$(dex_psql_query "SELECT rows_matched FROM ops.bridge_generation_runs WHERE bridge_name = 'sba_fl_cilb' AND status = 'completed' ORDER BY started_at DESC LIMIT 1" | tr -d '[:space:]')
if [[ -z "$rows_sba" || "$rows_sba" -lt 15000 ]]; then
  echo "FAIL: ops.bridge_generation_runs latest rows_matched for bridges.sba_fl_cilb='$rows_sba' < floor 15,000 (validator-refined; FAIL = canonical normalizer-drift signal — re-grep s2/s3 for entity_name_normalize.normalize_entity_name on BOTH sides per PR #459/#460 root cause)" >&2
  exit 1
fi
rows_sunbiz=$(dex_psql_query "SELECT rows_matched FROM ops.bridge_generation_runs WHERE bridge_name = 'fl_cilb_sunbiz' AND status = 'completed' ORDER BY started_at DESC LIMIT 1" | tr -d '[:space:]')
if [[ -z "$rows_sunbiz" || "$rows_sunbiz" -lt 100000 ]]; then
  echo "FAIL: ops.bridge_generation_runs latest rows_matched for bridges.fl_cilb_sunbiz='$rows_sunbiz' < floor 100,000 (validator-refined; FAIL = normalizer-drift signal)" >&2
  exit 1
fi

# --- (2-5) R2 release Parquet + 3 Lance datasets + BTREEs + provenance cols ---
cd "$REPO_ROOT/apps/data-engine-x" && doppler run --project hq-all --config prd -- uv run --quiet python - <<'PY'
import os
import sys
import boto3
import lance

errors = []

opts = {
    "aws_endpoint": os.environ["R2_ENDPOINT"],
    "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
    "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
    "aws_region": "us-east-1",
    "aws_virtual_hosted_style_request": "false",
}

# --- (2) R2 release Parquet exists with correct ContentType, no ContentEncoding ---
try:
    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    paginator = s3.get_paginator("list_objects_v2")
    release_keys = []
    for page in paginator.paginate(
        Bucket="dex-raw-landing-zone",
        Prefix="fl-cilb/release=",
    ):
        for obj in page.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith("/data.parquet"):
                release_keys.append((key, obj.get("Size", 0)))
    if not release_keys:
        errors.append("no R2 object matching s3://dex-raw-landing-zone/fl-cilb/release=*/data.parquet — directive s6 step (b) did not land any release")
    else:
        # Check the latest by lexicographic order (release dates sort correctly)
        release_keys.sort()
        latest_key, latest_size = release_keys[-1]
        if latest_size <= 5 * 1024 * 1024:  # > 5 MB floor for ZSTD Parquet (~9 MB observed 2026-05-18)
            errors.append(f"R2 release latest size {latest_size} bytes <= 5 MB ({latest_key}) — ZSTD Parquet of 265K rows should be ~9 MB; raw CSV is 47.6 MB")
        head = s3.head_object(Bucket="dex-raw-landing-zone", Key=latest_key)
        ct = head.get("ContentType")
        if ct != "application/x-parquet":
            errors.append(f"wrong ContentType={ct} on {latest_key} (expected application/x-parquet)")
        if "ContentEncoding" in head:
            errors.append(f"L42 violation: ContentEncoding={head.get('ContentEncoding')} on {latest_key}")
except Exception as e:
    errors.append("R2 head_object failed: " + repr(e))

def _check_lance(uri: str, floor: int, required_btree_cols: set, required_provenance_cols: set | None = None, expected_schema_subset: set | None = None) -> None:
    try:
        ds = lance.dataset(uri, storage_options=opts)
        n = ds.count_rows()
        if n < floor:
            errors.append(f"{uri}: count_rows {n} < floor {floor}")
        indexed_cols = set()
        for i in ds.list_indices():
            cols = i.get("columns") or i.get("fields") or []
            if isinstance(cols, list):
                for c in cols:
                    indexed_cols.add(str(c))
            nm = i.get("name")
            if isinstance(nm, str):
                indexed_cols.add(nm)
        # accept any one of the required cols (some bridges name LEFT-side BTREE differently)
        if required_btree_cols and not (required_btree_cols & indexed_cols):
            errors.append(f"{uri}: missing BTREE — need any of {sorted(required_btree_cols)}, have {sorted(indexed_cols)}")
        schema_names = set(ds.schema.names)
        if required_provenance_cols:
            missing = required_provenance_cols - schema_names
            if missing:
                errors.append(f"{uri}: missing provenance cols {sorted(missing)}")
        if expected_schema_subset:
            missing = expected_schema_subset - schema_names
            if missing:
                errors.append(f"{uri}: missing expected schema cols {sorted(missing)}")
    except Exception as e:
        errors.append(f"{uri}: Lance check failed: {e!r}")

# --- (3) licensure.fl_cilb_lance ---
_check_lance(
    "s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance",
    floor=250_000,
    required_btree_cols={"license_number", "licensee_name_normalized"},
    expected_schema_subset={"license_number", "licensee_name_normalized"},
)
# Also check that BOTH license_number AND licensee_name_normalized BTREEs are present (dual BTREE per s2)
try:
    ds = lance.dataset("s3://dex-raw-landing-zone/polaris-warehouse/licensure/fl_cilb_lance", storage_options=opts)
    indexed_cols = set()
    for i in ds.list_indices():
        cols = i.get("columns") or i.get("fields") or []
        if isinstance(cols, list):
            for c in cols:
                indexed_cols.add(str(c))
        nm = i.get("name")
        if isinstance(nm, str):
            indexed_cols.add(nm)
    for need in ("license_number", "licensee_name_normalized"):
        if need not in indexed_cols:
            errors.append(f"licensure.fl_cilb_lance: missing dual-BTREE column '{need}'; have {sorted(indexed_cols)}")
except Exception as e:
    errors.append(f"licensure.fl_cilb_lance dual-BTREE check failed: {e!r}")

# --- (4) bridges.sba_fl_cilb_lance ---
_check_lance(
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_fl_cilb_lance",
    floor=15_000,
    required_btree_cols={"borrower_name_normalized", "sba_legal_name_normalized", "legal_name_normalized"},
    required_provenance_cols={"bridge_run_id", "match_method", "confidence_tier"},
)

# --- (5) bridges.fl_cilb_sunbiz_lance ---
_check_lance(
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/fl_cilb_sunbiz_lance",
    floor=100_000,
    required_btree_cols={"licensee_name_normalized"},
    required_provenance_cols={"bridge_run_id", "match_method", "confidence_tier"},
)

if errors:
    for e in errors:
        print("FAIL: " + e, file=sys.stderr)
    sys.exit(1)
print("s6 R2+Lance OK")
PY

# --- (8) Polaris check-only for all 3 generic tables ---
doppler run --project hq-all --config prd -- python3 "$REPO_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py" --namespace licensure --table fl_cilb_lance --check-only
doppler run --project hq-all --config prd -- python3 "$REPO_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py" --namespace bridges  --table sba_fl_cilb_lance --check-only
doppler run --project hq-all --config prd -- python3 "$REPO_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py" --namespace bridges  --table fl_cilb_sunbiz_lance --check-only

echo "s6 OK: Modal app deployed + R2 release Parquet landed + 3 Lance datasets present with floors + BTREEs + provenance + ops.fl_cilb_r2_ingest_runs ledger + ops.bridge_generation_runs (2 bridges) + 3 Polaris generic-tables all verified"
