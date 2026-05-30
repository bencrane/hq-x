#!/usr/bin/env bash
# Verify harness for /scope cycle sec-8k-layout-fix-and-backfill.
#
# Runs the 4 surface verify commands (s6a, s6b, s6, s7). Exits 0 only if every
# requested surface PASSes. s6b is OPTIONAL — validator-recommended SKIP.
#
# Per directive ~/Desktop/hq/directives/2026-05-13-sec-8k-layout-fix-and-backfill.md.
#
# Usage:
#   ./sec-8k-layout-fix-and-backfill.sh                  # run all surfaces (s6a + s6 + s7; s6b only if requested)
#   ./sec-8k-layout-fix-and-backfill.sh --surface s6a    # single-surface verify
#   ./sec-8k-layout-fix-and-backfill.sh --include-s6b    # include the optional smoke-cleanup surface
#
# Volume floors inherited verbatim from parent `## Volume floors`:
#   Item 2.03 R2:     5,000
#   All-items R2:    30,000
#   8-K Lance:       30,000
#
# Layout note: 8-K script's PER_YEAR_STREAMS flush at `year=Y/<stream>/data.parquet`
# (per-year cardinality optimization) and PER_QUARTER_STREAMS flush at
# `year=Y/quarter=Q/<stream>/data.parquet`. The verify globs use `**/item_*/`
# WITHOUT hive_partitioning=1 so both partition shapes union cleanly.

set -euo pipefail

# shellcheck source=./_lib-shim.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

SURFACE_FILTER=""
INCLUDE_S6B=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)     SURFACE_FILTER="$2"; shift 2 ;;
    --include-s6b) INCLUDE_S6B=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces for sec-8k-layout-fix-and-backfill (surface=${SURFACE_FILTER:-all-required} include-s6b=$INCLUDE_S6B)"

run_surface() {
  local id="$1" cmd="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then echo "-- $id: SKIPPED (surface filter)"; return 0; fi
  if [[ "$id" == "s6b" && "$INCLUDE_S6B" == "0" && -z "$SURFACE_FILTER" ]]; then echo "-- $id: SKIPPED (s6b is optional; pass --include-s6b to run)"; return 0; fi
  echo "-- $id: RUNNING"
  if eval "$cmd"; then
    echo "-- $id: PASS"
  else
    echo "-- $id: FAIL" >&2
    return 1
  fi
}

REPO_ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")"

