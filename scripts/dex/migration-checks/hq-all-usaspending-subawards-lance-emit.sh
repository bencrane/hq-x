#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-usaspending-subawards-lance-emit.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-usaspending-subawards-lance-emit.md
# and the validator's stamped probe (2026-05-16 21:30Z):
#   - Pattern A pull-through x 2 datasets (contract + assistance subawards)
#     from CSV-bulk R2 parquet -> Lance + Polaris substrate.
#   - Floors: contract=15_191 (0.9 x 16,879), assistance=48_617 (0.9 x 54,019).
#   - BTREE x 4: prime_award_unique_key + subaward_number + subawardee_uei +
#     prime_awardee_uei. Validator confirmed all 4 present in BOTH feeds; no
#     synonym substitution.
#   - L47 modal `modal run --detach` mandatory (3600s timeout window).
#   - L49 TRY_CAST trap-VARCHARs at write-time (22 contract / 21 assistance).
#   - L50 ops.data_sources 5-col INSERT shape (NO `kind` column; PR #468/#469 precedent).
#   - Pattern A discipline: NO register_match_method*, NO register_bridge,
#     NO ops.bridges writes. This is a pull-through ingest, not a bridge.
#   - L21 N/A (no bridge or match-method registration).
#   - lance_commit_lock('contract_subawards_lance' | 'assistance_subawards_lance')
#     wraps each write; LANCE_BYPASS_SPILLING=true; compact_files + cleanup_old_versions.
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Mirrors hq-all-sam-pdl-usaspending-bridge-lance-emit.sh shape (direct
# precedent - PR #469). Differs in being Pattern A pull-through (not 3-way
# bridge), so we emit TWO scripts and TWO Lance datasets but ONE migration
# (2 INSERT rows) and TWO LanceView edits in a single lance_views.py file.
#
# Surface coverage (6 surfaces, single PR):
#   s1   code      scripts/run_usaspending_contract_subawards_lance_emit.py (Modal app)
#   s2   code      scripts/run_usaspending_assistance_subawards_lance_emit.py (Modal app)
#   s3   migration supabase/migrations/{ts}_usaspending_subawards_lance_data_sources.sql
#   s4   code      app/services/lance_views.py (2 new LanceView entries, register_at_boot=False)
#   s5   config    Polaris Generic Table registrations x 2
#   s6   deploy    Railway data-engine-x auto-redeploy
#
# Usage:
#   ./hq-all-usaspending-subawards-lance-emit.sh                # pre-deploy structural checks (s1-s4)
#   ./hq-all-usaspending-subawards-lance-emit.sh --surface s1   # single surface
#   POPULATED=1 ./hq-all-usaspending-subawards-lance-emit.sh    # incl. Lance row-count + BTREE gates (s1,s2) + DB row check (s3)
#   POPULATED=1 ./hq-all-usaspending-subawards-lance-emit.sh    # also runs Polaris GET (s5)
#   MERGE_SHA=<sha> ./hq-all-usaspending-subawards-lance-emit.sh # incl. deploy verify (s6)

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
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

# shellcheck disable=SC2034  # used inside eval'd run_surface single-quoted strings
DEX_APP="$HQ_ALL_ROOT/apps/data-engine-x"

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
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
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
\"
  "
}

# --- Lance BTREE index existence check (shared helper) ------------------ #
# Usage: _lance_btree_check <lance_uri> <column_name>
_lance_btree_check() {
  local uri="$1" col="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with pylance python3 -c \"
import os, sys, lance
storage_options = {
    'aws_endpoint': os.environ['R2_ENDPOINT'],
    'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'],
    'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'],
    'aws_region': 'us-east-1',
    'aws_virtual_hosted_style_request': 'false',
}
ds = lance.dataset('$uri', storage_options=storage_options)
indices = ds.list_indices()
btree_cols = []
for idx in indices:
    fields = idx.get('fields') if isinstance(idx, dict) else getattr(idx, 'fields', [])
    itype = idx.get('type') if isinstance(idx, dict) else getattr(idx, 'index_type', '')
    if 'BTREE' in str(itype).upper() or 'BTREE' in str(idx).upper():
        for f in (fields or []):
            btree_cols.append(str(f))
