#!/usr/bin/env bash
# Verification harness for the clinicaltrials-device-studies substrate.
#
# Original cycle: clinicaltrials-device-studies-ingest (2026-05-20, PR #584).
# Updated by:     clinicaltrials-device-studies-aact-refresh (2026-05-20) —
#   re-sourced the dataset from AACT (the CT.gov API WAF-blocks Modal egress).
#   c1 grep invariants now assert the AACT host + flat-file path; e1 floor
#   lowered to the verified AACT pull count.
#
# Per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-20-clinicaltrials-device-studies-aact-refresh-fix.md
#
# CANONICAL IN-REPO PATH:
#   apps/data-engine-x/scripts/migration-checks/clinicaltrials-device-studies.sh
#
# Pattern: mirrors `ca-cal-eprocure-archived-ingest.sh` (sub-A, PR #551).
#
# Surfaces (9 total, phase order):
#   Phase 1 — Migrations:   m1 (catalog row + status view), m2 (audit ledger)
#   Phase 2 — Code:         c1 (R2 ingest), c2 (Modal app), c4 (Lance emit), c6 (LanceViews)
#   Phase 3 — R2 backfill:  r1 (snapshot)                   [SOFT — may skip if not yet run]
#   Phase 4 — Lance emit:   e1 (Lance dataset)              [SOFT — may skip if not yet emitted]
#   Phase 5 — Modal deploy: s1
#
# r1/e1 use SOFT semantics: they SKIP (not FAIL) when the artifact is absent.
# STRICT=1 flips them to STRICT (post-deploy: missing r1/e1 FAIL).
#
# Usage:
#   ./clinicaltrials-device-studies.sh                         # all surfaces (soft mode for r*/e*)
#   ./clinicaltrials-device-studies.sh --surface m1            # one surface
#   ./clinicaltrials-device-studies.sh --repo bencrane/hq-all  # repo filter (trivial; single-repo)
#   STRICT=1 ./clinicaltrials-device-studies.sh                # post-deploy: missing r1/e1 artifacts FAIL
#
# Worktree note (L61): override HQ_ALL_ROOT to the worktree path when running
# from a worktree —
#   HQ_ALL_ROOT=/path/to/worktree STRICT=1 ./clinicaltrials-device-studies.sh
#
# Doppler idiom (per apps/data-engine-x/CLAUDE.md "Doppler shell gotcha"):
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

echo "==> Verifying clinicaltrials-device-studies (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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

# --- pinned constants per directive "Surface spec" ---------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_PREFIX="clinicaltrials-gov/device-studies"
LANCE_URI="s3://${R2_BUCKET}/polaris-warehouse/clinicaltrials/device_studies_lance"

# e1 row floor — set comfortably under the verified device-family pull. The
# device filter is the structured interventions table intervention_type in the
# device family — DEVICE + DIAGNOSTIC TEST + COMBINATION PRODUCT — the full
# medical-device universe (~95K studies from the 2026-05-20 AACT export). PR
# #591 filtered on DEVICE alone (73,521) and dropped ~18.9K Diagnostic Test +
# ~3.3K Combination Product studies; widened to the device family here.
FLOOR=90000

MIN_R2_OBJECT_FLOOR=1   # one snapshot partition with one .parquet

MODAL_APP_NAME="data-engine-x-clinicaltrials-device-studies"

