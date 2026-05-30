#!/usr/bin/env bash
# Verification harness for /scope cycle `openfda-device-r2-lance-ingest`.
#
# Authored by the Stage 3.A migration auditor per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-20-openfda-device-r2-lance-ingest.md.
#
# openFDA Medical Device (510k + PMA + classification) → R2 ZSTD-Parquet →
# DuckDB → Lance + Polaris, weekly Modal cron. 7 surfaces, single PR.
# Pattern: mirrors `caltrans-ccop-active-ingest.sh` (PR #552) — the canonical
# 7-surface single-PR state-procurement ingest shape with a Modal app.
#
# Surface map (directive s1-s7 ↔ runbook m1/m2/c1/c2/c4/c6/deploy):
#   Phase 1 — Migrations:    s1 (catalog row), s2 (ingest_runs ledger)
#   Phase 2 — Code:          s3 (R2 ingest), s4 (Modal app), s5 (Lance emit), s6 (LanceViews)
#   Phase 3 — R2 backfill:   r1 (snapshot partition per variant)     [SOFT — strict post-deploy]
#   Phase 4 — Lance emit:    e1 (Lance row floor + BTREE per variant)[SOFT — strict post-deploy]
#   Phase 5 — Modal deploy:  s7 (app deployed-state)
#
# r1/e1/s7 pass only AFTER the s7 deploy populates R2 + emits Lance — that is
# correct: they are SOFT pre-deploy (skip), HARD post-deploy (STRICT=1).
#
# Single-quote surface bodies so $VAR / $(...) defer to the doppler-injected
# subshell. DEX checks via apps/data-engine-x/scripts/_lib/dex.sh (sourced
# through the migration-checks/_lib-shim.sh thin shim).
#
# Usage:
#   ./openfda-device-r2-lance-ingest.sh                  # all surfaces (soft for r1/e1/s7)
#   ./openfda-device-r2-lance-ingest.sh --surface s1     # one surface
#   ./openfda-device-r2-lance-ingest.sh --repo hq-all    # repo filter (single repo)
#   STRICT=1 ./openfda-device-r2-lance-ingest.sh         # post-deploy: missing r1/e1/s7 FAIL
#   HQ_ALL_ROOT=<worktree> STRICT=1 ./openfda-device-r2-lance-ingest.sh  # worktree run (L61)

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

echo "==> Verifying openfda-device-r2-lance-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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
# 3 variants; one R2 raw prefix per variant, one Lance dataset per variant.
R2_PREFIX_510K="openfda/device/510k"
R2_PREFIX_PMA="openfda/device/pma"
R2_PREFIX_CLASSIFICATION="openfda/device/classification"
LANCE_URI_510K="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_510k_lance"
LANCE_URI_PMA="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_pma_lance"
LANCE_URI_CLASSIFICATION="s3://${R2_BUCKET}/polaris-warehouse/openfda/device_classification_lance"
# Volume floors per directive `## Volume floors` (live manifest probe 2026-05-20).
FLOOR_510K=170000
FLOOR_PMA=55000
FLOOR_CLASSIFICATION=6900
MIN_R2_OBJECT_FLOOR=1   # one snapshot partition with one .parquet, per variant
MODAL_APP_NAME="data-engine-x-openfda-device"

# --- Lance row-count + BTREE check (writes a temp .py file) ------- #
# Canonical PKs / BTREE keys per directive `## Surfaces`:
#   device_510k_lance           → k_number
#   device_pma_lance            → pma_number  (composite key; BTREE on pma_number)
#   device_classification_lance → product_code
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

# ── s1: data_source_catalog INSERT (3 rows) + status view extension ──── #
# 3 catalog rows (one per variant), all lifecycle_stage='r2_only' (V1);
# status view selectable and surfaces all 3 slugs (V3 — full recreate).
run_surface "s1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug LIKE '\''openfda_device_%'\''" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "3" ]] &&
  R2ONLY_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug LIKE '\''openfda_device_%'\'' AND lifecycle_stage = '\''r2_only'\''" | tr -d "[:space:]") &&
  [[ "$R2ONLY_COUNT" = "3" ]] &&
  VIEW_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug LIKE '\''openfda_device_%'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_COUNT" = "3" ]]
'