if '$col' in btree_cols:
    print(f'PASS: $uri has BTREE on $col')
    sys.exit(0)
print(f'FAIL: $uri missing BTREE on $col (saw indices: {indices})')
sys.exit(1)
\"
  "
}

# --- Polaris generic-table existence + format=lance check (shared) ------- #
# Usage: _polaris_lance_check <namespace> <table>
_polaris_lance_check() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- bash -c "
    uv run --quiet --with requests python3 \"$HQ_ALL_ROOT/apps/data-engine-x/scripts/init_polaris_lance_generic.py\" \
      --namespace $ns --table $tbl --check-only
  "
}

# ── PR-target sanity gate ──────────────────────────────────────────────── #
run_surface "p-remote" "hq-all" '
  if [[ ! -d "$HQ_ALL_ROOT/.git" ]]; then
    echo "   (no local hq-all checkout — skipping PR-target gate)"
    true
  else
    actual=$(cd "$HQ_ALL_ROOT" && git remote get-url origin)
    [[ "$actual" =~ bencrane/hq-all ]]
  fi
'

# ── s1: USAspending contract subawards Lance emit — Modal Pattern A ───── #
# Pattern A pull-through; reads r2://dex-raw-landing-zone/usaspending/
# contract_subawards/year=2026/data.parquet via DuckDB-on-R2 (httpfs + R2 SECRET),
# TRY_CASTs 22 trap VARCHARs at write-time, writes Lance to
# s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance.
# BTREE x 4: prime_award_unique_key + subaward_number + subawardee_uei + prime_awardee_uei.
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/run_usaspending_contract_subawards_lance_emit.py"
  test -f "$f" &&
  # Modal app shape — assert both decorator and constructor
  grep -qE "@app\.function" "$f" &&
  grep -qE "modal\.App" "$f" &&
  # Modal app name (data-engine-x-usaspending-contract-subawards-lance-emit)
  grep -qE "data-engine-x-usaspending-contract-subawards-lance-emit" "$f" &&
  # Modal resource floors per directive: memory=8192, timeout=3600, cpu=4
  grep -qE "memory[[:space:]]*=[[:space:]]*8192" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*3600" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*4" "$f" &&
  # Standard FUNCTION_SECRETS pair (bulk-ingest-r2 + dex-db)
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  # Input parquet URI (DuckDB scheme is r2://)
  grep -qE "r2://dex-raw-landing-zone/usaspending/contract_subawards/year=2026/data\.parquet" "$f" &&
  # Output Lance URI (Lance scheme is s3://)
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "$f" &&
  # Dataset slug for lance_commit_lock
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "contract_subawards_lance" "$f" &&
  # Floor literal must appear (15_191 = 0.9 x 16,879 measured)
  grep -qE "15[_,]?191" "$f" &&
  # 4 BTREE columns (validator-confirmed all present)
  grep -qE "create_scalar_index.*BTREE|index_type[[:space:]]*=[[:space:]]*[\"'"'"']BTREE[\"'"'"']" "$f" &&
  grep -qE "\"prime_award_unique_key\"|'"'"'prime_award_unique_key'"'"'" "$f" &&
  grep -qE "\"subaward_number\"|'"'"'subaward_number'"'"'" "$f" &&
  grep -qE "\"subawardee_uei\"|'"'"'subawardee_uei'"'"'" "$f" &&
  grep -qE "\"prime_awardee_uei\"|'"'"'prime_awardee_uei'"'"'" "$f" &&
  # L49 TRY_CAST trap VARCHARs (validator-confirmed 22 fields for contract feed).
  # Assert representative samples from each trap category.
  # — Dollars (DOUBLE):
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_total_outlayed_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subawardee_highly_compensated_officer_1_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  # — Dates (DATE):
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_base_action_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_period_of_performance_current_end_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_action_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_sam_report_last_modified_date[^)]*AS[[:space:]]+(DATE|TIMESTAMP)" &&
  # Contract-feed-only trap that distinguishes s1 from s2:
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_period_of_performance_potential_end_date[^)]*AS[[:space:]]+DATE" &&
  # — Fiscal year (INTEGER):
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_base_action_date_fiscal_year[^)]*AS[[:space:]]+(INTEGER|INT|BIGINT)" &&
  # Compact + cleanup discipline
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # DataFusion sort-spill bypass
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  # TMPDIR pinned to /tmp/lance
  grep -qE "/tmp/lance" "$f" &&
  # Detach launch documented (L47 — Modal CLI disconnect lesson)
  grep -qE "modal run --detach" "$f" &&
  # batch_size=100_000 per Pattern A precedent
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100_000|batch_size[[:space:]]*=[[:space:]]*100000" "$f" &&
  # Pattern A NEGATIVE asserts: no register_match_method*, no register_bridge call.
  ! grep -qE "register_match_method" "$f" &&
  ! grep -qE "register_bridge[[:space:]]*\\(" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\\.bridges" "$f" &&
  # @app.local_entrypoint() shape (def run, modal run --detach friendly)
  grep -qE "@app\.local_entrypoint" "$f" &&
  # POPULATED check: Lance dataset exists + row floor + 4 BTREE indices
  if [[ -n "${POPULATED:-}" ]]; then
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" 15191 &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "prime_award_unique_key" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "subaward_number" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "subawardee_uei" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "prime_awardee_uei"
  else
    echo "   (POPULATED unset — skipping Lance row-count + BTREE gates for s1)"
    true
  fi
