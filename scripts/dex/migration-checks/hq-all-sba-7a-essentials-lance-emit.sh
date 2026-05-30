#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-sba-7a-essentials-lance-emit.
#
# Runs all F2P (FAIL_TO_PASS) and P2P (PASS_TO_PASS) checks per the frozen
# contract at ~/Desktop/hq/scope-status/hq-all-sba-7a-essentials-lance-emit/contract.md.
#
# Accepts:
#   --surface <id>    run a single surface (e.g. --surface s1)
#   --repo <name>     filter to one repo's surfaces (only hq-all in this cycle)
#
# Modeled after sba-bridges-to-lance.sh. Sources _lib-shim.sh for
# dex_psql_query and the _lance_floor_check + _polaris_lance_check helpers.
#
# Key validator corrections applied in this harness:
#   F2P-5: column is display_name (NOT source_name)
#   F2P-7: use ILIKE '%Mountain Mike%' (apostrophe + trailing-whitespace safe)
#   P2P-3: baseline is 123 rows; post-cycle expected 124

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# Prefer ~/hq-all (the current monorepo checkout); fall back to ~/Desktop/hq-all
# (legacy auxiliary clone, often stale).
for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

# --- CLI parsing ---------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (surface filter: ${SURFACE_FILTER:-all}; repo filter: ${REPO_FILTER:-all})"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"
    SKIP_COUNT=$((SKIP_COUNT+1)); return 0
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

# --- Lance row-count gate (shared helper) -------------------------------- #
# Usage: _lance_floor_check <lance_uri> <floor>
_lance_floor_check() {
  local uri="$1" floor="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 -c "
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
rows = ds.count_rows()
if rows >= $floor:
    print(f'PASS: $uri rows={rows:,} >= floor $floor')
    sys.exit(0)
print(f'FAIL: $uri rows={rows:,} < floor $floor')
sys.exit(1)
"
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with requests python "$HQ_ALL_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py" \
      --namespace "$ns" --table "$tbl" --check-only
}

# --- Helper: write a temp python file, run it via doppler, then rm it ---- #
# Usage: _run_py <tempfile_body>
# Writes body to /tmp/hq_verify_$$.py, runs it via doppler, removes it.
_run_py() {
  local tmpfile="/tmp/hq_verify_$$.py"
  cat > "$tmpfile" << 'PYEOF'
PYEOF
  # The caller writes the content directly to $tmpfile before calling this
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}

# ── s1 (F2P-1): Lance dataset emitted at the new path ─────────────────── #
# Contract §s1: opens successfully AND count_rows >= 1,900,000
run_surface "s1" "hq-all" \
  'test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sba_7a_loans_essentials_lance.py" &&
   _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/" 1900000'

# ── s2 (F2P-2): Row count ±1% vs source parquet ───────────────────────── #
# Contract §s2: abs(new_count - src_count) / src_count < 0.01
# Validator measured src_count = 1,947,098 on 2026-05-14.
_s2_check() {
  local tmpfile="/tmp/hq_s2_$$.py"
  cat > "$tmpfile" << 'PYEOF'
import os, sys, lance, duckdb

storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset(
    's3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/',
    storage_options=storage_options,
)
new_count = ds.count_rows()

ep = os.environ['R2_ENDPOINT']
account_id = ep.split('//')[-1].split('.')[0]
con = duckdb.connect()
con.execute('INSTALL httpfs; LOAD httpfs;')
con.execute(
    f"CREATE SECRET (TYPE r2, KEY_ID '{os.environ['R2_ACCESS_KEY_ID']}', "
    f"SECRET '{os.environ['R2_SECRET_ACCESS_KEY']}', ACCOUNT_ID '{account_id}');"
)
src_count = con.execute(
    "SELECT COUNT(*) FROM read_parquet("
    "'r2://dex-raw-landing-zone/sba/program=7a/decade=*/*.parquet',"
    " union_by_name=true, hive_partitioning=true)"
).fetchone()[0]

delta = abs(new_count - src_count) / src_count
print(f'new_count={new_count:,}  src_count={src_count:,}  delta={delta:.4%}')
if delta < 0.01:
    print(f'PASS: delta {delta:.4%} < 1%')
    sys.exit(0)
print(f'FAIL: delta {delta:.4%} >= 1%')
sys.exit(1)
PYEOF
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}
run_surface "s2" "hq-all" '_s2_check'