# --- DuckDB-over-R2 floor check (reused pattern from parent harness) ----- #
r2_min_row_floor_check() {
  local glob="$1" floor="$2"
  local actual
  # shellcheck disable=SC2016
  actual=$(doppler run --project hq-all --config prd -- bash -c '
    R2_ENDPOINT_HOST="${R2_ENDPOINT#https://}"
    duckdb -noheader -csv -c "
      INSTALL httpfs; LOAD httpfs;
      SET s3_endpoint='\''${R2_ENDPOINT_HOST}'\'';
      SET s3_access_key_id='\''${R2_ACCESS_KEY_ID}'\'';
      SET s3_secret_access_key='\''${R2_SECRET_ACCESS_KEY}'\'';
      SET s3_region='\''auto'\'';
      SET s3_url_style='\''path'\'';
      SELECT COUNT(*) FROM read_parquet('\'''"$glob"''\'', union_by_name=true)
    " 2>&1 | tail -1
  ' 2>&1 | tr -d '[:space:]')
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: $glob has $actual rows >= floor $floor"
    return 0
  fi
  echo "FAIL: $glob has '$actual' rows < floor $floor (or DuckDB error)" >&2
  return 1
}

# --- Lance dataset rowcount via pylance --------------------------------- #
lance_min_row_floor_check() {
  local uri="$1" floor="$2"
  local actual
  # shellcheck disable=SC2016
  actual=$(doppler run --project hq-all --config prd -- bash -c '
    cd "'"$REPO_ROOT"'/apps/data-engine-x" && \
    uv run --with pylance --with pyarrow python3 -c "
import os, sys, lance
storage_options = {
    \"endpoint\":      os.environ[\"R2_ENDPOINT\"],
    \"access_key_id\": os.environ[\"R2_ACCESS_KEY_ID\"],
    \"secret_access_key\": os.environ[\"R2_SECRET_ACCESS_KEY\"],
    \"region\":        \"auto\",
    \"allow_http\":    \"false\",
}
try:
    ds = lance.dataset('\'''"$uri"''\'', storage_options=storage_options)
    print(ds.count_rows())
except Exception as exc:
    print(f\"LANCE_ERR: {exc}\", file=sys.stderr)
    sys.exit(1)
" 2>&1 | tail -1
  ' 2>&1 | tr -d '[:space:]')
  if [[ "$actual" =~ ^[0-9]+$ ]] && (( actual >= floor )); then
    echo "PASS: $uri has $actual Lance rows >= floor $floor"
    return 0
  fi
  echo "FAIL: $uri has '$actual' Lance rows < floor $floor (or pylance error)" >&2
  return 1
}

# --- s6a: Item 2.03 stream-emission wiring (no-op grep gate) ------------- #
run_surface "s6a" '
  parser_file="$REPO_ROOT/apps/data-engine-x/scripts/_lib/sec_edgar_form_8k_parser.py"
  ingest_file="$REPO_ROOT/apps/data-engine-x/scripts/run_sec_edgar_form_8k_r2_ingest.py"
  test -f "$parser_file" || { echo "missing $parser_file"; exit 1; }
  test -f "$ingest_file" || { echo "missing $ingest_file"; exit 1; }
  grep -q "ITEM_2_03 = \"2.03\"" "$parser_file" || { echo "ITEM_2_03 constant missing"; exit 1; }
  grep -A2 "BODY_PARSE_ITEMS:" "$parser_file" | grep -q "ITEM_2_03" || { echo "ITEM_2_03 not in BODY_PARSE_ITEMS"; exit 1; }
  grep -q "_parse_item_2_03" "$parser_file" || { echo "_parse_item_2_03 function missing"; exit 1; }
  grep -q "\"item_2_03_direct_financial_obligation\":" "$parser_file" || { echo "item_2_03_direct_financial_obligation stream key missing in parser"; exit 1; }
  grep -A6 "PER_YEAR_STREAMS:" "$ingest_file" | grep -q "item_2_03_direct_financial_obligation" || { echo "item_2_03_direct_financial_obligation missing from PER_YEAR_STREAMS"; exit 1; }
  echo "  all 5 grep checks pass: Item 2.03 wiring is correct end-to-end"
'

# --- s6b: Legacy smoke file cleanup (OPTIONAL — only fires with --include-s6b) -- #
# Validator-recommended SKIP. Smoke files at year=YYYY/<item>/data.parquet are
# at the CORRECT path for per-year-flushed streams (PER_YEAR_STREAMS); they are
# NOT pollution.
run_surface "s6b" '
  # Verify that the three legacy 2026-05-09 smoke files at the top-level
  # year=2024/<item-slug>/ paths are absent (operator has explicitly cleaned).
  for slug in item_1_01_material_agreement item_2_01_acquisition_disposition item_5_02_officer_changes; do
    out=$(doppler run --project hq-all --config prd -- bash -c "
      AWS_ACCESS_KEY_ID=\"\$R2_ACCESS_KEY_ID\" \
      AWS_SECRET_ACCESS_KEY=\"\$R2_SECRET_ACCESS_KEY\" \
      aws s3 ls --endpoint-url \"\$R2_ENDPOINT\" s3://dex-raw-landing-zone/sec-edgar/form-8k/year=2024/$slug/data.parquet 2>&1
    ")
    if echo "$out" | grep -q "data.parquet"; then
      echo "FAIL: legacy smoke file still present: year=2024/$slug/data.parquet"
      exit 1
    fi
  done
  echo "  all 3 legacy smoke files absent (or already cleaned)"
'

# --- s6: 8-K backfill — Item 2.03 R2 >= 5,000 AND aggregated >= 30,000 --- #
# Two-part. Item 2.03 glob targets the canonical slug emitted by the parser
# (`item_2_03_direct_financial_obligation`). Aggregated glob recursively matches
# both layout shapes (year=Y/<item>/ and year=Y/quarter=Q/<item>/).
# union_by_name handles minor per-stream schema width differences.
run_surface "s6" '
  r2_min_row_floor_check "s3://dex-raw-landing-zone/sec-edgar/form-8k/**/item_2_03*/data.parquet" 5000 || { echo "  s6 Item 2.03 floor not met"; exit 1; }
  r2_min_row_floor_check "s3://dex-raw-landing-zone/sec-edgar/form-8k/**/item_*/data.parquet" 30000 || { echo "  s6 aggregated all-items floor not met"; exit 1; }
  # Confirm Modal app stays enabled (cron forward-state).
  if ! doppler run --project hq-all --config prd -- modal app list --json 2>/dev/null | \
    python3 -c "import json,sys; data=json.load(sys.stdin); apps=[r for r in data if r.get(\"Description\") == \"data-engine-x-sec-edgar-form-8k\" and r.get(\"State\") == \"deployed\"]; sys.exit(0 if apps else 1)"; then
    echo "  Modal app not in deployed state"
    exit 1
  fi
  echo "  Modal app deployed-state confirmed"
'

# --- s7: 8-K Lance dataset rowcount >= 30,000 ---------------------------- #
run_surface "s7" '
  lance_min_row_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_8k_lance/" 30000
'

echo "All requested surfaces verified."