# ── s2: ops.openfda_device_ingest_runs table + index + status CHECK ──── #
run_surface "s2" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''openfda_device_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_openfda_device_ingest_runs_variant_status_started'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]] &&
  STATUS_CHECK=$(dex_psql_query "SELECT count(*) FROM pg_constraint WHERE conrelid = '\''ops.openfda_device_ingest_runs'\''::regclass AND contype = '\''c'\'' AND pg_get_constraintdef(oid) LIKE '\''%status%running%completed%failed%'\''" | tr -d "[:space:]") &&
  [[ "$STATUS_CHECK" -ge "1" ]] &&
  VARIANT_CHECK=$(dex_psql_query "SELECT count(*) FROM pg_constraint WHERE conrelid = '\''ops.openfda_device_ingest_runs'\''::regclass AND contype = '\''c'\'' AND pg_get_constraintdef(oid) LIKE '\''%source_variant%510k%pma%classification%'\''" | tr -d "[:space:]") &&
  [[ "$VARIANT_CHECK" -ge "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── s3: scripts/run_openfda_device_to_r2.py ─────────────────────────── #
run_surface "s3" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_openfda_device_to_r2.py" &&
  test -f "$F" &&
  python3 -m py_compile "$F" &&
  grep -q "api.fda.gov"                          "$F" &&
  grep -q "openfda/device"                       "$F" &&
  grep -q "snapshot="                            "$F" &&
  grep -qE "ZSTD|zstd"                           "$F" &&
  grep -qE "astype.*str|all_varchar|pa\.string"  "$F" &&
  grep -q "application/x-parquet"                "$F" &&
  grep -q "ops.openfda_device_ingest_runs"       "$F" &&
  grep -q "export_date"                          "$F"
'

# ── s4: modal/openfda_device_app.py ─────────────────────────────────── #
run_surface "s4" "bencrane/hq-all" '
  F="$APP_DIR/modal/openfda_device_app.py" &&
  test -f "$F" &&
  python3 -m py_compile "$F" &&
  grep -q "modal.App(\"data-engine-x-openfda-device\")" "$F" &&
  grep -qE "Cron\("                                     "$F" &&
  grep -q "dex-db"                             "$F" &&
  grep -q "bulk-ingest-r2"                              "$F" &&
  grep -q "run_openfda_device_to_r2"                    "$F" &&
  grep -q "run_openfda_device_lance_emit"               "$F"
'

# ── s5: scripts/run_openfda_device_lance_emit.py ────────────────────── #
# Negative grep: no `duckdb.typing.` literal in CODE (comments excluded per L59).
run_surface "s5" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_openfda_device_lance_emit.py" &&
  test -f "$F" &&
  python3 -m py_compile "$F" &&
  grep -q "device_510k_lance"                "$F" &&
  grep -q "device_pma_lance"                 "$F" &&
  grep -q "device_classification_lance"      "$F" &&
  grep -q "openfda"                          "$F" &&
  grep -q "lance_commit_lock"                "$F" &&
  grep -qE "BTREE|create_scalar_index"       "$F" &&
  ! grep -E "^[^#]*duckdb\.typing\." "$F"
'

# ── s6: app/services/lance_views.py — 3 LanceView entries appended ──── #
run_surface "s6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  test -f "$F" &&
  python3 -m py_compile "$F" &&
  grep -q "device_510k_lance"           "$F" &&
  grep -q "device_pma_lance"            "$F" &&
  grep -q "device_classification_lance" "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first run)
# ====================================================================== #

# ── r1: ≥1 .parquet under each of the 3 openfda/device/{variant}/ prefixes #
run_surface_soft "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    for PREFIX in '"$R2_PREFIX_510K"' '"$R2_PREFIX_PMA"' '"$R2_PREFIX_CLASSIFICATION"'; do
      OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET"'/\$PREFIX/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c \"\.parquet\$\" | tr -d \"[:space:]\")
      [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$PREFIX has \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
      echo \"r1 ok: \$PREFIX objects=\$OBJ_COUNT\"
    done
  "
'

# ====================================================================== #
# Phase 4 — Lance emit (SOFT until first emit run)
# ====================================================================== #

# ── e1: each Lance dataset — row floor + BTREE on canonical PK ───────── #
_E1_510K_PY=$(_lance_check_inline "$LANCE_URI_510K" "$FLOOR_510K" "k_number" "")
_LANCE_CHECK_TMPFILES+=("$_E1_510K_PY")
run_surface_soft "e1-510k" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_510K_PY'
"

_E1_PMA_PY=$(_lance_check_inline "$LANCE_URI_PMA" "$FLOOR_PMA" "pma_number" "")
_LANCE_CHECK_TMPFILES+=("$_E1_PMA_PY")
run_surface_soft "e1-pma" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PMA_PY'
"

_E1_CLASSIFICATION_PY=$(_lance_check_inline "$LANCE_URI_CLASSIFICATION" "$FLOOR_CLASSIFICATION" "product_code" "")
_LANCE_CHECK_TMPFILES+=("$_E1_CLASSIFICATION_PY")
run_surface_soft "e1-classification" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_CLASSIFICATION_PY'
"

# ====================================================================== #
# Phase 5 — Modal deploy
# ====================================================================== #

# ── s7: Modal app data-engine-x-openfda-device deployed (L62) ───────── #
run_surface_soft "s7" "bencrane/hq-all" '
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
exit 0
