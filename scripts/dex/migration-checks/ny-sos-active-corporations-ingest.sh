#!/usr/bin/env bash
# Verification harness for cycle `ny-sos-active-corporations-ingest` (2026-05-19).
#
# Authored 2026-05-19 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-19-ny-sos-active-corporations-ingest.md
#
# Pattern: mirrors `ca-opsc-school-facility-funding-ingest.sh` (PR #554, weekly CKAN
# precedent — cleanest single-feed cadence structure). Differs in:
#   - DuckDB-based c1 (4.2M rows: pandas would OOM Modal's 8GB worker)
#   - sos namespace (matches sos.ca_*_lance and sos.fl_*_lance precedents)
#   - Daily cron at 14:00 UTC (instead of weekly Monday)
#
# Surfaces (7 total, phase order):
#   Phase 1 — Migrations:    m1 (catalog row), m2 (ledger + status view ext)
#   Phase 2 — Code:          c1 (R2 ingest), c2 (Modal app), c4 (Lance emit), c6 (LanceViews)
#   Phase 3 — R2 backfill:   r1 (snapshot partition)               [SOFT — strict post-deploy]
#   Phase 4 — Lance emit:    e1 (Lance row floor + BTREE)          [SOFT — strict post-deploy]
#   Phase 5 — Modal deploy:  s1
#
# Usage:
#   ./ny-sos-active-corporations-ingest.sh                  # all surfaces (soft for r1/e1)
#   ./ny-sos-active-corporations-ingest.sh --surface m1     # one surface
#   STRICT=1 ./ny-sos-active-corporations-ingest.sh         # post-deploy: missing r1/e1 FAIL
#
# Worktree invocation (per L61):
#   HQ_ALL_ROOT=/Users/benjamincrane/hq-all/.claude/worktrees/competent-rubin-fcce42 \
#     STRICT=1 ./ny-sos-active-corporations-ingest.sh

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

echo "==> Verifying ny-sos-active-corporations-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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

# Soft-skip helper: SKIPs (not FAILs) in default mode; FAILs under STRICT=1.
# Use for surfaces whose artifacts only exist post-deploy (r1, e1).
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
R2_PREFIX="sos-ny/active-corporations"
LANCE_URI="s3://${R2_BUCKET}/polaris-warehouse/sos/ny_active_corporations_lance"
# Floor: 4,212,163 rows × 0.95 = 4,001,554 (validator baseline 2026-05-19).
FLOOR_ROWS=4001554
MIN_R2_OBJECT_FLOOR=1   # one snapshot partition with one .parquet
MODAL_APP_NAME="data-engine-x-ny-sos-active-corporations"

# --- Lance row-count + BTREE check (writes a temp .py file) ------- #
# Usage: _lance_check_inline <lance_uri> <floor> <required_btree_col1> [<col2>]
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
    idx_cols.extend(i.get('fields', i.get('columns', [])))
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

# ── m1: data_source_catalog INSERT ───────────────────────────────────── #
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''ny_sos_active_corporations'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]]
'

# ── m2: ops.ny_sos_active_corporations_ingest_runs table + index + view ext ── #
run_surface "m2" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''ny_sos_active_corporations_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_ny_sos_active_corporations_ingest_runs_status_started'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]] &&
  VIEW_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''ny_sos_active_corporations'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_COUNT" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_ny_sos_active_corporations_to_r2.py ──────────────── #
# Asserts: file exists, parses, contains required invariants:
#   - source host data.ny.gov
#   - R2 prefix sos-ny/active-corporations
#   - snapshot=YYYY-MM-DD partition
#   - ZSTD compression
#   - DuckDB-based all-VARCHAR read (this script uses DuckDB read_csv with all_varchar=TRUE
#     given 4.2M-row scale; pandas dtype=str would OOM Modal 8GB worker)
#   - ContentType application/x-parquet
#   - Writes to ops.ny_sos_active_corporations_ingest_runs
#   - X-SODA2-Truth-Last-Modified idempotency header (per validator p3)
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_ny_sos_active_corporations_to_r2.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data.ny.gov"                                  "$F" &&
  grep -q "sos-ny/active-corporations"                   "$F" &&
  grep -q "snapshot="                                    "$F" &&
  grep -qE "ZSTD|zstd"                                   "$F" &&
  grep -qE "all_varchar|dtype=str|astype.*str"           "$F" &&
  grep -q "application/x-parquet"                        "$F" &&
  grep -q "ops.ny_sos_active_corporations_ingest_runs"   "$F" &&
  grep -q "X-SODA2-Truth-Last-Modified"                  "$F"
'

# ── c2: modal/ny_sos_active_corporations_app.py ──────────────────────── #
# Asserts: file exists, parses, contains:
#   - Modal app name data-engine-x-ny-sos-active-corporations
#   - Cron schedule (daily, 14:00 UTC)
#   - Both secrets (dex-db + bulk-ingest-r2)
#   - 60-min timeout (4.2M-row scale per validator p2)
#   - 8 GB memory (8192 MiB)
#   - Delegates to c1 ingest + c4 emit modules
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/ny_sos_active_corporations_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-ny-sos-active-corporations"     "$F" &&
  grep -qE "Cron\("                                      "$F" &&
  grep -q "dex-db"                              "$F" &&
  grep -q "bulk-ingest-r2"                               "$F" &&
  grep -qE "timeout=60\*60|timeout=3600"                 "$F" &&
  grep -qE "memory=8192"                                 "$F" &&
  grep -q "run_ny_sos_active_corporations_to_r2"         "$F" &&
  grep -q "run_ny_sos_active_corporations_lance_emit"    "$F"
'

# ── c4: scripts/run_ny_sos_active_corporations_lance_emit.py ─────────── #
# Asserts: file exists, parses, contains:
#   - Lance dataset name ny_active_corporations_lance
#   - sos namespace
#   - lance_commit_lock wrapper
#   - BTREE / create_scalar_index on indexed columns
#   - Must NOT contain duckdb.typing. literal (per L59 — including comments)
run_surface "c4" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_ny_sos_active_corporations_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "ny_active_corporations_lance"        "$F" &&
  grep -qE "\"sos\"|'\''sos'\''"                 "$F" &&
  grep -q "lance_commit_lock"                   "$F" &&
  grep -qE "BTREE|create_scalar_index"          "$F" &&
  ! grep -E "^[^#]*duckdb\\.typing\\." "$F"
'

# ── c6: app/services/lance_views.py — LanceView entry appended ──────── #
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "ny_active_corporations_lance" "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first run)
# ====================================================================== #

# ── r1: NY SoS R2 partition has ≥1 .parquet ─────────────────────────── #
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

# ── e1: Lance dataset — floor 4,001,554, BTREE on dos_id ──────────── #
_E1_PY=$(_lance_check_inline "$LANCE_URI" "$FLOOR_ROWS" "dos_id" "")
_LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 5 — Modal deploy
# ====================================================================== #

# ── s1: Modal app deployed ─────────────────────────────────────────── #
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