# ── s3 (F2P-3): Rich columns preserved (>= 45 cols, all required present) #
# Contract §s3 hard gate: >= 45 columns AND all 20 named columns present.
_s3_check() {
  local tmpfile="/tmp/hq_s3_$$.py"
  cat > "$tmpfile" << 'PYEOF'
import os, sys, lance

opts = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset(
    's3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/',
    storage_options=opts,
)
fields = {f.name for f in ds.schema}
required = {
    'approvaldate', 'bankfdicnumber', 'bankname', 'bankncuanumber',
    'borrcity', 'borrname', 'borrstate', 'borrstreet', 'borrzip',
    'businessage', 'businesstype', 'firstdisbursementdate',
    'franchisecode', 'franchisename', 'grossapproval', 'jobssupported',
    'loanstatus', 'naicscode', 'naicsdescription', 'sbaguaranteedapproval',
}
missing = required - fields
if missing:
    print(f'FAIL: missing columns: {missing}')
    sys.exit(1)
if len(fields) < 45:
    print(f'FAIL: too few columns: {len(fields)} (expected >= 45)')
    sys.exit(1)
print(f'PASS: {len(fields)} cols, all required present')
print(f'  columns: {sorted(fields)}')
PYEOF
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}
run_surface "s3" "hq-all" '_s3_check'

# ── s4 (F2P-4): Franchise records preserved — UPS Store seed ──────────── #
# Validator-pinned: 'The UPS Store' has 1133+ rows. Use ILIKE for mixed case.
_s4_check() {
  local tmpfile="/tmp/hq_s4_$$.py"
  cat > "$tmpfile" << 'PYEOF'
import os, sys, lance, duckdb

storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset(
    's3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/',
    storage_options=storage_options,
)
tbl = ds.scanner(columns=['borrname', 'franchisename', 'franchisecode']).to_table()
con = duckdb.connect()
con.register('tbl', tbl)
rows = con.execute(
    "SELECT borrname, franchisename FROM tbl "
    "WHERE franchisecode IS NOT NULL AND franchisename ILIKE '%UPS STORE%' LIMIT 10"
).fetchall()
print(f'UPS STORE franchise rows found: {len(rows)}')
for r in rows[:3]:
    print(f'  {r}')
if len(rows) >= 5:
    print('PASS: >= 5 UPS Store franchise rows found')
    sys.exit(0)
print(f'FAIL: expected >= 5 UPS Store franchise rows, got {len(rows)}')
sys.exit(1)
PYEOF
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}
run_surface "s4" "hq-all" '_s4_check'

# ── s5 (F2P-5): ops.data_sources registered ───────────────────────────── #
# Contract §s5: display_name='sba_7a_loans_essentials_lance', format='lance',
# status='active', owner_app='data-engine-x', storage_uri matches.
run_surface "s5" "hq-all" \
  'ACTIVE=$(dex_psql_query "SELECT 1 FROM ops.data_sources WHERE display_name='"'"'sba_7a_loans_essentials_lance'"'"' AND format='"'"'lance'"'"' AND status='"'"'active'"'"' AND owner_app='"'"'data-engine-x'"'"' AND storage_uri='"'"'s3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/'"'"'") &&
   test "$ACTIVE" = "1"'

# ── s6 (F2P-6): Trigger.dev refresh step appended ─────────────────────── #
# Contract §s6: new script file exists + present in _DAILY_SCRIPTS + referenced
# in sba-bridges-daily.ts comment block.
run_surface "s6" "hq-all" \
  'test -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/emit_sba_7a_loans_essentials_lance.py" &&
   grep -q "\"emit_sba_7a_loans_essentials_lance.py\"" "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
   grep -q "emit_sba_7a_loans_essentials_lance" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts"'

# ── s7 (F2P-7): Sample funnel-shape query — Mountain Mike (validator seed) #
# Contract §s7 corrected: franchisename ILIKE '%Mountain Mike%' (apostrophe-safe).
# Validator measured >= 5 CA COMMIT rows for decade=2020 alone.
_s7_check() {
  local tmpfile="/tmp/hq_s7_$$.py"
  cat > "$tmpfile" << 'PYEOF'
import os, sys, lance, duckdb

storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset(
    's3://dex-raw-landing-zone/polaris-warehouse/sba/7a_loans_essentials_lance/',
    storage_options=storage_options,
)
tbl = ds.scanner(
    columns=['borrname', 'borrstate', 'bankname', 'grossapproval', 'naicscode', 'franchisename', 'loanstatus']
).to_table()
con = duckdb.connect()
con.register('tbl', tbl)
rows = con.execute(
    "SELECT borrname, borrstate, bankname, grossapproval, naicscode, franchisename, loanstatus "
    "FROM tbl "
    "WHERE borrstate = 'CA' AND loanstatus = 'COMMIT' AND franchisename ILIKE '%Mountain Mike%' "
    "LIMIT 5"
).fetchall()
print(f'Mountain Mike CA COMMIT rows found: {len(rows)}')
for r in rows:
    print(f'  {r}')
if len(rows) >= 1:
    print('PASS: >= 1 Mountain Mike CA COMMIT row found')
    sys.exit(0)
print('FAIL: no Mountain Mike CA COMMIT rows found')
sys.exit(1)
PYEOF
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance --with duckdb python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}
run_surface "s7" "hq-all" '_s7_check'

