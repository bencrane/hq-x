#!/usr/bin/env bash
# Verification harness for cycle `usaspending-sos-ny-owner-bridge` (2026-05-19).
#
# Pattern B Lance bridge — USAspending federal contract recipients (NY state)
# × NY DoS Active Corporations. REUSER of legal_name_state_exact_ny v1.0.0
# (publisher PR #513, 2026-05-18). EIGHTH REUSER (after sba_ny_contracts,
# sba_ny_usaspending, sba_ny_sam, sba_ny_nyc_contracts, sba_ny_mta,
# sba_ny_local_authority, sam_sos_ny_entities from PR #569).
#
# Structural precedents:
#   - PR #569 SAM × NY SoS entities bridge — NY-side handling, REUSER discipline,
#     harness shape (post-PR-#570 with sys.path and to_char quoting fixes).
#   - PR #487 USAspending × CA SoS owner bridge — LEFT-spine handling
#     (contracts_lance filter + DISTINCT + Python-side normalize).
#
# Surfaces (5 hard surfaces + 4 post-deploy verify-only):
#   Phase 1 — Migrations:    m1 (catalog row + view recreation)
#   Phase 2 — Code:          c1 (build script), c2 (Modal cron), c6 (LanceViews entry)
#   Phase 3 — R2:            r1 (Lance dataset present)              [SOFT — strict post-deploy]
#   Phase 4 — Lance:         e1 (row floor + dual-BTREE)             [SOFT — strict post-deploy]
#   Phase 5 — DB invariants: e2 (REUSER framing — method row UNCHANGED, bridge row CREATED) [SOFT]
#   Phase 6 — Deploy:        s1 (Modal app deployed)                 [SOFT — strict post-deploy]
#
# Usage:
#   ./usaspending-sos-ny-owner-bridge.sh                  # all surfaces (r1/e1/e2/s1 soft)
#   ./usaspending-sos-ny-owner-bridge.sh --surface m1     # one surface only
#   STRICT=1 ./usaspending-sos-ny-owner-bridge.sh         # post-deploy: missing r1/e1/e2/s1 FAIL
#
# Worktree-aware invocation per L61 (mirror of PR #569 / NY ingest cycle):
#   HQ_ALL_ROOT=/Users/benjamincrane/hq-all/.claude/worktrees/competent-rubin-fcce42 \
#   STRICT=1 \
#     bash apps/data-engine-x/scripts/migration-checks/usaspending-sos-ny-owner-bridge.sh

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

echo "==> Verifying usaspending-sos-ny-owner-bridge (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} strict=$STRICT)"

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
R2_PREFIX="polaris-warehouse/bridges/usaspending_sos_ny_owner_lance"
LANCE_URI="s3://${R2_BUCKET}/${R2_PREFIX}"
FLOOR_ROWS=2832         # MIN_ROWS_MATCHED (validator-calibrated 2026-05-19; ~70% of 4,046 probe yield)
MIN_R2_OBJECT_FLOOR=1   # one Lance dataset under the prefix

# Method pre-state lock (validator p2 / e2): match_methods.created_at for
# legal_name_state_exact_ny MUST equal this exact timestamp post-build.
# Mismatch = executor accidentally UPSERTed the method row (corruption).
METHOD_PRE_STATE_CREATED_AT="2026-05-18T06:38:25.095232+00:00"
METHOD_VER_PRE_STATE_CREATED_AT="2026-05-18T06:38:25.302138+00:00"

# --- Lance row-count + dual-BTREE inline checker (writes temp .py) ------ #
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

