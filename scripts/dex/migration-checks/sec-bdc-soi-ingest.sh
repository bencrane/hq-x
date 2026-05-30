#!/usr/bin/env bash
# Verification harness for cycle `sec-bdc-soi-ingest` (2026-05-20).
#
# Authored 2026-05-20 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-20-sec-bdc-soi-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/sec-bdc-soi-ingest.sh
#
# Pattern: mirrors `2026-05-18-sec-dera-fsds-ingest.sh` (closest precedent —
# SEC source family, Volume-King R2→Lance, Modal scheduled ingest). Shares the
# `_lance_check_inline` temp-file helper (Lance row-count + BTREE, `'fields'`
# key + case-insensitive compare) and the capitalized Modal CLI JSON shape
# (`.Description`/`.State`).
#
# Surfaces (12 total, declared order):
#   Phase 1 — Migrations:  s1, s2
#   Phase 2 — Code:        s3, s4, s5, s6
#   Phase 3 — Polaris:     s7
#   Phase 4 — Modal:       s8, s9
#   Phase 5 — Verify-only: r1, e1, e2   (soft pre-deploy; STRICT=1 post-deploy)
#
# r1/e1/e2 are soft pre-deploy: a missing R2 prefix / Lance dataset reports
# SOFT-SKIP and does NOT fail the run. Post-deploy, run with STRICT=1 to make
# them hard gates (per directive §"Verification harness").
#
# Usage:
#   ./sec-bdc-soi-ingest.sh                              # all surfaces (soft r1/e1/e2)
#   ./sec-bdc-soi-ingest.sh --surface s1                 # one surface
#   ./sec-bdc-soi-ingest.sh --repo data-engine-x         # repo filter (single-repo cycle)
#   STRICT=1 ./sec-bdc-soi-ingest.sh                     # post-deploy: r1/e1/e2 hard-gated
#   HQ_ALL_ROOT=/path/to/worktree ./sec-bdc-soi-ingest.sh  # worktree override
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

STRICT="${STRICT:-0}"

echo "==> Verifying sec-bdc-soi-ingest (repo=${REPO_FILTER:-all} surface=${SURFACE_FILTER:-all} STRICT=$STRICT)"

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

# run_soft_surface — for r1/e1/e2. Pre-deploy (STRICT=0): a verify miss reports
# SOFT-SKIP and does NOT increment FAIL_COUNT. Post-deploy (STRICT=1): identical
# to run_surface — a miss is a hard FAIL.
run_soft_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- $id ($repo): RUNNING (soft; STRICT=$STRICT)"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    if [[ "$STRICT" -eq 1 ]]; then
      echo "-- $id ($repo): FAIL (STRICT)" >&2
      FAIL_COUNT=$((FAIL_COUNT+1))
    else
      echo "-- $id ($repo): SOFT-SKIP (pre-deploy; re-run with STRICT=1 post-deploy)"
      SKIP_COUNT=$((SKIP_COUNT+1))
    fi
  fi
}

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_SOI_PREFIX="sec-bdc/soi"
R2_TXT_PREFIX="sec-bdc/txt"
LANCE_SOI_URI="s3://${R2_BUCKET}/polaris-warehouse/sec_bdc/soi_lance"

MODAL_APP_NAME="data-engine-x-sec-bdc-soi"
POLARIS_NAMESPACE="sec_bdc"
POLARIS_TABLE="soi_lance"

# Volume floors per directive §"Volume floors" (validator-set 2026-05-20):
#   e1: 450,000 total rows across all periods (10 quarterly snapshots restate
#       full holdings + 13 monthly deltas; ≈57% of the ~793K extrapolation).
FLOOR_E1=450000
#   e2: ≥55% maturity-date coverage of DEBT-LIKE rows across the top-20 BDCs
#       by investment-row count. Debt-like predicate pinned below.
E2_COVERAGE_FLOOR=55
E2_TOP_N_BDCS=20
# Debt-like predicate (directive §e2): identifier / instrument_type text
# matching lien|loan|note|bond|debt|term. Applied case-insensitively in s5's
# emitted Lance dataset against the fused identifier column AND the parsed
# instrument_type column.
E2_DEBTLIKE_REGEX='(?i)(lien|loan|note|bond|debt|term)'

