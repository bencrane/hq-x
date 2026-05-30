#!/usr/bin/env bash
# Verification harness for cycle `sec-dera-fsds-ingest` (2026-05-18).
#
# Authored 2026-05-18 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-18-sec-dera-fsds-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/2026-05-18-sec-dera-fsds-ingest.sh
#
# Pattern: mirrors `2026-05-18-sec-dera-form-d-ingest.sh` (sub-A gold-standard).
# Shares the same `_lance_check_inline` helper (fixed in PR #539 — `'fields'`
# key + case-insensitive compare) and Modal CLI JSON shape (`.Description`/`.State`).
#
# Surfaces (19 total, phase order):
#   Phase 1 — Migrations:    m1, m2
#   Phase 2 — Code:          c1, c2, c3, c4, c5, c6, c7
#   Phase 3 — R2 backfill:   r1
#   Phase 4 — Lance emits:   e1, e2, e3, e4
#   Phase 5 — Polaris:       p1, p2, p3, p4
#   Phase 6 — Modal deploy:  mod1
#
# Usage:
#   ./2026-05-18-sec-dera-fsds-ingest.sh                       # all surfaces
#   ./2026-05-18-sec-dera-fsds-ingest.sh --surface m1          # one surface
#   ./2026-05-18-sec-dera-fsds-ingest.sh --repo bencrane/hq-all  # repo filter (trivial; single-repo)
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)    REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying sec-dera-fsds-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"

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

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_FSDS_PREFIX="sec-dera/fsds"
LANCE_SUB_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/fsds_sub_lance"
LANCE_TAG_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/fsds_tag_lance"
LANCE_PRE_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/fsds_pre_lance"
LANCE_NUM_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/fsds_num_lance"

MIN_R2_OBJECT_FLOOR=260         # tolerance for ≥260 of expected ~276 (69q × 4 tables)

# Lance volume floors per directive §"Volume floors" (conservative 50-60% of
# operator extrapolation per sub-A's PR #539 "over-estimate" lesson):
FLOOR_SUB=250000
FLOOR_TAG=2500000
FLOOR_PRE=25000000
FLOOR_NUM=150000000

MODAL_APP_NAME="data-engine-x-sec-dera-fsds"