# ── m1: data_source_catalog INSERT + view recreation ─────────────────── #
# The audit_ledger_table column points at ops.bridge_generation_runs
# (shared across all bridges); no per-bridge _ingest_runs table is created.
# View MUST include a new usaspending_sos_ny_owner branch.
run_surface "m1" "bencrane/hq-all" '
  CATALOG_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog WHERE source_slug='\''usaspending_sos_ny_owner'\'' AND is_active = TRUE" | tr -d "[:space:]") &&
  [[ "$CATALOG_COUNT" = "1" ]] &&
  VIEW_EXISTS=$(dex_psql_query "SELECT 1 FROM information_schema.views WHERE table_schema='\''ops'\'' AND table_name='\''data_source_catalog_status'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_EXISTS" = "1" ]] &&
  VIEW_BRANCH=$(dex_psql_query "SELECT count(*) FROM ops.data_source_catalog_status WHERE source_slug='\''usaspending_sos_ny_owner'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_BRANCH" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/build_bridge_usaspending_sos_ny_owner_lance.py ───────── #
# REUSER pattern: register_bridge + start/complete/fail_bridge_run ONLY.
# Method-definition helpers (register_match_method,
# register_match_method_version) are INTENTIONALLY OMITTED — validator p2.
# Greps positive for required identifiers; negative for anti-patterns.
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/build_bridge_usaspending_sos_ny_owner_lance.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "legal_name_state_exact_ny"        "$F" &&
  grep -q "usaspending_sos_ny_owner"         "$F" &&
  grep -q "usaspending/contracts_lance"      "$F" &&
  grep -q "sos/ny_active_corporations_lance" "$F" &&
  grep -q "register_bridge"                  "$F" &&
  grep -q "start_bridge_run"                 "$F" &&
  grep -q "complete_bridge_run"              "$F" &&
  grep -q "fail_bridge_run"                  "$F" &&
  grep -q "lance_commit_lock"                "$F" &&
  grep -q "recipient_uei"                    "$F" &&
  grep -q "sos_dos_id"                       "$F" &&
  grep -q "recipient_state_code"             "$F" &&
  grep -qE "_lib\\.entity_name_normalize|from scripts\\._lib\\.entity_name_normalize|entity_name_normalize" "$F" &&
  grep -qE "MIN_ROWS_MATCHED *= *2_832|MIN_ROWS_MATCHED *= *2832" "$F" &&
  ! grep -qE "^[^#]*register_match_method"      "$F" &&
  ! grep -qE "^[^#]*sos_entity_num"             "$F" &&
  ! grep -qE "^[^#]*duckdb\\.typing\\."         "$F" &&
  ! grep -qE "^[^#]*Content-Encoding.*zstd"     "$F" &&
  ! grep -qE "^[^#]*LIST<VARCHAR>"              "$F"
'

# ── c2: modal/usaspending_sos_ny_owner_bridge_app.py ─────────────────── #
# Modal app name + Cron schedule + secrets + delegates to c1 build script.
# Per PR #570 lesson: sys.path.insert(0, "/root/scripts") — NOT "/root".
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/usaspending_sos_ny_owner_bridge_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-usaspending-sos-ny-owner-bridge" "$F" &&
  grep -q "Cron("                                         "$F" &&
  grep -q "dex-db"                               "$F" &&
  grep -q "bulk-ingest-r2"                                "$F" &&
  grep -q "timeout="                                      "$F" &&
  grep -q "memory="                                       "$F" &&
  grep -q "build_bridge_usaspending_sos_ny_owner_lance"   "$F" &&
  grep -qE "sys\\.path\\.insert\\(0, *[\"'\'']/root/scripts[\"'\'']\\)" "$F"
'

# ── c6: app/services/lance_views.py — LanceView entry appended ──────── #
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "usaspending_sos_ny_owner_lance" "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill (SOFT until post-deploy first refresh)
# ====================================================================== #

# ── r1: bridge Lance dataset prefix has ≥1 object ────────────────────── #
run_surface_soft "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | wc -l | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
    echo \"r1 ok: objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emit (SOFT until first refresh run)
# ====================================================================== #

# ── e1: Lance row floor ≥ 2,832 + dual-BTREE on recipient_uei + sos_dos_id ─ #
_E1_PY=$(_lance_check_inline "$LANCE_URI" "$FLOOR_ROWS" "recipient_uei" "sos_dos_id")
_LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface_soft "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ====================================================================== #
# Phase 5 — DB invariants (SOFT until first refresh — these can only pass
# after the build script has run end-to-end and written to the DB)
# REUSER framing per validator p2 / e2:
#   - method row UNCHANGED (PR #513's 2026-05-18 timestamp preserved)
#   - method_versions row UNCHANGED
#   - ops.bridges has a NEW row for usaspending_sos_ny_owner (this cycle owns it)
#   - ops.bridge_generation_runs has ≥1 completed row for this bridge
# ====================================================================== #

run_surface_soft "e2" "bencrane/hq-all" '
  # match_methods row exactly 1 (UNCHANGED — count parity vs pre-state)
  MM_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.match_methods WHERE method_name='\''legal_name_state_exact_ny'\''" | tr -d "[:space:]") &&
  [[ "$MM_COUNT" = "1" ]] &&
  # match_method_versions row exactly 1 (UNCHANGED).
  # SCHEMA NOTE (per PR #569 review-correction 2026-05-19): ops.match_method_versions
  # has NO method_name column — it FKs to ops.match_methods via match_method_id.
  # Query MUST JOIN through that FK, not filter on a nonexistent column.
  MMV_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.match_method_versions v JOIN ops.match_methods m USING (match_method_id) WHERE m.method_name='\''legal_name_state_exact_ny'\'' AND v.semver='\''1.0.0'\''" | tr -d "[:space:]") &&
  [[ "$MMV_COUNT" = "1" ]] &&
  # ops.bridges has new row for this bridge
  BR_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.bridges WHERE bridge_name='\''usaspending_sos_ny_owner'\''" | tr -d "[:space:]") &&
  [[ "$BR_COUNT" = "1" ]] &&
  # ≥1 completed bridge_generation_runs row
  RUN_COUNT=$(dex_psql_query "SELECT count(*) FROM ops.bridge_generation_runs WHERE bridge_name='\''usaspending_sos_ny_owner'\'' AND status='\''completed'\''" | tr -d "[:space:]") &&
  [[ "$RUN_COUNT" -ge 1 ]] &&
  # CRITICAL — method row created_at matches PR #513 pre-state (validator p2 lock):
  # If executor accidentally UPSERTed (called register_match_method), created_at
  # would shift to the build-script run timestamp. This check fails.
  # Per PR #570 quoting fix: use (created_at)::text + space-stripped prefix match
  # ("2026-05-1806:38:25*") because tr -d [:space:] strips the space between
  # date and time in the cast output. Logically equivalent invariant check.
  # Pre-state pinned constants (see METHOD_PRE_STATE_CREATED_AT above):
  #   ops.match_methods.created_at         = 2026-05-18T06:38:25.095232+00
  #   ops.match_method_versions.created_at = 2026-05-18T06:38:25.302138+00
  MM_CREATED=$(dex_psql_query "SELECT (created_at AT TIME ZONE '\''UTC'\'')::text FROM ops.match_methods WHERE method_name='\''legal_name_state_exact_ny'\''" | tr -d "[:space:]") &&
  echo "method created_at = $MM_CREATED (expect 2026-05-18 06:38:25 prefix)" &&
  [[ "$MM_CREATED" == "2026-05-1806:38:25"* ]] &&
  # Symmetric check on the version row — register_match_method_version UPSERT
  # would shift this too. Same JOIN-through-match_method_id schema as MMV_COUNT.
  MMV_CREATED=$(dex_psql_query "SELECT (v.created_at AT TIME ZONE '\''UTC'\'')::text FROM ops.match_method_versions v JOIN ops.match_methods m USING (match_method_id) WHERE m.method_name='\''legal_name_state_exact_ny'\'' AND v.semver='\''1.0.0'\''" | tr -d "[:space:]") &&
  echo "method_version created_at = $MMV_CREATED (expect 2026-05-18 06:38:25 prefix)" &&
  [[ "$MMV_CREATED" == "2026-05-1806:38:25"* ]]
'

# ====================================================================== #
# Phase 6 — Modal deploy (SOFT until post-deploy)
# ====================================================================== #

# ── s1: Modal app deployed (capitalized fields per L62) ──────────────── #
run_surface_soft "s1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    modal app list --json | jq -e \".[] | select(.Description==\\\"data-engine-x-usaspending-sos-ny-owner-bridge\\\") | select(.State==\\\"deployed\\\")\" >/dev/null
  "
'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