# ── s8 (F2P-8 / Polaris): Polaris generic-table registered ────────────── #
# Contract §s8: init_polaris_lance_generic.py --check-only exits 0.
run_surface "s8" "hq-all" '_polaris_lance_check "sba" "7a_loans_essentials_lance"'

# ── p2p1 (P2P-1): Existing sba_loans_lance unchanged ─────────────────── #
# Contract §p2p1: row count EXACTLY 13,642,662; schema field count EXACTLY 19.
_p2p1_check() {
  local tmpfile="/tmp/hq_p2p1_$$.py"
  cat > "$tmpfile" << 'PYEOF'
import os, sys, lance

opts = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset(
    's3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/',
    storage_options=opts,
)
rows = ds.count_rows()
cols = len(ds.schema)
print(f'existing sba_loans_lance: rows={rows:,} schema_cols={cols}')
if rows != 13642662:
    print(f'FAIL: row count {rows:,} != 13,642,662')
    sys.exit(1)
if cols != 19:
    print(f'FAIL: schema cols {cols} != 19')
    sys.exit(1)
print('PASS: sba_loans_lance unchanged (13,642,662 rows, 19 cols)')
PYEOF
  doppler run --project hq-all --config prd -- \
    uv run --quiet --with pylance python3 "$tmpfile"
  local rc=$?
  rm -f "$tmpfile"
  return $rc
}
run_surface "p2p1" "hq-all" '_p2p1_check'

# ── p2p2 (P2P-2): Other 5 spot-check Lance datasets still queryable ────── #
# Contract §p2p2: spot-check 5 widely-used datasets.
run_surface "p2p2" "hq-all" \
  '_lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/" 8000000 &&
   _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sba/lenders_lance/" 4000 &&
   _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/gleif/lei_records_lance/" 1000000 &&
   _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/sam_gov/entities_lance/" 800000 &&
   _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/pdl/free_companies_lance/" 5000000'

# ── p2p3 (P2P-3): ops.data_sources delta exactly +1 ─────────────────── #
# Contract §p2p3: total count = 124 (was 123); existing SBA rows still 3.
run_surface "p2p3" "hq-all" \
  'POST_COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources") &&
   test "$POST_COUNT" = "124" &&
   EXISTING_SBA=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'sba_loans_lance'"'"','"'"'sba_borrowers_lance'"'"','"'"'sba_lenders_lance'"'"')") &&
   test "$EXISTING_SBA" = "3"'

# ── p2p4 (P2P-4): Existing sba-bridges-daily Trigger.dev task still works #
# Contract §p2p4: all 17 scripts present (16 original + 1 new);
# cron and task id unchanged.
run_surface "p2p4" "hq-all" \
  'for script in emit_sba_loans_lance.py emit_sba_borrowers_lance.py emit_sba_lenders_lance.py emit_sam_entities_lance.py build_bridge_pdl_sba_borrower.py build_bridge_sam_sba_borrower.py build_bridge_usaspending_sba_borrower.py emit_gleif_relationships_lance.py emit_gleif_with_parent_lance.py build_bridge_ucc_gleif_lance.py build_bridge_ucc_pdl_lance.py build_bridge_ucc_sba_lender_lance.py build_bridge_ucc_sba_borrower_lance.py build_bridge_sba_lender_gleif_lance.py build_bridge_sba_borrower_gleif_lance.py build_bridge_uspto_sba_capital_matching_lance.py emit_borrowers_ucc_profile_lance.py; do
     grep -q "\"$script\"" "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" || { echo "MISSING from _DAILY_SCRIPTS: $script"; exit 1; }
   done &&
   grep -q "\"emit_sba_7a_loans_essentials_lance.py\"" "$HQ_ALL_ROOT/apps/data-engine-x/app/routers/sba_bridges_internal_v1.py" &&
   grep -q "cron: CRON_DAILY_09_UTC" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts" &&
   grep -q "id: \"sba-bridges-daily\"" "$HQ_ALL_ROOT/apps/hq-x/src/trigger/sba-bridges-daily.ts"'

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