# --- Python lance row-count + BTREE check (writes a temp .py file) ------- #
# Usage: _lance_check_inline <lance_uri> <floor> <required_btree_col1> [<col2>]
#
# Returns: path to a temp .py file that asserts rows>=floor + BTREE on col1
# (and optionally col2). Invoke via `doppler run -- uv run python <returned-path>`.
#
# Why a temp file: inlining Python source through nested bash -c '...' triggers
# single-quote stripping by the outer shell, mangling string literals (rendering
# `ds.dataset('s3://...')` as `ds.dataset(s3://...)`). The temp-file pattern
# sidesteps the quoting nightmare entirely.
#
# Why `'fields'` key + lowercase compare (sub-A PR #539 fix): post-Lance-0.18.x
# the index metadata exposes column names under the `'fields'` key (the old
# `'columns'` key returns []). Case-insensitive compare because the BTREE
# column is canonicalised to lowercase by lance internals while our config
# strings may be lower- or mixed-case in different code paths.
_lance_check_inline() {
  local uri="$1" floor="$2" col1="$3" col2="${4:-}"
  local extra_check=""
  if [[ -n "$col2" ]]; then
    extra_check="assert '$col2'.lower() in idx_cols, f'BTREE on $col2 missing: {idx_cols}'"
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
idx_cols = set()
for i in ds.list_indices():
    for f in (i.get('fields') or i.get('columns') or []):
        idx_cols.add(f.lower())
assert '$col1'.lower() in idx_cols, f'BTREE on $col1 missing: {idx_cols}'
$extra_check
print(f'rows={rows} idx={sorted(idx_cols)}')
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

# ── m1: ops.sec_dera_fsds_r2_ingest_runs table + index ────────────────── #
# Per precedent abs-15g-ingest.sh:11/47, sql literals use '\''...'\'' to survive
# the bash → eval → _dex_doppler → bash → psql expansion chain. Dollar-quoted
# postgres ($tag$...$tag$) does NOT survive — the $tag gets eaten.
run_surface "m1" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''sec_dera_fsds_r2_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_sec_dera_fsds_r2_ingest_runs_release'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]]
'

# ── m2: 5 ops.data_sources rows (1 R2 + 4 Lance) ──────────────────────── #
run_surface "m2" "bencrane/hq-all" '
  COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_sources WHERE display_name LIKE '\''sec_dera.fsds_%_lance'\'' OR display_name = '\''sec-dera/fsds'\''" | tr -d "[:space:]") &&
  [[ "$COUNT" -ge 5 ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_sec_dera_fsds_r2_ingest.py ────────────────────────── #
# Asserts: file exists, parses, contains required invariants:
#   - R2 prefix sec-dera/fsds
#   - User-Agent Mozilla/5.0 (L55)
#   - DuckDB read_csv flags (all_varchar=TRUE, null_padding=TRUE, strict_mode=FALSE)
#   - URL pattern financial-statement-data-sets (NOT form-d, NOT invented suffix)
#   - ZSTD parquet compression
#   - ContentType ONLY (no ContentEncoding header per L42)
#   - Audit-table write to ops.sec_dera_fsds_r2_ingest_runs
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_fsds_r2_ingest.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec-dera/fsds"                          "$F" &&
  grep -q "Mozilla/5.0"                            "$F" &&
  grep -q "all_varchar=TRUE"                       "$F" &&
  grep -q "null_padding=TRUE"                      "$F" &&
  grep -q "strict_mode=FALSE"                      "$F" &&
  grep -q "financial-statement-data-sets"          "$F" &&
  grep -q "COMPRESSION ZSTD"                       "$F" &&
  grep -q "application/x-parquet"                  "$F" &&
  grep -q "ops.sec_dera_fsds_r2_ingest_runs"       "$F" &&
  ! grep -E "^[^#]*ContentEncoding"                "$F"
'

# ── c2: modal/sec_dera_fsds_app.py ────────────────────────────────────── #
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/sec_dera_fsds_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-sec-dera-fsds"     "$F" &&
  grep -qE "Cron\("                          "$F" &&
  grep -q "run_fsds_backfill"               "$F" &&
  grep -q "dex-db"                 "$F" &&
  grep -q "bulk-ingest-r2"                  "$F"
'

# ── c3: scripts/run_sec_dera_fsds_sub_lance_emit.py ───────────────────── #
# Also asserts the helper extension (partition_mode="multi_release") is live in
# _lib/lance_emit.py (shipped in sub-A's PR #536; sub-B inherits — guards
# against accidental regression).
run_surface "c3" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_fsds_sub_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "fsds_sub_lance" "$F" &&
  grep -q "multi_release"   "$F" &&
  grep -q "adsh"            "$F" &&
  grep -q "multi_release" "$APP_DIR/scripts/_lib/lance_emit.py"
'

# ── c4: scripts/run_sec_dera_fsds_tag_lance_emit.py ───────────────────── #
# Composite-key dataset (tag, version); BTREE on tag.
run_surface "c4" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_fsds_tag_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "fsds_tag_lance" "$F" &&
  grep -q "multi_release"   "$F" &&
  grep -q "tag"             "$F"
'

# ── c5: scripts/run_sec_dera_fsds_pre_lance_emit.py ───────────────────── #
# ~50M rows historical — 32GB Modal required for BTREE creation
# (LANCE_BYPASS_SPILLING; PR #464 precedent).
run_surface "c5" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_fsds_pre_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "fsds_pre_lance"        "$F" &&
  grep -q "multi_release"          "$F" &&
  grep -q "adsh"                   "$F" &&
  grep -q "LANCE_BYPASS_SPILLING"  "$F"
'

# ── c6: scripts/run_sec_dera_fsds_num_lance_emit.py ───────────────────── #
# ~200M+ rows historical — largest Lance dataset to date.
# Modal memory=65536 (64GB) AND timeout=14400 (4h) on Modal function decorator.
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_fsds_num_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "fsds_num_lance"        "$F" &&
  grep -q "multi_release"          "$F" &&
  grep -q "adsh"                   "$F" &&
  grep -q "tag"                    "$F" &&
  grep -q "LANCE_BYPASS_SPILLING"  "$F"
'

# ── c7: app/services/lance_views.py — 4 LanceView entries appended ────── #
# Asserts BOTH presence of all 4 FSDS entries AND register_at_boot=False for
# the two heavy datasets (fsds_pre_lance + fsds_num_lance) per ARCHITECTURE-
# PATTERNS anti-pattern §"Materialize a Pattern A Lance view via the boot-time
# DuckDB Arrow bridge".
run_surface "c7" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "fsds_sub_lance" "$F" &&
  grep -q "fsds_tag_lance" "$F" &&
  grep -q "fsds_pre_lance" "$F" &&
  grep -q "fsds_num_lance" "$F" &&
  grep -E "name=.sec_dera_fsds_pre_lance_raw.*" -A 14 "$F" | grep -qE "^[[:space:]]+register_at_boot=False," &&
  grep -E "name=.sec_dera_fsds_num_lance_raw.*" -A 14 "$F" | grep -qE "^[[:space:]]+register_at_boot=False,"
'

# ====================================================================== #
# Phase 3 — R2 backfill
# ====================================================================== #

# ── r1: R2 prefix sec-dera/fsds/ has ≥260 objects (≥260 of ~276) ──────── #
# R2 cred-mapping: AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
# are required for `aws s3 ls` to authenticate against R2 (per precedent
# scripts/migration-checks/abs-15g-ingest.sh:83 + ucc-ca-master-ingest.sh:75).
run_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_FSDS_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | wc -l | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
    echo \"r1 ok: objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emits (count_rows + BTREE)
# ====================================================================== #

# ── e1: fsds_sub_lance — floor 250K, BTREE on adsh + cik ──────────────── #
# Build the temp .py at surface-list-time; harness eval runs `uv run python <tmpfile>`
# from $APP_DIR so the project's uv venv (with `pylance>=6.0` per pyproject.toml)
# resolves. Bare `python3` would ModuleNotFoundError on lance.
_E1_PY=$(_lance_check_inline "$LANCE_SUB_URI" "$FLOOR_SUB" "adsh" "cik"); _LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ── e2: fsds_tag_lance — floor 2.5M, BTREE on tag ─────────────────────── #
_E2_PY=$(_lance_check_inline "$LANCE_TAG_URI" "$FLOOR_TAG" "tag"); _LANCE_CHECK_TMPFILES+=("$_E2_PY")
run_surface "e2" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# ── e3: fsds_pre_lance — floor 25M, BTREE on adsh ─────────────────────── #
# (Read path needs no 32GB; write path does. This is read-only verification.)
_E3_PY=$(_lance_check_inline "$LANCE_PRE_URI" "$FLOOR_PRE" "adsh"); _LANCE_CHECK_TMPFILES+=("$_E3_PY")
run_surface "e3" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E3_PY'
"

# ── e4: fsds_num_lance — floor 150M, BTREE on adsh + tag ──────────────── #
# (Largest Lance dataset to date; read path verification only.)
_E4_PY=$(_lance_check_inline "$LANCE_NUM_URI" "$FLOOR_NUM" "adsh" "tag"); _LANCE_CHECK_TMPFILES+=("$_E4_PY")
run_surface "e4" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E4_PY'
"

# ====================================================================== #
# Phase 5 — Polaris Generic Tables (sec_dera namespace, existing from sub-A)
# ====================================================================== #
# All 4 use --check-only against the same idempotent helper.

run_surface "p1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table fsds_sub_lance --check-only
  "
'

run_surface "p2" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table fsds_tag_lance --check-only
  "
'

run_surface "p3" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table fsds_pre_lance --check-only
  "
'

run_surface "p4" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table fsds_num_lance --check-only
  "
'

# ====================================================================== #
# Phase 6 — Modal deploy
# ====================================================================== #

# ── mod1: Modal app data-engine-x-sec-dera-fsds deployed ──────────────── #
# `.Description` and `.State` keys are capitalised in `modal app list --json`
# output post-Modal-CLI-0.65 (sub-A's PR #538 hotfix); the fallback `.name`/
# `.state` paths are kept for forward-compat with older CLI versions.
run_surface "mod1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app list --json | jq -e \".[] | select(.Description==\\\"'"$MODAL_APP_NAME"'\\\" or .name==\\\"'"$MODAL_APP_NAME"'\\\") | select(.State==\\\"deployed\\\" or .state==\\\"deployed\\\")\" >/dev/null
  "
'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
