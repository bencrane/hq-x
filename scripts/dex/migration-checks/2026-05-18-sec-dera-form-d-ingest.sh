#!/usr/bin/env bash
# Verification harness for cycle `sec-dera-form-d-ingest` (2026-05-18).
#
# Authored 2026-05-18 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-18-sec-dera-form-d-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/2026-05-18-sec-dera-form-d-ingest.sh
#
# Pattern: mirrors `usaspending-recipient-grain-lance-emit-v1.sh`.
#
# Surfaces (25 total, phase order):
#   Phase 1 — Migrations:    m1, m2
#   Phase 2 — Code:          c1, c2, c3, c4, c5, c6, c7, c8, c9
#   Phase 3 — R2 backfill:   r1
#   Phase 4 — Lance emits:   e1, e2, e3, e4, e5, e6
#   Phase 5 — Polaris:       p1, p2, p3, p4, p5, p6
#   Phase 6 — Modal deploy:  mod1
#
# Usage:
#   ./2026-05-18-sec-dera-form-d-ingest.sh                       # all surfaces
#   ./2026-05-18-sec-dera-form-d-ingest.sh --surface m1          # one surface
#   ./2026-05-18-sec-dera-form-d-ingest.sh --repo bencrane/hq-all  # repo filter (trivial; single-repo)
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

echo "==> Verifying sec-dera-form-d-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all})"

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
R2_FORM_D_PREFIX="sec-dera/form-d"
LANCE_SUBMISSION_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_submission_lance"
LANCE_ISSUERS_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_issuers_lance"
LANCE_OFFERING_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_offering_lance"
LANCE_RECIPIENTS_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_recipients_lance"
LANCE_RELATED_PERSONS_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_related_persons_lance"
LANCE_SIGNATURES_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_dera/form_d_signatures_lance"

MIN_R2_OBJECT_FLOOR=420         # tolerance for ≥420 of expected ~438 (73q × 6 tables; actual 437 — 2008q2 recipients TSV missing)

# Lance volume floors — calibrated against REALIZED 2026-05-18 emit volumes,
# not the 2026q1 × 73 extrapolation (older quarters 2008-2011 were much
# thinner than 2026q1, so extrapolation over-estimates). Floors set at ~90%
# of realized to absorb future-quarter variance.
FLOOR_SUBMISSION=670000        # realized 746,374
FLOOR_ISSUERS=680000           # realized 757,857
FLOOR_OFFERING=670000          # realized 746,374
FLOOR_RECIPIENTS=370000        # realized 418,658
FLOOR_RELATED_PERSONS=2500000  # realized 2,882,264 (matches prior estimate)
FLOOR_SIGNATURES=670000        # realized 753,568

MODAL_APP_NAME="data-engine-x-sec-dera-form-d"