# --- Python Lance row-count + BTREE check (writes a temp .py file) ------- #
# Usage: _lance_check_inline <lance_uri> <floor> <required_btree_col1> [<col2>]
#
# Returns: path to a temp .py file that asserts rows>=floor + BTREE on col1
# (and optionally col2). Invoke via `doppler run -- uv run python <returned-path>`.
#
# Why a temp file: inlining Python source through nested bash -c '...' triggers
# single-quote stripping by the outer shell, mangling string literals. The
# temp-file pattern sidesteps the quoting nightmare entirely.
#
# Why `'fields'` key + lowercase compare (sub-A PR #539 fix): post-Lance-0.18.x
# the index metadata exposes column names under the `'fields'` key (the old
# `'columns'` key returns []). Case-insensitive compare because the BTREE
# column is canonicalised to lowercase by lance internals.
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

# --- Python e2 coverage check (writes a temp .py file) ------------------- #
# Reads the Lance dataset via DuckDB-on-Lance. For the top-20 BDCs by
# investment-row count, restricts to DEBT-LIKE rows (fused identifier OR
# parsed instrument_type matching lien|loan|note|bond|debt|term) and asserts
# maturity_date is non-null for >= 55% of them.
#
# Column names below are the typed siblings s5 emits per the directive s5 row:
#   maturity_date  DATE
#   cik            (BDC filer identity — top-20 grouping key)
#   instrument_type  (s4-parsed, cleaned)
# The fused free-text identifier column from soi.tsv is `Investment, Identifier Axis`.
# DuckDB-on-Lance reads it via the bracketed-name quoting below.
_e2_check_inline() {
  local uri="$1" floor_pct="$2" top_n="$3" debtlike_regex="$4"
  local tmpfile
  tmpfile=$(mktemp -t e2_check.XXXXXX.py)
  cat >"$tmpfile" <<PYEOF
import duckdb, lance, os
ds = lance.dataset('$uri', storage_options={
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
})
con = duckdb.connect()
con.register('soi', ds.to_table())
# Top-N BDCs by investment-row count.
top = [r[0] for r in con.execute(
    "SELECT cik FROM soi WHERE cik IS NOT NULL "
    "GROUP BY cik ORDER BY COUNT(*) DESC LIMIT $top_n"
).fetchall()]
if not top:
    raise SystemExit('FAIL e2: no cik values in Lance dataset')
placeholders = ','.join(['?'] * len(top))
# Debt-like = fused identifier OR parsed instrument_type matches the predicate.
# COALESCE so a row counts as debt-like if EITHER column matches.
row = con.execute(
    f"""
    SELECT
      COUNT(*)                                                      AS debt_rows,
      COUNT(*) FILTER (WHERE maturity_date IS NOT NULL)              AS with_maturity
    FROM soi
    WHERE cik IN ({placeholders})
      AND (
        regexp_matches(COALESCE("Investment, Identifier Axis", ''), '$debtlike_regex')
        OR regexp_matches(COALESCE(instrument_type, ''), '$debtlike_regex')
      )
    """,
    top,
).fetchone()
debt_rows, with_maturity = row[0], row[1]
if debt_rows == 0:
    raise SystemExit('FAIL e2: zero debt-like rows across top-$top_n BDCs')
pct = 100.0 * with_maturity / debt_rows
print(f'e2: debt_rows={debt_rows} with_maturity={with_maturity} coverage={pct:.1f}%')
assert pct >= $floor_pct, f'e2 floor breach: {pct:.1f}% < $floor_pct% of debt-like rows'
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

# ── s1: ops.data_source_catalog row sec_bdc_soi + view UNION branch ────── #
# Per precedent sec-dera-fsds-ingest.sh, SQL string literals use '\''...'\''
# to survive the bash → eval → _dex_doppler → bash → psql expansion chain.
# Checks: (a) the catalog row exists; (b) the data_source_catalog_status view
# carries the new slug — a row for sec_bdc_soi appears in the view output.
run_surface "s1" "data-engine-x" '
  ROW=$(dex_psql_query "SELECT 1 FROM ops.data_source_catalog WHERE source_slug='\''sec_bdc_soi'\''" | tr -d "[:space:]") &&
  [[ "$ROW" = "1" ]] &&
  VIEW_ROW=$(dex_psql_query "SELECT 1 FROM ops.data_source_catalog_status WHERE source_slug='\''sec_bdc_soi'\''" | tr -d "[:space:]") &&
  [[ "$VIEW_ROW" = "1" ]]
'

# ── s2: ops.sec_bdc_soi_ingest_runs table + index ─────────────────────── #
# Mirrors ops.sec_dera_fsds_r2_ingest_runs verbatim (7-value status enum).
# Checks: table present; index idx_sec_bdc_soi_ingest_runs_release present;
# the 7-value status CHECK enum present (no_change required for skip-if-unchanged).
run_surface "s2" "data-engine-x" '
  TABLE_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_tables WHERE schemaname='\''ops'\'' AND tablename='\''sec_bdc_soi_ingest_runs'\''" | tr -d "[:space:]") &&
  [[ "$TABLE_EXISTS" = "1" ]] &&
  IDX_EXISTS=$(dex_psql_query "SELECT 1 FROM pg_indexes WHERE schemaname='\''ops'\'' AND indexname='\''idx_sec_bdc_soi_ingest_runs_release'\''" | tr -d "[:space:]") &&
  [[ "$IDX_EXISTS" = "1" ]] &&
  ENUM_OK=$(dex_psql_query "SELECT 1 FROM pg_constraint WHERE conrelid='\''ops.sec_bdc_soi_ingest_runs'\''::regclass AND contype='\''c'\'' AND pg_get_constraintdef(oid) LIKE '\''%no_change%'\''" | tr -d "[:space:]") &&
  [[ "$ENUM_OK" = "1" ]]
'

# ====================================================================== #
# Phase 2 — Code
# ====================================================================== #

# ── s3: scripts/run_sec_bdc_soi_r2_ingest.py ──────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - SEC host (BDC bulk-zip host)
#   - R2 prefix sec-bdc
#   - release= partition key
#   - ZSTD compression
#   - DuckDB read_csv all_varchar flag (no pinned column dict, L56)
#   - ContentType application/x-parquet (L42)
#   - audit ledger ops.sec_bdc_soi_ingest_runs
#   - period discovery (no hardcoded list) — tolerates {YYYY}q{N} and {YYYY}_{MM}
run_surface "s3" "data-engine-x" '
  F="$APP_DIR/scripts/run_sec_bdc_soi_r2_ingest.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "www.sec.gov"                       "$F" &&
  grep -q "sec-bdc"                            "$F" &&
  grep -q "release="                           "$F" &&
  grep -qE "all_varchar"                       "$F" &&
  grep -qE "ZSTD|zstd"                         "$F" &&
  grep -q "application/x-parquet"              "$F" &&
  grep -q "ops.sec_bdc_soi_ingest_runs"        "$F" &&
  ! grep -E "^[^#]*ContentEncoding"            "$F"
'

# ── s4: scripts/parse_sec_bdc_soi_html.py ─────────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - inlineurl (the soi.tsv column = PRIMARY HTML source; NOT txt.tsv —
#     validator §s4 source-path finding)
#   - the SOI TextBlock ix tag literal
#   - continuedAt (the ix continuation chain)
#   - R2 output prefix sec-bdc/soi-parsed
#   - maturity_date extraction
# Coverage itself (≥55% of debt-like rows) is measured by e2, not grepped here.
run_surface "s4" "data-engine-x" '
  F="$APP_DIR/scripts/parse_sec_bdc_soi_html.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "inlineurl"                                          "$F" &&
  grep -q "InvestmentHoldingsScheduleOfInvestments"            "$F" &&
  grep -q "continuedAt"                                        "$F" &&
  grep -q "sec-bdc/soi-parsed"                                 "$F" &&
  grep -q "maturity_date"                                      "$F"
'

# ── s5: scripts/run_sec_bdc_soi_lance_emit.py ─────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - Lance URI polaris-warehouse/sec_bdc/soi_lance
#   - lance_commit_lock (concurrent-emit guard)
#   - BTREE / create_scalar_index
#   - union_by_name (51↔213 column drift across periods)
#   - maturity_date typed sibling
#   - does NOT contain duckdb.typing. in uncommented code (runbook §Gotchas #2
#     negation check; ^[^#]* so an explanatory comment naming the gotcha does
#     not false-FAIL the surface — same comment-exclusion shape as s3's
#     ContentEncoding grep and the FSDS precedent)
run_surface "s5" "data-engine-x" '
  F="$APP_DIR/scripts/run_sec_bdc_soi_lance_emit.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "polaris-warehouse/sec_bdc/soi_lance"  "$F" &&
  grep -q "lance_commit_lock"                     "$F" &&
  grep -qE "BTREE|create_scalar_index"            "$F" &&
  grep -q "union_by_name"                         "$F" &&
  grep -q "maturity_date"                         "$F" &&
  ! grep -E "^[^#]*duckdb\.typing\."              "$F"
'

# ── s6: app/services/lance_views.py — sec_bdc_soi_lance_raw entry ──────── #
# Asserts the LanceView entry exists. The directive leaves register_at_boot to
# the audit: at ~450K-1M rows this dataset is in the "register_at_boot=False"
# band per the ARCHITECTURE-PATTERNS Arrow-bridge anti-pattern (every Lance
# dataset >~1M, and the borderline ones, set False; see lance_views.py — all
# recent multi-hundred-K datasets use False). Harness asserts BOTH the entry
# literal AND register_at_boot=False inside the sec_bdc_soi block.
run_surface "s6" "data-engine-x" '
  F="$APP_DIR/app/services/lance_views.py" &&
  grep -q "sec_bdc"   "$F" &&
  grep -q "soi_lance" "$F" &&
  python3 -c "
import re
src = open(\"$F\").read()
m = re.search(r\"name=[\\\"'\'']sec_bdc_soi_lance_raw[\\\"'\''].*?\\)\", src, re.DOTALL)
assert m, \"could not find LanceView block for sec_bdc_soi_lance_raw\"
assert \"register_at_boot=False\" in m.group(0), \"sec_bdc_soi_lance_raw must be register_at_boot=False\"
"
'

# ====================================================================== #
# Phase 3 — Polaris Generic Table (sec_bdc namespace — new this cycle)
# ====================================================================== #

# ── s7: Polaris generic-table sec_bdc.soi_lance registered ────────────── #
# --check-only exits 0 iff the generic table is registered (idempotent helper).
# Pre-deploy this FAILS (not yet registered) — that is correct; s7 runs as a
# discrete post-emit step (runbook §Gotchas #3: registration fails silently
# from inside Modal). Hard surface (not soft) — Polaris registration is an
# explicit cycle deliverable, gated strict.
run_surface "s7" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    cd '"$APP_DIR"' &&
    python3 scripts/init_polaris_lance_generic.py \
      --namespace '"$POLARIS_NAMESPACE"' --table '"$POLARIS_TABLE"' --check-only
  "
'

# ====================================================================== #
# Phase 4 — Modal
# ====================================================================== #

# ── s8: modal/sec_bdc_soi_app.py ──────────────────────────────────────── #
# Asserts: file exists, AST-parses, contains required invariants:
#   - Modal app name data-engine-x-sec-bdc-soi
#   - Cron( schedule
#   - both standard ingest secrets (dex-db DB + bulk-ingest-r2 R2)
#   - delegate refs to s3/s4/s5 (the three pipeline scripts)
run_surface "s8" "data-engine-x" '
  F="$APP_DIR/modal/sec_bdc_soi_app.py" &&
  test -f "$F" &&
  python3 -c "import ast; ast.parse(open(\"$F\").read())" &&
  grep -q "data-engine-x-sec-bdc-soi"  "$F" &&
  grep -qE "Cron\("                     "$F" &&
  grep -q "dex-db"            "$F" &&
  grep -q "bulk-ingest-r2"             "$F" &&
  grep -q "run_sec_bdc_soi_r2_ingest"  "$F" &&
  grep -q "parse_sec_bdc_soi_html"     "$F" &&
  grep -q "run_sec_bdc_soi_lance_emit" "$F"
'

# ── s9: Modal app data-engine-x-sec-bdc-soi deployed ──────────────────── #
# `.Description` and `.State` keys are capitalised in `modal app list --json`
# output (runbook §Gotchas #4); `.name`/`.state` fallbacks kept for older CLI.
run_surface "s9" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app list --json | jq -e \".[] | select(.Description==\\\"'"$MODAL_APP_NAME"'\\\" or .name==\\\"'"$MODAL_APP_NAME"'\\\") | select(.State==\\\"deployed\\\" or .state==\\\"deployed\\\")\" >/dev/null
  "
'

# ====================================================================== #
# Phase 5 — Verify-only (soft pre-deploy; STRICT=1 post-deploy)
# ====================================================================== #

# ── r1: R2 sec-bdc/ — each table prefix has ≥1 .parquet per period ────── #
# Checks soi/ AND txt/ each have ≥1 .parquet object (soi + txt are populated
# for every discovered period per directive r1). R2 cred-mapping
# AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID is required for `aws s3 ls` against R2.
run_soft_surface "r1" "data-engine-x" '
  doppler run --project hq-all --config prd -- bash -c "
    set -e
    SOI_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_SOI_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c '\''\.parquet'\'' || true)
    TXT_COUNT=\$(AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 ls s3://'"$R2_BUCKET/$R2_TXT_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT 2>/dev/null | grep -c '\''\.parquet'\'' || true)
    [[ \"\$SOI_COUNT\" -ge 1 && \"\$TXT_COUNT\" -ge 1 ]] || { echo \"r1 miss: soi=\$SOI_COUNT txt=\$TXT_COUNT (each must be >=1)\"; exit 1; }
    echo \"r1 ok: soi_parquets=\$SOI_COUNT txt_parquets=\$TXT_COUNT\"
  "
'

# ── e1: Lance sec_bdc/soi_lance — rows ≥ 450K, BTREE on adsh + maturity_date #
# Build the temp .py at surface-list time; harness eval runs `uv run python`
# from $APP_DIR so the project uv venv (pylance) resolves.
_E1_PY=$(_lance_check_inline "$LANCE_SOI_URI" "$FLOOR_E1" "adsh" "maturity_date"); _LANCE_CHECK_TMPFILES+=("$_E1_PY")
run_soft_surface "e1" "data-engine-x" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E1_PY'
"

# ── e2: Lance sec_bdc/soi_lance — maturity coverage ≥55% of debt-like rows ─ #
# DuckDB-on-Lance aggregate: top-20 BDCs by row count, debt-like predicate
# (lien|loan|note|bond|debt|term), maturity_date non-null fraction.
_E2_PY=$(_e2_check_inline "$LANCE_SOI_URI" "$E2_COVERAGE_FLOOR" "$E2_TOP_N_BDCS" "$E2_DEBTLIKE_REGEX"); _LANCE_CHECK_TMPFILES+=("$_E2_PY")
run_soft_surface "e2" "data-engine-x" "
  cd '$APP_DIR' && doppler run --project hq-all --config prd -- uv run python '$_E2_PY'
"

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
