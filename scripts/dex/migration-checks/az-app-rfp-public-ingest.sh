#!/usr/bin/env bash
# Verification harness for cycle `az-app-rfp-public-ingest` (2026-05-19).
#
# Pattern: mirrors `ca-opsc-school-facility-funding-ingest.sh` (PR #554).
#
# Surfaces (7 total, phase order):
#   Phase 1 — Migrations:    m1 (catalog row), m2 (ledger + status view ext)
#   Phase 2 — Code:          c1 (R2 scraper), c2 (Modal app), c4 (Lance emit), c6 (LanceViews)
#   Phase 3 — R2 backfill:   r1 (snapshot partition)               [SOFT — strict post-deploy]
#   Phase 4 — Lance emit:    e1 (Lance row floor + BTREE)          [SOFT — strict post-deploy]
#   Phase 5 — Modal deploy:  s1
#
# Usage:
#   ./az-app-rfp-public-ingest.sh                  # all surfaces (soft for r1/e1)
#   ./az-app-rfp-public-ingest.sh --surface m1     # one surface
#   STRICT=1 ./az-app-rfp-public-ingest.sh         # post-deploy: missing r1/e1 FAIL

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
else
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

APP_DIR="$HQ_ALL_ROOT/apps/data-engine-x"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

# --- CLI parsing --------------------------------------------------------- #
REPO_FILTER=""
SURFACE_FILTER=""
STRICT="${STRICT:-0}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --strict)  STRICT=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying az-app-rfp-public-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

run_surface_soft() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ "$STRICT" -eq 1 ]]; then
    run_surface "$id" "$repo" "$cmd"
    return 0
  fi
  echo "-- $id ($repo): RUNNING (soft)"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id ($repo): SKIP (artifact not yet present; STRICT=1 to FAIL)"
    SKIP_COUNT=$((SKIP_COUNT+1))
  fi
}

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="azstate/app-rfp-public"
LANCE_URI="s3://${R2_BUCKET}/polaris-warehouse/azstate/app_rfp_public_lance"
FLOOR_ROWS=100         # 150 actual per 2026-05-19 probe; floor at 100 leaves ~33% slack
MIN_R2_OBJECT_FLOOR=1  # one snapshot partition with one .parquet
MODAL_APP_NAME="data-engine-x-az-app-rfp-public"

# --- Lance row-count + BTREE check (writes a temp .py file) ------- #
_lance_check_inline() {
  local uri="$1" floor="$2" col1="$3" col2="${4:-}"
  local extra_check=""
  if [[ -n "$col2" ]]; then
    extra_check="assert '$col2' in idx_cols, f'BTREE on $col2 missing: {idx_cols}'"
  fi
  local tmpfile
  tmpfile=$(mktemp -t lance_check.XXXXXX.py)
  cat >"$tmpfile" <<PYEOF
import lance, os
ds = lance.dataset('$uri', storage_options={
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
})
rows = ds.count_rows()
assert rows >= $floor, f'floor breach: {rows} < $floor'
idx_cols = []
for i in ds.list_indices():
    idx_cols.extend(i.get('fields', []))
assert '$col1' in idx_cols, f'BTREE on $col1 missing: {idx_cols}'
$extra_check
print(f'rows={rows} idx={idx_cols}')
PYEOF
  echo "$tmpfile"
}

_LANCE_CHECK_TMPFILES=()
_cleanup_lance_tmpfiles() {
  for f in "${_LANCE_CHECK_TMPFILES[@]}"; do
    [[ -f "$f" ]] && rm -f "$f"
  done
}
trap _cleanup_lance_tmpfiles EXIT

# ====================================================================== #
# Phase 1 — Migrations
# ====================================================================== #

run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''az_app_rfp_public'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]]
'

run_surface "m2" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''az_app_rfp_public_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_az_app_rfp_public_ingest_runs_status_started'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]] &&
  VIEW_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''az_app_rfp_public'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_COUNT" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_az_app_rfp_public_to_r2.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "app.az.gov"                          "$F" &&
  grep -q "azstate/app-rfp-public"              "$F" &&
  grep -q "snapshot="                           "$F" &&
  grep -qE "ZSTD|zstd"                          "$F" &&
  grep -qE "dtype=str|all_varchar|astype.*str"  "$F" &&
  grep -q "application/x-parquet"               "$F" &&
  grep -q "ops.az_app_rfp_public_ingest_runs"   "$F"
'

run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/az_app_rfp_public_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-az-app-rfp-public"  "$F" &&
  grep -qE "Cron\("                          "$F" &&
  grep -q "dex-db"                  "$F" &&
  grep -q "bulk-ingest-r2"                   "$F" &&
  grep -qE "run_az_app_rfp_public_to_r2|daily_az_app_rfp_refresh" "$F"
'

run_surface "c4" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_az_app_rfp_public_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "app_rfp_public_lance"  "$F" &&
  grep -q "azstate"               "$F" &&
  grep -q "lance_commit_lock"     "$F" &&
  grep -qE "BTREE|create_scalar_index" "$F" &&
  ! grep -E "^[^#]*duckdb\\.typing\\." "$F"
'

run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "az_app_rfp_public_lance" "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first run)
# ====================================================================== #

run_surface_soft "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c \"\\.parquet\$\" | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
    echo \"r1 ok: objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emit (SOFT until first emit run)
# ====================================================================== #

_E1_PY=$(_lance_check_inline "$LANCE_URI" "$FLOOR_ROWS" "code" "")
_LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 5 — Modal deploy
# ====================================================================== #

run_surface "s1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app list --json | jq -e \".[] | select(.Description==\\\"'"$MODAL_APP_NAME"'\\\") | select(.State==\\\"deployed\\\")\" >/dev/null
  "
'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
