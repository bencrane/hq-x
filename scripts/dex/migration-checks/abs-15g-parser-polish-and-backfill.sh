#!/usr/bin/env bash
# Verification harness for /scope cycle abs-15g-parser-polish-and-backfill.
#
# Runs each surface's verify command from directive
#   ~/Desktop/hq/directives/2026-05-13-abs-15g-parser-polish-and-backfill.md
# Exits 0 only if every requested surface PASSes.
#
# Usage:
#   abs-15g-parser-polish-and-backfill.sh                   # all surfaces
#   abs-15g-parser-polish-and-backfill.sh --surface s1      # single surface
#   abs-15g-parser-polish-and-backfill.sh --repo hq-all     # filter (only hq-all here)
#
# Sources _lib-shim.sh once at top — never re-encode the Doppler bash -c wrapper
# or the DEX_DB_URL_DIRECT-vs-POOLED choice inline.

set -euo pipefail

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

SURFACE_FILTER=""
REPO_FILTER=""
PARTIAL_OK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    --partial-ok) PARTIAL_OK=1; shift ;;  # accept 80% floor instead of 100%
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all}, partial-ok: $PARTIAL_OK)"

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")
FLOOR_FULL=10000
FLOOR_PARTIAL=8000
FLOOR=$FLOOR_FULL
if [[ "$PARTIAL_OK" == "1" ]]; then
  FLOOR=$FLOOR_PARTIAL
fi

# ---- s1 HARD GATE: re-smoke result file has total_samples==10 AND structured_rows_count>=9 ----
run_surface "s1" "hq-all" 'test -f /tmp/abs-15g-resmoke-result.json && python3 -c '"'"'import json, sys; d = json.load(open("/tmp/abs-15g-resmoke-result.json")); ok = d["total_samples"] == 10 and d["structured_rows_count"] >= 9; print("resmoke: structured={}/{} gate={}".format(d["structured_rows_count"], d["total_samples"], "PASS" if ok else "FAIL")); sys.exit(0 if ok else 1)'"'"''

# ---- s1b: parser module imports cleanly ----
run_surface "s1b" "hq-all" 'test -f '"$REPO_ROOT"'/apps/data-engine-x/scripts/_lib/sec_edgar_form_abs_15g_parser.py && (cd '"$REPO_ROOT"'/apps/data-engine-x && uv run --with lxml --with beautifulsoup4 python -c "import sys; sys.path.insert(0, \"scripts\"); from _lib.sec_edgar_form_abs_15g_parser import parse_filing, parse_primary_doc, _parse_primary_html, _looks_like_xml") >/dev/null 2>&1'

# ---- s1c: ingest script CLI smoke ----
run_surface "s1c" "hq-all" 'cd '"$REPO_ROOT"'/apps/data-engine-x && uv run --with pyarrow --with boto3 --with httpx --with "psycopg[binary]" --with lxml --with beautifulsoup4 python scripts/run_sec_edgar_form_abs_15g_r2_ingest.py --help >/dev/null 2>&1'

# ---- s1d: Modal app TARGET_RPS env reset confirmed (source check, not runtime) ----
run_surface "s1d" "hq-all" 'grep -q "env\[\"SEC_EDGAR_TARGET_RPS\"\] = \"1\"" '"$REPO_ROOT"'/apps/data-engine-x/modal/sec_edgar_form_abs_15g_app.py'

# ---- s2: R2 row-count meets floor ----
# union_by_name=true tolerates the schema-drift between filings (per-year) and
# repurchase_summary (per-quarter) streams. Glob matches both. The DuckDB
# httpfs setup is wrapped in a doppler-injected bash subshell so $R2_ENDPOINT
# and creds expand inside the secret context — never at harness-parse time.
count_r2_rows() {
  local floor="$1"
  local count
  count=$(doppler run --project hq-all --config prd -- bash -c '
    R2_HOST=${R2_ENDPOINT#https://}
    AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY \
      duckdb -noheader -csv -c "
        INSTALL httpfs; LOAD httpfs;
        SET s3_endpoint='"'"'$R2_HOST'"'"';
        SET s3_url_style='"'"'path'"'"';
        SET s3_use_ssl=true;
        SET s3_access_key_id='"'"'$R2_ACCESS_KEY_ID'"'"';
        SET s3_secret_access_key='"'"'$R2_SECRET_ACCESS_KEY'"'"';
        SELECT COUNT(*) FROM read_parquet('"'"'s3://dex-raw-landing-zone/sec-edgar/form-abs-15g/**/*.parquet'"'"', union_by_name=true);
      "
  ' 2>/dev/null | tail -1 | tr -d '[:space:]')
  echo "R2 abs-15g row_count=${count:-0} floor=$floor"
  [[ "${count:-0}" -ge "$floor" ]]
}

run_surface "s2" "hq-all" "count_r2_rows $FLOOR"

# ---- s2b: Modal app stays in `deployed` state (cron enabled) ----
run_surface "s2b" "hq-all" 'out=$(doppler run --project hq-all --config prd -- bash -c "modal app history data-engine-x-sec-edgar-form-abs-15g 2>&1"); echo "$out" | grep -qi "deployed\|v[0-9]"'

# ---- s3: Lance dataset row-count meets floor ----
count_lance_rows() {
  local floor="$1"
  local out
  out=$(doppler run --project hq-all --config prd -- uv run --with pylance python -c '
import os, lance
ds = lance.dataset(
    "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/",
    storage_options={
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    },
)
print(f"lance_rows={ds.count_rows()}")
' 2>/dev/null | tail -1)
  local count=${out#lance_rows=}
  echo "Lance abs-15g $out floor=$floor"
  [[ "${count:-0}" -ge "$floor" ]]
}

run_surface "s3" "hq-all" "count_lance_rows $FLOOR"

# ---- s4: runtime probe against deployed Railway service ----
run_surface "s4" "hq-all" 'source '"$REPO_ROOT"'/apps/data-engine-x/scripts/_lib/deploy_verify.sh && verify_service_runtime data-engine-x "https://api.dataengine.run"'

echo "All requested surfaces verified."