# --- Python lance row-count + BTREE check (writes a temp .py file) ------- #
# Asserts rows>=floor + BTREE on the canonical key column.
# Why a temp file: inlining Python source through nested bash -c '...' triggers
# single-quote stripping by the outer shell, mangling string literals. The
# temp-file pattern sidesteps the quoting nightmare. (Precedent: sub-A harness.)
_lance_check_inline() {
  local uri="$1" floor="$2" col1="$3"
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

# ── m1: data_source_catalog row + status view ─────────────────────────── #
# Asserts the catalog row exists + is_active + resolves in the status view.
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''clinicaltrials_device_studies'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]] &&
  VIEW_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''clinicaltrials_device_studies'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_COUNT" = "1" ]]
'

# ── m2: ops.clinicaltrials_device_studies_ingest_runs table + index ───── #
run_surface "m2" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''clinicaltrials_device_studies_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_clinicaltrials_device_studies_ingest_runs_status_started'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_clinicaltrials_device_studies_to_r2.py ────────────── #
# Asserts: file exists, parses, contains required invariants:
#   - AACT host (the source switched off the CT.gov API — see AACT-refresh
#     directive; the CT.gov API WAF-blocks Modal egress)
#   - AACT exported_files flat-file path segment
#   - structured intervention_type device filter
#   - DuckDB pipe-delimited read (delim)
#   - R2 prefix clinicaltrials-gov/device-studies (UNCHANGED)
#   - snapshot= partition path (UNCHANGED)
#   - ZSTD parquet compression
#   - all-VARCHAR write (DuckDB all_varchar)
#   - ContentType: application/x-parquet
#   - audit-table write to ops.clinicaltrials_device_studies_ingest_runs
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_clinicaltrials_device_studies_to_r2.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "aact.ctti-clinicaltrials.org"                "$F" &&
  grep -q "exported_files"                              "$F" &&
  grep -q "intervention_type"                           "$F" &&
  grep -q "delim"                                       "$F" &&
  grep -q "clinicaltrials-gov/device-studies"           "$F" &&
  grep -q "snapshot="                                   "$F" &&
  grep -qE "ZSTD|zstd"                                  "$F" &&
  grep -qE "pa\.string\(\)|dtype=str|all_varchar"       "$F" &&
  grep -q "application/x-parquet"                       "$F" &&
  grep -q "ops.clinicaltrials_device_studies_ingest_runs" "$F"
'

# ── c2: modal/clinicaltrials_device_studies_app.py ────────────────────── #
# Asserts: file exists, parses, contains:
#   - Modal app name data-engine-x-clinicaltrials-device-studies
#   - Cron schedule
#   - secrets dex-db + bulk-ingest-r2
#   - delegates to c1 + c4
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/clinicaltrials_device_studies_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-clinicaltrials-device-studies"  "$F" &&
  grep -qE "Cron\("                                      "$F" &&
  grep -q "dex-db"                              "$F" &&
  grep -q "bulk-ingest-r2"                               "$F" &&
  grep -qE "run_clinicaltrials_device_studies_to_r2|run_clinicaltrials_device_studies_lance_emit" "$F"
'

# ── c4: scripts/run_clinicaltrials_device_studies_lance_emit.py ───────── #
# Asserts: file exists, parses, registers the Lance dataset, wraps
# lance.write_dataset in lance_commit_lock, BTREE present, and does NOT
# contain the duckdb.typing. literal in code (L34/L59 negation).
run_surface "c4" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_clinicaltrials_device_studies_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "device_studies_lance"               "$F" &&
  grep -q "clinicaltrials"                      "$F" &&
  grep -q "lance_commit_lock"                   "$F" &&
  grep -qE "BTREE|create_scalar_index"          "$F" &&
  ! grep -E "^[^#]*duckdb\\.typing\\." "$F"
'

# ── c6: app/services/lance_views.py — LanceView entry appended ────────── #
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "device_studies_lance"  "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first run)
# ====================================================================== #
# R2 cred-mapping: AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
# are required for `aws s3 ls` to authenticate against R2.

# ── r1: R2 snapshot partition has >=1 .parquet ────────────────────────── #
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

# ── e1: Lance dataset — floor ~90K, BTREE on nct_id ───────────────────── #
_E1_PY=$(_lance_check_inline "$LANCE_URI" "$FLOOR" "nct_id")
_LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 5 — Modal deploy
# ====================================================================== #

# ── s1: Modal app data-engine-x-clinicaltrials-device-studies deployed ── #
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