'

# ── s2: USAspending assistance subawards Lance emit — Modal Pattern A ─── #
# Same shape as s1 with assistance-feed paths + 21 trap VARCHARs + floor 48_617.
run_surface "s2" "hq-all" '
  f="$DEX_APP/scripts/run_usaspending_assistance_subawards_lance_emit.py"
  test -f "$f" &&
  grep -qE "@app\.function" "$f" &&
  grep -qE "modal\.App" "$f" &&
  grep -qE "data-engine-x-usaspending-assistance-subawards-lance-emit" "$f" &&
  grep -qE "memory[[:space:]]*=[[:space:]]*8192" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*3600" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*4" "$f" &&
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  grep -qE "r2://dex-raw-landing-zone/usaspending/assistance_subawards/year=2026/data\.parquet" "$f" &&
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" "$f" &&
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "assistance_subawards_lance" "$f" &&
  grep -qE "48[_,]?617" "$f" &&
  grep -qE "create_scalar_index.*BTREE|index_type[[:space:]]*=[[:space:]]*[\"'"'"']BTREE[\"'"'"']" "$f" &&
  grep -qE "\"prime_award_unique_key\"|'"'"'prime_award_unique_key'"'"'" "$f" &&
  grep -qE "\"subaward_number\"|'"'"'subaward_number'"'"'" "$f" &&
  grep -qE "\"subawardee_uei\"|'"'"'subawardee_uei'"'"'" "$f" &&
  grep -qE "\"prime_awardee_uei\"|'"'"'prime_awardee_uei'"'"'" "$f" &&
  # L49 TRY_CAST representative samples (assistance feed — 21 trap VARCHARs):
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subawardee_highly_compensated_officer_1_amount[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_base_action_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_period_of_performance_current_end_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_action_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_sam_report_last_modified_date[^)]*AS[[:space:]]+(DATE|TIMESTAMP)" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*prime_award_base_action_date_fiscal_year[^)]*AS[[:space:]]+(INTEGER|INT|BIGINT)" &&
  # Negative: assistance feed does NOT have prime_award_period_of_performance_potential_end_date
  # (validator probe — that is contract-feed-only). Must NOT appear in s2.
  ! grep -qE "prime_award_period_of_performance_potential_end_date" "$f" &&
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  grep -qE "/tmp/lance" "$f" &&
  grep -qE "modal run --detach" "$f" &&
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100_000|batch_size[[:space:]]*=[[:space:]]*100000" "$f" &&
  ! grep -qE "register_match_method" "$f" &&
  ! grep -qE "register_bridge[[:space:]]*\\(" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\\.bridges" "$f" &&
  grep -qE "@app\.local_entrypoint" "$f" &&
  if [[ -n "${POPULATED:-}" ]]; then
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" 48617 &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" "prime_award_unique_key" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" "subaward_number" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" "subawardee_uei" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/assistance_subawards_lance" "prime_awardee_uei"
  else
    echo "   (POPULATED unset — skipping Lance row-count + BTREE gates for s2)"
    true
  fi
