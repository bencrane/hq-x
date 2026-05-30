#!/usr/bin/env bash
# Verification harness for cycle `ca-cal-eprocure-archived-ingest` (2026-05-19).
#
# Authored 2026-05-19 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-19-ca-cal-eprocure-archived-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/ca-cal-eprocure-archived-ingest.sh
#
# Pattern: mirrors `2026-05-18-sec-dera-form-d-ingest.sh`.
#
# Surfaces (7 total, phase order):
#   Phase 1 — Migrations:    m1 (catalog 2 rows + status view ext), m2 (ledger)
#   Phase 2 — Code:          c1 (R2 ingest), c3 (Modal app), c5 (Lance emit), c6 (LanceViews)
#   Phase 3 — R2 backfill:   r1 (PO snapshot), r2 (NCB snapshot)  [SOFT — may skip if not yet run]
#   Phase 4 — Lance emits:   e1 (PO Lance), e2 (NCB Lance)        [SOFT — may skip if not yet emitted]
#   Phase 5 — Modal deploy:  s1
#
# r1/r2/e1/e2 use SOFT semantics: they SKIP (not FAIL) when the artifact is absent.
# The post-merge deploy-verifier flips them to STRICT (passing $STRICT=1).
#
# Usage:
#   ./ca-cal-eprocure-archived-ingest.sh                       # all surfaces (soft mode for r*/e*)
#   ./ca-cal-eprocure-archived-ingest.sh --surface m1          # one surface
#   ./ca-cal-eprocure-archived-ingest.sh --repo bencrane/hq-all # repo filter (trivial; single-repo)
#   STRICT=1 ./ca-cal-eprocure-archived-ingest.sh              # post-deploy: missing r*/e* artifacts FAIL
#
# Doppler idiom (per apps/data-engine-x/CLAUDE.md §"Doppler shell gotcha"):
#   doppler run --project hq-all --config prd -- bash -c '...'

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

echo "==> Verifying ca-cal-eprocure-archived-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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
# Use for surfaces whose artifacts only exist post-deploy (r1, r2, e1, e2).
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
R2_PO_PREFIX="cal-eprocure-archived/po"
R2_NCB_PREFIX="cal-eprocure-archived/ncb"
LANCE_PO_URI="s3://${R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_po_lance"
LANCE_NCB_URI="s3://${R2_BUCKET}/polaris-warehouse/castate/cal_eprocure_archived_ncb_lance"

# Lance volume floors per directive §"Volume floors", validator-refined from
# CKAN datastore_search?limit=0 probes:
#   PO  total = 344,504 (FY 2012-2015 historical, static since 2019)
#   NCB total =     469 (monthly Special Category NCB ≥$1M)
# Floor at 300K (PO) is conservative — leaves slack for upstream row drift.
# Floor at 300 (NCB) is conservative — leaves slack for monthly contraction.
FLOOR_PO=300000
FLOOR_NCB=300

MIN_R2_OBJECT_FLOOR_PO=1   # one snapshot partition with one .parquet
MIN_R2_OBJECT_FLOOR_NCB=1

MODAL_APP_NAME="data-engine-x-cal-eprocure-archived"

# --- Python lance row-count + BTREE check (writes a temp .py file) ------- #
# Usage: _lance_check_inline <lance_uri> <floor> <required_btree_col1> [<col2>]
#
# Returns: path to a temp .py file that asserts rows>=floor + BTREE on col1
# (and optionally col2). Invoke via `doppler run -- python3 <returned-path>`.
#
# Why a temp file: inlining Python source through nested bash -c '...' triggers
# single-quote stripping by the outer shell, mangling string literals (rendering
# `ds.dataset('s3://...')` as `ds.dataset(s3://...)`). The temp-file pattern
# sidesteps the quoting nightmare entirely. (Precedent: sec-dera-form-d harness.)
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
    # lance.list_indices() returns dicts with key 'fields' (not 'columns')
    idx_cols.extend(i.get('fields', i.get('columns', [])))
assert '$col1' in idx_cols, f'BTREE on $col1 missing: {idx_cols}'
$extra_check
print(f'rows={rows} idx={idx_cols}')
PYEOF
  echo "$tmpfile"
}

# Track temp lance-check files for cleanup at exit.
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

# ── m1: data_source_catalog INSERT (2 rows) + status view ext ─────────── #
# Asserts both catalog rows exist + active + appear in the catalog status view.
# Per precedent abs-15g-ingest.sh:11/47, sql literals use '\''...'\'' to survive
# the bash → eval → _dex_doppler → bash → psql expansion chain.
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug IN ('\''cal_eprocure_archived_po'\'','\''cal_eprocure_archived_ncb'\'') AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "2" ]] &&
  VIEW_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug IN ('\''cal_eprocure_archived_po'\'','\''cal_eprocure_archived_ncb'\'')" | tr -d "[:space:]") &&
  [[ "$VIEW_COUNT" = "2" ]]
'

