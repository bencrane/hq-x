#!/usr/bin/env bash
# Verify harness for /scope cycle sec-10k-backfill-to-floor.
#
# Surfaces:
#   s4 — 10-K R2 backfill: row count across s3://dex-raw-landing-zone/sec-edgar/form-10k/**/*.parquet >= floor
#   s5 — 10-K Lance emit:   row count via pylance.dataset(...).count_rows() >= floor
#
# Normal floor: 50,000.
# Partial floor (--partial): 40,000 (80% of normal — accepted per parent contract).
#
# Verifier uses doppler-wrapped Python (boto3 + pyarrow + pylance) rather than
# DuckDB-via-bash, because the parent harness sec-edgar-feeds-backfill-completion.sh
# has a quoting bug that produces `R2_ENDPOINT_HOST: unbound variable` (verified
# at 2026-05-13T01:05Z). Python is the working pattern.
#
# Per directive ~/Desktop/hq/directives/2026-05-13-sec-10k-backfill-to-floor.md.
#
# Usage:
#   ./sec-10k-backfill-to-floor.sh                # both surfaces, normal floor
#   ./sec-10k-backfill-to-floor.sh --surface s4   # one surface
#   ./sec-10k-backfill-to-floor.sh --surface s5
#   ./sec-10k-backfill-to-floor.sh --partial      # lower floors to 40K
#   ./sec-10k-backfill-to-floor.sh --repo hq-all  # repo filter (no-op; single repo)
#
# Exit codes:
#   0 — all requested surfaces PASS
#   1 — any requested surface FAILs

set -euo pipefail

SURFACE_FILTER=""
REPO_FILTER=""
PARTIAL=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)  SURFACE_FILTER="$2"; shift 2 ;;
    --repo)     REPO_FILTER="$2";    shift 2 ;;
    --partial)  PARTIAL=1;           shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if (( PARTIAL )); then
  R2_FLOOR=40000
  LANCE_FLOOR=40000
  FLOOR_LABEL="partial(40K)"
else
  R2_FLOOR=50000
  LANCE_FLOOR=50000
  FLOOR_LABEL="normal(50K)"
fi

echo "==> Verifying surfaces for sec-10k-backfill-to-floor (surface=${SURFACE_FILTER:-all} repo=${REPO_FILTER:-all} floor=${FLOOR_LABEL})"

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER"    && "$REPO_FILTER"    != "$repo" ]]; then echo "-- $id ($repo): SKIPPED (repo filter)"; return 0; fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id"   ]]; then echo "-- $id ($repo): SKIPPED (surface filter)"; return 0; fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

# ----------------------------------------------------------------------------
# s4 — R2 parquet row count via doppler-wrapped python (boto3 + pyarrow).
# ----------------------------------------------------------------------------
r2_form10k_min_row_floor_check() {
  local floor="$1"
  local repo_root
  repo_root=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")
  local actual
  actual=$(doppler run --project hq-all --config prd -- bash -c "
    cd '$repo_root/apps/data-engine-x' && \
    uv run --with boto3 --with pyarrow python3 -c \"
import os, sys, boto3, io
import pyarrow.parquet as pq

s3 = boto3.client('s3',
    endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto')

total_rows = 0
parquet_files = 0
paginator = s3.get_paginator('list_objects_v2')
for page in paginator.paginate(Bucket='dex-raw-landing-zone', Prefix='sec-edgar/form-10k/'):
    for obj in page.get('Contents', []):
        key = obj['Key']
        if not key.endswith('.parquet'):
            continue
        try:
            r = s3.get_object(Bucket='dex-raw-landing-zone', Key=key)
            buf = io.BytesIO(r['Body'].read())
            pf = pq.ParquetFile(buf)
            total_rows += pf.metadata.num_rows
            parquet_files += 1
        except Exception as exc:
            print(f'WARN: failed to read metadata for {key}: {exc}', file=sys.stderr)

print(total_rows)
print(f'r2_parquet_files_seen={parquet_files}', file=sys.stderr)
\"
  " 2>&1 | tail -1 | tr -d '[:space:]')
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: s3://dex-raw-landing-zone/sec-edgar/form-10k/**/*.parquet has $actual rows >= floor $floor"
    return 0
  fi
  echo "FAIL: s3://dex-raw-landing-zone/sec-edgar/form-10k/**/*.parquet has '$actual' rows < floor $floor" >&2
  return 1
}

# ----------------------------------------------------------------------------
# s5 — Lance dataset row count via pylance (canonical).
# ----------------------------------------------------------------------------
lance_form10k_min_row_floor_check() {
  local floor="$1"
  local repo_root
  repo_root=$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")
  local actual
  actual=$(doppler run --project hq-all --config prd -- bash -c "
    cd '$repo_root/apps/data-engine-x' && \
    uv run --with pylance --with pyarrow python3 -c \"
import os, sys, lance
storage_options = {
    'endpoint':           os.environ['R2_ENDPOINT'],
    'access_key_id':      os.environ['R2_ACCESS_KEY_ID'],
    'secret_access_key':  os.environ['R2_SECRET_ACCESS_KEY'],
    'region':             'auto',
    'allow_http':         'false',
}
try:
    ds = lance.dataset('s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/', storage_options=storage_options)
    print(ds.count_rows())
except Exception as exc:
    print(f'LANCE_ERR: {exc}', file=sys.stderr)
    sys.exit(1)
\"
  " 2>&1 | tail -1 | tr -d '[:space:]')
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: lance form_10k_lance has $actual rows >= floor $floor"
    return 0
  fi
  echo "FAIL: lance form_10k_lance has '$actual' rows < floor $floor (or pylance error)" >&2
  return 1
}

run_surface "s4" "hq-all" "r2_form10k_min_row_floor_check $R2_FLOOR"
run_surface "s5" "hq-all" "lance_form10k_min_row_floor_check $LANCE_FLOOR"

echo "All requested surfaces verified."