'

# ── s3: ops.data_sources migration (2 INSERT rows, 5-col shape) ──────── #
# Migration filename: {YYYYMMDDHHMMSS}_usaspending_subawards_lance_data_sources.sql
# L50: ops.data_sources is 5-col (display_name, storage_uri, format, status,
#      owner_app). NO `kind` column. INSERT 2 rows in a single VALUES list with
# ON CONFLICT (display_name) DO UPDATE for idempotency.
run_surface "s3" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_usaspending_subawards_lance_data_sources.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # Both display_names present
  grep -qE "contract_subawards_lance" "$mig" &&
  grep -qE "assistance_subawards_lance" "$mig" &&
  # Both storage_uri canonical paths
  grep -qE "polaris-warehouse/usaspending/contract_subawards_lance" "$mig" &&
  grep -qE "polaris-warehouse/usaspending/assistance_subawards_lance" "$mig" &&
  # format=lance, owner_app=data-engine-x, status=active
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  grep -qE "'"'"'data-engine-x'"'"'" "$mig" &&
  grep -qE "'"'"'active'"'"'" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO UPDATE
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # L50 NEGATIVE assert: NO `kind` column (precedent PR #468/#469 unanimous).
  ! grep -qE "(^|[[:space:](,])kind[[:space:]]*[=,)]" "$mig" &&
  # Pattern A discipline: NO ops.bridges, NO register_match_method*.
  ! grep -qE "ops\.bridges" "$mig" &&
  ! grep -qE "register_match_method" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_usaspending_subawards_lance_data_sources\.sql$"
'

# ── s3 (POPULATED): post-apply row count = 2 ─────────────────────────── #
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s3-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'contract_subawards_lance'"'"', '"'"'assistance_subawards_lance'"'"')")
    test "$COUNT" = "2"
  '
fi

# ── s4: lance_views.py — 2 new LanceView entries, register_at_boot=False ── #
# Per-key reads go through lance.dataset(uri).scanner(filter=...) directly.
run_surface "s4" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  grep -qE "usaspending_contract_subawards_lance_raw" "$f" &&
  grep -qE "usaspending_assistance_subawards_lance_raw" "$f" &&
  grep -qE "polaris-warehouse/usaspending/contract_subawards_lance" "$f" &&
  grep -qE "polaris-warehouse/usaspending/assistance_subawards_lance" "$f" &&
  # Boot ergonomics: register_at_boot=False adjacent to each new entry
  awk "/usaspending_contract_subawards_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  awk "/usaspending_assistance_subawards_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False"
'

# ── s5: Polaris Generic Tables — 2 invocations ────────────────────────── #
# usaspending.contract_subawards_lance + usaspending.assistance_subawards_lance.
# Pre-deploy: structural-only (no Polaris write happens until post-merge).
# POPULATED: GET-only check via init_polaris_lance_generic.py --check-only x 2.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s5-contract" "hq-all" '_polaris_lance_check "usaspending" "contract_subawards_lance"'
  run_surface "s5-assistance" "hq-all" '_polaris_lance_check "usaspending" "assistance_subawards_lance"'
else
  echo "-- s5 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET checks)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# ── s6: Railway data-engine-x deploy status + runtime probe ───────────── #
# THIS cycle does NOT add new endpoints — only register_at_boot=False LanceView
# entries (boot imports them but does not materialize); default runtime probe
# per apps/data-engine-x/CLAUDE.md §"Deploy verification".
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s6-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-usasp-subaw-sha.$$
      actual_sha=\$(cat /tmp/dex-usasp-subaw-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-usasp-subaw-sha.$$
      [[ -n \"\$actual_sha\" ]] || { echo \"FAIL: Railway returned no SUCCESS deployment SHA\" >&2; exit 1; }
      case \"\$actual_sha\" in
        $MERGE_SHA*) exit 0 ;;
        *)
          case \"$MERGE_SHA\" in
            \$actual_sha*) exit 0 ;;
          esac
          echo \"FAIL: Railway latest .meta.commitHash=\$actual_sha != \$MERGE_SHA\" >&2
          exit 1
          ;;
      esac
    "
  '
  run_surface "s6-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s6 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