# ── m2: ops.cal_eprocure_ingest_runs table + index ────────────────────── #
# Asserts table exists + the (source_variant, status, started_at) index landed.
run_surface "m2" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''cal_eprocure_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_cal_eprocure_ingest_runs_variant_status_started'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_cal_eprocure_archived_to_r2.py ──────────────────────── #
# Asserts: file exists, parses, contains required invariants:
#   - CKAN resource ids for PO + NCB
#   - R2 prefix cal-eprocure-archived/po + .../ncb
#   - ZSTD parquet compression
#   - all-VARCHAR DuckDB read (or pandas dtype=str — script's choice; we grep generically)
#   - Audit-table write to ops.cal_eprocure_ingest_runs
#   - ContentType: application/x-parquet
#   - Idempotent on snapshot partition (snapshot= in path)
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_cal_eprocure_archived_to_r2.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "bb82edc5-9c78-44e2-8947-68ece26197c5" "$F" &&
  grep -q "14932789-485b-481b-910a-dafb40d3471c" "$F" &&
  grep -q "cal-eprocure-archived/po"             "$F" &&
  grep -q "cal-eprocure-archived/ncb"            "$F" &&
  grep -q "snapshot="                            "$F" &&
  grep -qE "ZSTD|zstd"                           "$F" &&
  grep -qE "dtype=str|all_varchar"               "$F" &&
  grep -q "application/x-parquet"                "$F" &&
  grep -q "ops.cal_eprocure_ingest_runs"         "$F"
'

# ── c3: modal/cal_eprocure_archived_app.py ────────────────────────────── #
# Asserts: file exists, parses, contains:
#   - Modal app name data-engine-x-cal-eprocure-archived
#   - Cron schedule for NCB (monthly — 5th-of-month per SAM precedent)
#   - Two scheduled functions OR one with variant dispatch
#   - secrets dex-db + bulk-ingest-r2
#   - delegates to scripts/run_cal_eprocure_archived_to_r2.py
run_surface "c3" "bencrane/hq-all" '
  F="$APP_DIR/modal/cal_eprocure_archived_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-cal-eprocure-archived"  "$F" &&
  grep -qE "Cron\("                              "$F" &&
  grep -q "dex-db"                      "$F" &&
  grep -q "bulk-ingest-r2"                       "$F" &&
  grep -qE "run_cal_eprocure_archived_to_r2|ingest_po|ingest_ncb" "$F"
'

# ── c5: scripts/run_cal_eprocure_archived_lance_emit.py ───────────────── #
# Asserts: file exists, parses, registers BOTH Lance datasets, wraps lance.write_dataset
# in lance_commit_lock, calls init_polaris_lance_generic (or imports its module),
# DuckDB UDF registration uses STRING type names per HEAD 6b77d687 (no duckdb.typing.*).
run_surface "c5" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_cal_eprocure_archived_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "cal_eprocure_archived_po_lance"    "$F" &&
  grep -q "cal_eprocure_archived_ncb_lance"   "$F" &&
  grep -q "castate"                            "$F" &&
  grep -q "lance_commit_lock"                  "$F" &&
  grep -qE "BTREE|create_scalar_index"         "$F" &&
  ! grep -E "duckdb\\.typing\\." "$F"
'

# ── c6: app/services/lance_views.py — 2 LanceView entries appended ────── #
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "cal_eprocure_archived_po_lance"   "$F" &&
  grep -q "cal_eprocure_archived_ncb_lance"  "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first run)
# ====================================================================== #
# R2 cred-mapping: AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
# are required for `aws s3 ls` to authenticate against R2 (per precedent
# scripts/migration-checks/abs-15g-ingest.sh:83 + ucc-ca-master-ingest.sh:75).

# ── r1: PO R2 partition has ≥1 .parquet ───────────────────────────────── #
run_surface_soft "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_PO_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c \"\\.parquet\$\" | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR_PO"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR_PO"'\"; exit 1; }
    echo \"r1 ok: po objects=\$OBJ_COUNT\"
  "
'

# ── r2: NCB R2 partition has ≥1 .parquet ──────────────────────────────── #
run_surface_soft "r2" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_NCB_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c \"\\.parquet\$\" | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR_NCB"' ]] || { echo \"r2 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR_NCB"'\"; exit 1; }
    echo \"r2 ok: ncb objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emits (SOFT until first emit run)
# ====================================================================== #
# Floor + BTREE check. Canonical BTREE column is audit-deferred to executor
# (after sample probe of the parquet schema). The grep below accepts ANY
# BTREE column name; the inline _lance_check_inline asserts presence-of-floor
# regardless. We pass an empty col1 — the check will then trivially assert
# rows>=floor only. (Executor MUST pin the col1 name once it inspects the
# source schema and produces the emit script.)
#
# Column names pinned post-executor probe:
#   PO:  purchase_order_number (from CSV header "Purchase Order Number")
#   NCB: ncb_number            (aliased from XLSX header "Number")

# ── e1: PO Lance — floor 300K, BTREE present (col name asserted in c5) ── #
# Column name pinned post-schema-probe: actual key column is purchase_order_number.
_E1_PY=$(_lance_check_inline "$LANCE_PO_URI" "$FLOOR_PO" "purchase_order_number" "")
_LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ── e2: NCB Lance — floor 300, BTREE present ──────────────────────────── #
# Column name pinned post-schema-probe: actual key column is ncb_number.
_E2_PY=$(_lance_check_inline "$LANCE_NCB_URI" "$FLOOR_NCB" "ncb_number" "")
_LANCE_CHECK_TMPFILES+=("$_E2_PY")
run_surface_soft "e2" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# ====================================================================== #
# Phase 5 — Modal deploy
# ====================================================================== #

# ── s1: Modal app data-engine-x-cal-eprocure-archived deployed ────────── #
# Note: `modal app list --json` uses "Description" (not "name") and "State" (capital S).
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