# --- Python lance row-count + BTREE check (writes a temp .py file) ------- #
# Usage: _lance_check_inline <lance_uri> <floor> <required_btree_col1> [<col2>]
#
# Returns: path to a temp .py file that asserts rows>=floor + BTREE on col1
# (and optionally col2). Invoke via `doppler run -- python3 <returned-path>`.
#
# Why a temp file: inlining Python source through nested bash -c '...' triggers
# single-quote stripping by the outer shell, mangling string literals (rendering
# `ds.dataset('s3://...')` as `ds.dataset(s3://...)`). The temp-file pattern
# sidesteps the quoting nightmare entirely.
_lance_check_inline() {
  local uri="$1" floor="$2" col1="$3" col2="${4:-}"
  local extra_check=""
  if [[ -n "$col2" ]]; then
    extra_check="assert '$col2'.lower() in idx_cols_lower, f'BTREE on $col2 missing: {idx_cols_lower}'"
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
# Lance 1.5.x: list_indices() returns dicts with 'fields' key (NOT 'columns').
# Source-TSV uppercase column names are preserved through DuckDB read_csv +
# Lance write, so compare case-insensitively.
idx_cols_lower = set()
for i in ds.list_indices():
    for f in i.get('fields') or i.get('columns') or []:
        idx_cols_lower.add(f.lower())
assert '$col1'.lower() in idx_cols_lower, f'BTREE on $col1 missing: {idx_cols_lower}'
$extra_check
print(f'rows={rows} idx={idx_cols_lower}')
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

# ── m1: ops.sec_dera_form_d_r2_ingest_runs table + index ──────────────── #
# Per precedent abs-15g-ingest.sh:11/47, sql literals use '\''...'\'' to survive
# the bash → eval → _dex_doppler → bash → psql expansion chain. Dollar-quoted
# postgres ($tag$...$tag$) does NOT survive — the $tag gets eaten.
run_surface "m1" "bencrane/hq-all" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''sec_dera_form_d_r2_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_sec_dera_form_d_r2_ingest_runs_release'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]]
'

# ── m2: 7 ops.data_sources rows (1 R2 + 6 Lance) ──────────────────────── #
run_surface "m2" "bencrane/hq-all" '
  COUNT=$(dex_psql_query "SELECT count(*) FROM ops.data_sources WHERE display_name LIKE '\''sec_dera.form_d_%_lance'\'' OR display_name = '\''sec-dera/form-d'\''" | tr -d "[:space:]") &&
  [[ "$COUNT" -ge 7 ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── c1: scripts/run_sec_dera_form_d_r2_ingest.py ──────────────────────── #
# Asserts: file exists, parses, contains required invariants:
#   - R2 prefix sec-dera/form-d
#   - User-Agent Mozilla/5.0 (L55)
#   - DuckDB read_csv flags (all_varchar=TRUE, null_padding=TRUE, strict_mode=FALSE)
#   - ZSTD parquet compression
#   - ContentType ONLY (no ContentEncoding header per L42)
#   - Audit-table write to ops.sec_dera_form_d_r2_ingest_runs
run_surface "c1" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_r2_ingest.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec-dera/form-d"                        "$F" &&
  grep -q "Mozilla/5.0"                            "$F" &&
  grep -q "all_varchar=TRUE"                       "$F" &&
  grep -q "null_padding=TRUE"                      "$F" &&
  grep -q "strict_mode=FALSE"                      "$F" &&
  grep -q "COMPRESSION ZSTD"                       "$F" &&
  grep -q "application/x-parquet"                  "$F" &&
  grep -q "ops.sec_dera_form_d_r2_ingest_runs"     "$F" &&
  ! grep -E "^[^#]*ContentEncoding"                "$F"
'

# ── c2: modal/sec_dera_form_d_app.py ──────────────────────────────────── #
run_surface "c2" "bencrane/hq-all" '
  F="$APP_DIR/modal/sec_dera_form_d_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-sec-dera-form-d"  "$F" &&
  grep -qE "Cron\(\"0 4 \* \* \*\"\)"       "$F" &&
  grep -q "run_form_d_backfill"             "$F" &&
  grep -q "dex-db"                 "$F" &&
  grep -q "bulk-ingest-r2"                  "$F"
'

# ── c3: scripts/run_sec_dera_form_d_submission_lance_emit.py ──────────── #
# Also asserts the helper extension (partition_mode="multi_release") landed in
# _lib/lance_emit.py — guards against the "thin-wrap looks fine but helper
# never extended" failure mode.
run_surface "c3" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_submission_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_submission_lance" "$F" &&
  grep -q "multi_release"                     "$F" &&
  grep -q "accessionnumber"                   "$F" &&
  grep -q "multi_release" "$APP_DIR/scripts/_lib/lance_emit.py"
'

# ── c4: scripts/run_sec_dera_form_d_issuers_lance_emit.py ─────────────── #
# Composite-key dataset: BTREE on accessionnumber + secondary BTREE on cik
# (built post-emit via direct ds.create_scalar_index call).
run_surface "c4" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_issuers_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_issuers_lance" "$F" &&
  grep -q "multi_release"                  "$F" &&
  grep -q "accessionnumber"                "$F" &&
  grep -qE "\"cik\".*BTREE"                "$F"
'

# ── c5: scripts/run_sec_dera_form_d_offering_lance_emit.py ────────────── #
run_surface "c5" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_offering_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_offering_lance" "$F" &&
  grep -q "multi_release"                   "$F" &&
  grep -q "accessionnumber"                 "$F"
'

# ── c6: scripts/run_sec_dera_form_d_recipients_lance_emit.py ──────────── #
# Composite-key: BTREE on accessionnumber + secondary BTREE on recipientcrdnumber.
run_surface "c6" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_recipients_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_recipients_lance" "$F" &&
  grep -q "multi_release"                     "$F" &&
  grep -q "accessionnumber"                   "$F" &&
  grep -qE "\"recipientcrdnumber\".*BTREE"    "$F"
'

# ── c7: scripts/run_sec_dera_form_d_related_persons_lance_emit.py ─────── #
# ~3.9M rows historical — 32GB Modal required for BTREE creation (LANCE_BYPASS_SPILLING).
run_surface "c7" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_related_persons_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_related_persons_lance" "$F" &&
  grep -q "multi_release"                          "$F" &&
  grep -q "accessionnumber"                        "$F" &&
  grep -q "LANCE_BYPASS_SPILLING"                  "$F"
'

# ── c8: scripts/run_sec_dera_form_d_signatures_lance_emit.py ──────────── #
run_surface "c8" "bencrane/hq-all" '
  F="$APP_DIR/scripts/run_sec_dera_form_d_signatures_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "sec_dera_form_d_signatures_lance" "$F" &&
  grep -q "multi_release"                     "$F" &&
  grep -q "accessionnumber"                   "$F"
'

# ── c9: app/services/lance_views.py — 6 LanceView entries appended ────── #
run_surface "c9" "bencrane/hq-all" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "sec_dera_form_d_submission_lance"      "$F" &&
  grep -q "sec_dera_form_d_issuers_lance"          "$F" &&
  grep -q "sec_dera_form_d_offering_lance"         "$F" &&
  grep -q "sec_dera_form_d_recipients_lance"       "$F" &&
  grep -q "sec_dera_form_d_related_persons_lance"  "$F" &&
  grep -q "sec_dera_form_d_signatures_lance"       "$F"
'

# ====================================================================== #
# Phase 3 — R2 backfill
# ====================================================================== #

# ── r1: R2 prefix sec-dera/form-d/ has ≥420 objects (≥420 of ~438) ────── #
# R2 cred-mapping: AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
# are required for `aws s3 ls` to authenticate against R2 (per precedent
# scripts/migration-checks/abs-15g-ingest.sh:83 + ucc-ca-master-ingest.sh:75).
run_surface "r1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    OBJ_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_FORM_D_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | wc -l | tr -d \"[:space:]\")
    [[ \"\$OBJ_COUNT\" -ge '"$MIN_R2_OBJECT_FLOOR"' ]] || { echo \"r1 floor breach: \$OBJ_COUNT < '"$MIN_R2_OBJECT_FLOOR"'\"; exit 1; }
    echo \"r1 ok: objects=\$OBJ_COUNT\"
  "
'

# ====================================================================== #
# Phase 4 — Lance emits (count_rows + BTREE)
# ====================================================================== #

# ── e1: form_d_submission_lance — floor 800K, BTREE on accessionnumber ── #
# Build the temp .py at surface-list-time; harness eval runs `uv run python <tmpfile>`
# from $APP_DIR so the project's uv venv (with `pylance>=6.0` per pyproject.toml)
# resolves. Bare `python3` would ModuleNotFoundError on lance.
_E1_PY=$(_lance_check_inline "$LANCE_SUBMISSION_URI" "$FLOOR_SUBMISSION" "accessionnumber"); _LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_surface "e1" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ── e2: form_d_issuers_lance — floor 900K, BTREE on accessionnumber + cik #
_E2_PY=$(_lance_check_inline "$LANCE_ISSUERS_URI" "$FLOOR_ISSUERS" "accessionnumber" "cik"); _LANCE_CHECK_TMPFILES+=("$_E2_PY")
run_surface "e2" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# ── e3: form_d_offering_lance — floor 800K, BTREE on accessionnumber ──── #
_E3_PY=$(_lance_check_inline "$LANCE_OFFERING_URI" "$FLOOR_OFFERING" "accessionnumber"); _LANCE_CHECK_TMPFILES+=("$_E3_PY")
run_surface "e3" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E3_PY'
"

# ── e4: form_d_recipients_lance — floor 350K, BTREE on accessionnumber + recipientcrdnumber #
_E4_PY=$(_lance_check_inline "$LANCE_RECIPIENTS_URI" "$FLOOR_RECIPIENTS" "accessionnumber" "recipientcrdnumber"); _LANCE_CHECK_TMPFILES+=("$_E4_PY")
run_surface "e4" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E4_PY'
"

# ── e5: form_d_related_persons_lance — floor 2.5M, BTREE on accessionnumber #
# (Read path needs no 32GB; write path does. This is read-only verification.)
_E5_PY=$(_lance_check_inline "$LANCE_RELATED_PERSONS_URI" "$FLOOR_RELATED_PERSONS" "accessionnumber"); _LANCE_CHECK_TMPFILES+=("$_E5_PY")
run_surface "e5" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E5_PY'
"

# ── e6: form_d_signatures_lance — floor 850K, BTREE on accessionnumber ── #
_E6_PY=$(_lance_check_inline "$LANCE_SIGNATURES_URI" "$FLOOR_SIGNATURES" "accessionnumber"); _LANCE_CHECK_TMPFILES+=("$_E6_PY")
run_surface "e6" "bencrane/hq-all" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E6_PY'
"

# ====================================================================== #
# Phase 5 — Polaris Generic Tables (sec_dera namespace)
# ====================================================================== #
# All 6 use --check-only against the same idempotent helper.

run_surface "p1" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_submission_lance --check-only
  "
'

run_surface "p2" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_issuers_lance --check-only
  "
'

run_surface "p3" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_offering_lance --check-only
  "
'

run_surface "p4" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_recipients_lance --check-only
  "
'

run_surface "p5" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_related_persons_lance --check-only
  "
'

run_surface "p6" "bencrane/hq-all" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace sec_dera --table form_d_signatures_lance --check-only
  "
'

# ====================================================================== #
# Phase 6 — Modal deploy
# ====================================================================== #

# ── mod1: Modal app data-engine-x-sec-dera-form-d deployed ────────────── #
# Modal CLI JSON shape: [{"App ID":"ap-...","Description":"<name>","State":"deployed",...}]
# (capitalized + word `Description`, not `name` — Modal CLI changed schema)
run_surface "mod1" "bencrane/hq-all" '
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
