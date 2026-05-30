#!/usr/bin/env bash
# Verification harness for migration abs-15g-ingest.
#
# Runs each surface's verify command from directive
#   ~/Desktop/hq/directives/2026-05-12-abs-15g-ingest.md
# Exits 0 only if every requested surface PASSes.
#
# Usage:
#   abs-15g-ingest.sh                  # all surfaces
#   abs-15g-ingest.sh --repo hq-all    # filter (only hq-all in this directive)
#
# Sources _lib-shim.sh once at top — never re-encode the Doppler bash -c wrapper
# or the DDL/POOLED URL choice inline.

set -euo pipefail

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (filter: ${REPO_FILTER:-all})"

run_surface() {
  local id="$1" repo="$2" cmd="$3"
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

# Repo root for python imports / file-existence checks.
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")

# s1 — migration: ops.sec_edgar_form_abs_15g_r2_ingest_runs table exists
run_surface "s1" "hq-all" 'dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''sec_edgar_form_abs_15g_r2_ingest_runs'\''" | grep -q 1'

# s2 — parser module imports. Use the same import shape the ingest script uses
# (sys.path includes scripts/, so the parser does `from _lib.sec_edgar_form_abs_15g_normalize import ...`).
# Requires lxml in the active interpreter — uses uv run within apps/data-engine-x to pick up pyproject deps.
run_surface "s2" "hq-all" 'test -f '"$REPO_ROOT"'/apps/data-engine-x/scripts/_lib/sec_edgar_form_abs_15g_parser.py && (cd '"$REPO_ROOT"'/apps/data-engine-x && uv run --with lxml --with beautifulsoup4 python -c "import sys; sys.path.insert(0, \"scripts\"); from _lib.sec_edgar_form_abs_15g_parser import parse_filing")'

# s3 — normalize module imports + basic shape (no external deps; bare python3 fine)
run_surface "s3" "hq-all" 'python3 -c "import sys; sys.path.insert(0, \"'"$REPO_ROOT"'/apps/data-engine-x/scripts/_lib\"); import sec_edgar_form_abs_15g_normalize as N; assert N.normalize_cik(\"320193\") == \"0000320193\"; assert N.normalize_asset_class(\"Residential Mortgage\") == \"residential_mortgage\""'

# s4 — ingest script CLI smoke. Requires pyarrow + boto3 + httpx + psycopg + lxml + bs4 (the script's full import surface).
# Via uv run with the canonical apps/data-engine-x pyproject deps.
run_surface "s4" "hq-all" 'test -f '"$REPO_ROOT"'/apps/data-engine-x/scripts/run_sec_edgar_form_abs_15g_r2_ingest.py && (cd '"$REPO_ROOT"'/apps/data-engine-x && uv run --with pyarrow --with boto3 --with httpx --with "psycopg[binary]" --with lxml --with beautifulsoup4 python scripts/run_sec_edgar_form_abs_15g_r2_ingest.py --help) >/dev/null'

# s5 — Lance emitter file exists + CLI smoke (--help has no deps beyond argparse)
run_surface "s5" "hq-all" 'test -f '"$REPO_ROOT"'/apps/data-engine-x/scripts/emit_sec_edgar_form_abs_15g_lance.py && python3 '"$REPO_ROOT"'/apps/data-engine-x/scripts/emit_sec_edgar_form_abs_15g_lance.py --help >/dev/null'

# s6 — ops.data_sources has 2 active rows for the new source
run_surface "s6" "hq-all" 'dex_psql_query "SELECT COUNT(*)::int FROM ops.data_sources WHERE display_name IN ('\''sec_edgar_form_abs_15g'\'','\''sec_edgar_form_abs_15g_lance'\'') AND status='\''active'\''" | grep -q "^2$"'

# s7a — Modal app file exists
run_surface "s7a" "hq-all" 'test -f '"$REPO_ROOT"'/apps/data-engine-x/modal/sec_edgar_form_abs_15g_app.py'

# s7b — Modal app deployed (queried by name via `modal app history`)
# `modal app list` truncates description column; `modal app history <name>` queries by name directly.
# Uses command substitution (not piped grep) to avoid pipefail SIGPIPE-141 false negatives under set -euo pipefail.
run_surface "s7b" "hq-all" 'out=$(doppler run --project hq-all --config prd -- bash -c "modal app history data-engine-x-sec-edgar-form-abs-15g 2>&1"); echo "$out" | grep -qi "deployed\|v[0-9]"'

# s7c — audit ledger has ≥1 row post-smoke-backfill (or after first cron tick)
run_surface "s7c" "hq-all" 'dex_psql_query "SELECT 1 FROM ops.sec_edgar_form_abs_15g_r2_ingest_runs ORDER BY started_at DESC LIMIT 1" | grep -q 1'

# s7d — R2 has ≥1 parquet under sec-edgar/form-abs-15g/ (smoke-pass; full floor 10K is deploy-verify state)
# Uses aws s3 ls (recursive) + command substitution. R2 secrets are exposed as R2_ACCESS_KEY_ID etc.;
# aws CLI expects AWS_ACCESS_KEY_ID etc., so we alias them inline.
run_surface "s7d" "hq-all" 'out=$(doppler run --project hq-all --config prd -- bash -c "AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://dex-raw-landing-zone/sec-edgar/form-abs-15g/ --recursive --endpoint-url \$R2_ENDPOINT 2>/dev/null" || true); echo "$out" | grep -q "\.parquet"'

# s7-runtime-probe — data-engine-x default probe (per CLAUDE.md §"Deploy verification")
run_surface "s7-probe" "hq-all" 'source '"$REPO_ROOT"'/apps/data-engine-x/scripts/_lib/deploy_verify.sh && verify_service_runtime data-engine-x "https://api.dataengine.run"'

echo "All requested surfaces verified."
