#!/usr/bin/env bash
# Verification harness for /scope cycle hq-all-contracts-with-subawards-lance-emit (Cycle B).
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC) from the directive at
# /Users/benjamincrane/Desktop/hq/directives/2026-05-16-hq-all-contracts-with-subawards-lance-emit.md
# and the validator's stamped probe (2026-05-16T22:50Z):
#   - Pattern A enriched-cohort emit at prime_award_unique_key grain.
#   - Reads usaspending.contracts_lance + usaspending.contract_subawards_lance.
#   - Writes bridges/contracts_with_subawards_lance (Lance) + 1 ops.data_sources row.
#   - Floor: 11,829,546 (0.9 × 13,143,940 distinct contract_award_unique_key per probe).
#   - BTREE x 3: prime_award_unique_key + prime_awardee_uei + prime_naics_code.
#   - Validator drift findings resolved by audit:
#       (1) HIGH — contracts_lance prime key is `contract_award_unique_key`; emit
#           aliases to `prime_award_unique_key` in output (matches subaward side).
#       (2) HIGH — 12 prime-side column-name aliases per Finding 2's mapping table.
#       (3) MEDIUM — `subaward_naics_array` DROPPED (degenerate, FSRS carries no
#           subaward NAICS; would always equal the prime's single-element NAICS).
#       (4) MEDIUM — Modal sizing bumped to memory=49152, timeout=14400 (PR #469
#           precedent; 13M-row write is 52× PR #469's output).
#       (5) MEDIUM — TRY_CAST only on contracts side (3 dates + 1 double);
#           subaward_amount/subaward_action_date are already typed.
#   - L47 modal `modal run --detach` mandatory.
#   - L50 ops.data_sources 5-col INSERT shape (NO `kind` column).
#   - Pattern A discipline: NO register_match_method*, NO register_bridge,
#     NO ops.bridges writes, NO ops.match_method_versions writes.
#
# Single-repo cycle: every surface lands in /Users/benjamincrane/hq-all.
# Mirrors hq-all-usaspending-subawards-lance-emit.sh shape (direct precedent — PR #471).
# Differs: ONE Modal app + ONE Lance dataset + ONE ops.data_sources row +
# ONE LanceView entry (vs the predecessor's two of each).
#
# Surface coverage (5 surfaces, single PR):
#   s1   code      scripts/build_bridge_contracts_with_subawards_lance.py (Modal app, Pattern A)
#   s2   migration supabase/migrations/{ts}_contracts_with_subawards_lance_data_source.sql
#   s3   code      app/services/lance_views.py (1 new LanceView entry, register_at_boot=False)
#   s4   config    Polaris Generic Table registration (bridges/contracts_with_subawards_lance)
#   s5   deploy    Railway data-engine-x auto-redeploy + runtime probe
#
# Usage:
#   ./hq-all-contracts-with-subawards-lance-emit.sh                # pre-deploy structural checks (s1-s3)
#   ./hq-all-contracts-with-subawards-lance-emit.sh --surface s1   # single surface
#   POPULATED=1 ./hq-all-contracts-with-subawards-lance-emit.sh    # incl. Lance row-count + BTREE gates (s1) + DB row check (s2) + Polaris GET (s4)
#   MERGE_SHA=<sha> ./hq-all-contracts-with-subawards-lance-emit.sh # incl. deploy verify (s5)

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

# ── s1: contracts × rolled-up subawards emit — Modal Pattern A (NEW) ──── #
# Pattern A enriched-cohort at prime_award_unique_key grain. Reads:
#   - usaspending.contracts_lance        (prime spine; 15.5M rows -> 13.1M distinct keys)
#   - usaspending.contract_subawards_lance (FSRS rollup; 16,879 rows -> 4,993 distinct primes)
# DuckDB-on-R2 (httpfs + R2 SECRET) for the heavier 15.5M-row GROUP BY pass.
# Writes Lance to s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance.
# BTREE × 3: prime_award_unique_key + prime_awardee_uei + prime_naics_code.
# TRY_CAST scope: prime-side ONLY (3 dates + 1 double per Finding 5).
run_surface "s1" "hq-all" '
  f="$DEX_APP/scripts/build_bridge_contracts_with_subawards_lance.py"
  test -f "$f" &&
  # Modal app shape — assert both decorator and constructor
  grep -qE "@app\.function" "$f" &&
  grep -qE "modal\.App" "$f" &&
  # Modal app name
  grep -qE "data-engine-x-contracts-with-subawards-lance-emit" "$f" &&
  # Modal resource floors per audit decision (validator-recommended bump):
  # memory=49152 (49 GiB; 13M-row Lance write + 15.5M GROUP BY headroom)
  # timeout=14400 (4h; mirrors PR #469 precedent)
  # cpu=8
  grep -qE "memory[[:space:]]*=[[:space:]]*49152" "$f" &&
  grep -qE "timeout[[:space:]]*=[[:space:]]*14400" "$f" &&
  grep -qE "cpu[[:space:]]*=[[:space:]]*8" "$f" &&
  # Standard FUNCTION_SECRETS pair
  grep -qE "bulk-ingest-r2" "$f" &&
  grep -qE "dex-db" "$f" &&
  # Input Lance URIs (both upstream datasets)
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contracts_lance" "$f" &&
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/contract_subawards_lance" "$f" &&
  # Output Lance URI
  grep -qE "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance" "$f" &&
  # Dataset slug for lance_commit_lock
  grep -qE "lance_commit_lock" "$f" &&
  grep -qE "contracts_with_subawards_lance" "$f" &&
  # Floor literal (11_829_546 = 0.9 × 13,143,940 validator-stamped)
  grep -qE "11[_,]?829[_,]?546" "$f" &&
  # 3 BTREE columns (audit-decided per validator Finding 5)
  grep -qE "create_scalar_index.*BTREE|index_type[[:space:]]*=[[:space:]]*[\"'"'"']BTREE[\"'"'"']" "$f" &&
  grep -qE "\"prime_award_unique_key\"|'"'"'prime_award_unique_key'"'"'" "$f" &&
  grep -qE "\"prime_awardee_uei\"|'"'"'prime_awardee_uei'"'"'" "$f" &&
  grep -qE "\"prime_naics_code\"|'"'"'prime_naics_code'"'"'" "$f" &&
  # Drift Finding 1 (HIGH): prime key column rename — emit MUST source from
  # contract_award_unique_key and alias to prime_award_unique_key.
  grep -qE "contract_award_unique_key" "$f" &&
  tr "\n" " " < "$f" | grep -qE "contract_award_unique_key[[:space:]]+AS[[:space:]]+prime_award_unique_key" &&
  # Drift Finding 2 (HIGH): the 12 prime-side aliases must each appear in the SQL.
  # Spot-check representative aliases per validator mapping table.
  tr "\n" " " < "$f" | grep -qE "recipient_uei[[:space:]]+AS[[:space:]]+prime_awardee_uei" &&
  tr "\n" " " < "$f" | grep -qE "recipient_name[[:space:]]+AS[[:space:]]+prime_awardee_name" &&
  tr "\n" " " < "$f" | grep -qE "naics_code[[:space:]]+AS[[:space:]]+prime_naics_code" &&
  tr "\n" " " < "$f" | grep -qE "naics_description[[:space:]]+AS[[:space:]]+prime_naics_description" &&
  tr "\n" " " < "$f" | grep -qE "awarding_agency_name[[:space:]]+AS[[:space:]]+prime_awarding_agency_name" &&
  tr "\n" " " < "$f" | grep -qE "awarding_sub_agency_name[[:space:]]+AS[[:space:]]+prime_awarding_sub_agency_name" &&
  tr "\n" " " < "$f" | grep -qE "primary_place_of_performance_state_code[[:space:]]+AS[[:space:]]+prime_place_of_performance_state_code" &&
  tr "\n" " " < "$f" | grep -qE "primary_place_of_performance_county_name[[:space:]]+AS[[:space:]]+prime_place_of_performance_county_name" &&
  # Drift Finding 5 (MEDIUM): TRY_CAST scope confined to contracts side
  # (3 dates + 1 double). Spot-check.
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*period_of_performance_start_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*period_of_performance_current_end_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*action_date[^)]*AS[[:space:]]+DATE" &&
  tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*total_dollars_obligated[^)]*AS[[:space:]]+(DOUBLE|DECIMAL)" &&
  # Drift Finding 5 NEGATIVE: subaward_amount/subaward_action_date are already
  # typed (double, date32) — directive §28/§29 wraps were wrong. Audit removed.
  # Must NOT wrap typed subaward cols in TRY_CAST.
  ! tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_amount[^)]*AS" &&
  ! tr "\n" " " < "$f" | grep -qE "TRY_CAST[(][^)]*subaward_action_date[^)]*AS" &&
  # Drift Finding 3 (MEDIUM): subaward_naics_array DROPPED — must NOT appear
  # as a SQL column reference or alias. Docstring + comment mentions are OK
  # (recommended for human readability). Tighten the negative grep to require
  # SQL-shape context: `AS subaward_naics_array`, `subaward_naics_array,`, or
  # `subaward_naics_array)` — any of which would be a real column reference.
  ! tr "\n" " " < "$f" | grep -qE "(AS[[:space:]]+subaward_naics_array|subaward_naics_array[[:space:]]*[,)])" &&
  ! tr "\n" " " < "$f" | grep -qE "(AS[[:space:]]+prime_award_naics_code|prime_award_naics_code[[:space:]]*[,)])" &&
  # Subaward rollup: confirm SUM/COUNT/MIN/MAX/array_agg shapes do appear
  grep -qE "subaward_count" "$f" &&
  grep -qE "total_subaward_amount" "$f" &&
  grep -qE "earliest_subaward_action_date" "$f" &&
  grep -qE "latest_subaward_action_date" "$f" &&
  grep -qE "subawardee_uei_array" "$f" &&
  grep -qE "subawardee_name_array" "$f" &&
  grep -qE "subaward_state_array" "$f" &&
  # Provenance triplet
  grep -qE "bridge_run_id" "$f" &&
  grep -qE "generated_at" "$f" &&
  grep -qE "bridge_version" "$f" &&
  # DuckDB DISTINCT-ON-equivalent: ROW_NUMBER OVER PARTITION BY + WHERE rn=1.
  # (Note: DuckDB *does* support Postgres-style DISTINCT ON since v0.9; the
  # audit-frozen pattern uses ROW_NUMBER for functional equivalence.)
  grep -qE "ROW_NUMBER[[:space:]]*\\(\\)" "$f" &&
  grep -qE "PARTITION BY[[:space:]]+contract_award_unique_key" "$f" &&
  # Lance access pattern (reviewer-locked, 2026-05-16T23:30Z):
  # PR #469 precedent — open via lance.dataset(URI).scanner(columns=[...])
  # .to_table(), then con.register(). DuckDB read_parquet() against Lance
  # URIs would match ZERO files (Lance stores fragments as `.lance`, NOT
  # `.parquet`). NEGATIVE assert on read_parquet over the two upstream
  # Lance URIs blocks accidental fallback to the wrong access shape.
  grep -qE "lance\.dataset\([[:space:]]*CONTRACTS_LANCE_URI" "$f" &&
  grep -qE "lance\.dataset\([[:space:]]*CONTRACT_SUBAWARDS_LANCE_URI" "$f" &&
  grep -qE "contracts_ds\.scanner\(" "$f" &&
  grep -qE "subawards_ds\.scanner\(" "$f" &&
  grep -qE "con\.register\([[:space:]]*[\"'"'"']contracts_proj[\"'"'"']" "$f" &&
  grep -qE "con\.register\([[:space:]]*[\"'"'"']subawards_proj[\"'"'"']" "$f" &&
  # NEGATIVE: must NOT read_parquet either upstream Lance URI.
  ! grep -qE "read_parquet\([^)]*usaspending/contracts_lance" "$f" &&
  ! grep -qE "read_parquet\([^)]*usaspending/contract_subawards_lance" "$f" &&
  # Compact + cleanup discipline
  grep -qE "compact_files" "$f" &&
  grep -qE "cleanup_old_versions" "$f" &&
  # DataFusion sort-spill bypass
  grep -qE "LANCE_BYPASS_SPILLING" "$f" &&
  # TMPDIR pinned to /tmp/lance
  grep -qE "/tmp/lance" "$f" &&
  # Detach launch documented (L47)
  grep -qE "modal run --detach" "$f" &&
  # batch_size=100_000 per Pattern A precedent
  grep -qE "batch_size[[:space:]]*=[[:space:]]*100_000|batch_size[[:space:]]*=[[:space:]]*100000" "$f" &&
  # Pattern A NEGATIVE asserts. Tighten `register_match_method` to require a
  # function-call (`(`) or import context — docstring + comment mentions like
  # "NO register_match_method" are intentional human documentation and pass.
  ! grep -qE "register_match_method[a-z_]*[[:space:]]*\\(" "$f" &&
  ! grep -qE "from[[:space:]]+.*[[:space:]]+import[[:space:]]+.*register_match_method" "$f" &&
  ! grep -qE "register_bridge[[:space:]]*\\(" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\\.bridges" "$f" &&
  ! grep -qE "INSERT[[:space:]]+INTO[[:space:]]+ops\\.match_method_versions" "$f" &&
  # @app.local_entrypoint() shape (def run, modal run --detach friendly)
  grep -qE "@app\.local_entrypoint" "$f" &&
  # POPULATED check: Lance dataset exists + row floor + 3 BTREE indices
  if [[ -n "${POPULATED:-}" ]]; then
    _lance_floor_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance" 11829546 &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance" "prime_award_unique_key" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance" "prime_awardee_uei" &&
    _lance_btree_check "s3://dex-raw-landing-zone/polaris-warehouse/bridges/contracts_with_subawards_lance" "prime_naics_code"
  else
    echo "   (POPULATED unset — skipping Lance row-count + BTREE gates for s1)"
    true
  fi
'

# ── s2: ops.data_sources migration (1 INSERT row, 5-col shape) ──────────── #
# Migration filename: {YYYYMMDDHHMMSS}_contracts_with_subawards_lance_data_source.sql
# L50: 5-col shape, NO `kind` column. Single row.
run_surface "s2" "hq-all" '
  mig=$(ls "$DEX_APP/supabase/migrations/"*_contracts_with_subawards_lance_data_source.sql 2>/dev/null | head -1)
  test -n "$mig" -a -f "$mig" &&
  # display_name + storage_uri present
  grep -qE "contracts_with_subawards_lance" "$mig" &&
  grep -qE "polaris-warehouse/bridges/contracts_with_subawards_lance" "$mig" &&
  # format=lance, owner_app=data-engine-x, status=active
  grep -qE "'"'"'lance'"'"'" "$mig" &&
  grep -qE "'"'"'data-engine-x'"'"'" "$mig" &&
  grep -qE "'"'"'active'"'"'" "$mig" &&
  # Idempotency: ON CONFLICT (display_name) DO UPDATE
  grep -qE "ON CONFLICT \(display_name\) DO (NOTHING|UPDATE)" "$mig" &&
  # L50 NEGATIVE assert: NO `kind` column
  ! grep -qE "(^|[[:space:](,])kind[[:space:]]*[=,)]" "$mig" &&
  # Pattern A discipline: NO ops.bridges, NO register_match_method*
  ! grep -qE "ops\.bridges" "$mig" &&
  ! grep -qE "register_match_method" "$mig" &&
  # Timestamp prefix (UTC, 14 digits)
  basename_=$(basename "$mig") &&
  echo "$basename_" | grep -qE "^[0-9]{14}_contracts_with_subawards_lance_data_source\.sql$"
'

# ── s2 (POPULATED): post-apply row count = 1 ─────────────────────────── #
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s2-applied" "hq-all" '
    COUNT=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name = '"'"'contracts_with_subawards_lance'"'"'")
    test "$COUNT" = "1"
  '
fi

# ── s3: lance_views.py — 1 new LanceView entry, register_at_boot=False ── #
# Per-key reads go through lance.dataset(uri).scanner(filter=...) directly.
# Naming convention matches PR #469/#471: <slug>_raw suffix.
run_surface "s3" "hq-all" '
  f="$DEX_APP/app/services/lance_views.py"
  test -f "$f" &&
  grep -qE "contracts_with_subawards_lance_raw" "$f" &&
  grep -qE "polaris-warehouse/bridges/contracts_with_subawards_lance" "$f" &&
  # Boot ergonomics: register_at_boot=False adjacent to the new entry
  awk "/contracts_with_subawards_lance_raw/,/register_at_boot=False/" "$f" | grep -qE "register_at_boot=False" &&
  # Exactly one entry (avoid accidental duplication on rebase)
  test "$(grep -c '"'"'contracts_with_subawards_lance_raw'"'"' "$f")" = "1"
'

# ── s4: Polaris Generic Table registration ──────────────────────────── #
# Single namespace+table: bridges.contracts_with_subawards_lance.
# Pre-deploy: structural-only (no Polaris write happens until post-merge).
# POPULATED: GET-only check via init_polaris_lance_generic.py --check-only.
if [[ -n "${POPULATED:-}" ]]; then
  run_surface "s4" "hq-all" '_polaris_lance_check "bridges" "contracts_with_subawards_lance"'
else
  echo "-- s4 (hq-all): SKIPPED (set POPULATED=1 to run Polaris GET check)"
  SKIP_COUNT=$((SKIP_COUNT+1))
fi

# ── s5: Railway data-engine-x deploy status + runtime probe ───────────── #
# THIS cycle does NOT add new endpoints — only a register_at_boot=False LanceView
# entry (boot imports it but does not materialize); default runtime probe per
# apps/data-engine-x/CLAUDE.md §"Deploy verification".
if [[ -n "${MERGE_SHA:-}" ]]; then
  run_surface "s5-deploy-status" "hq-all" '
    doppler run --project hq-all --config prd -- bash -c "
      railway deployment list --service data-engine-x --limit 1 --json | jq -e -r \"
        .[0] | select(.status==\\\"SUCCESS\\\") | .meta.commitHash
      \" > /tmp/dex-contracts-subaw-sha.$$
      actual_sha=\$(cat /tmp/dex-contracts-subaw-sha.$$ | head -1 | tr -d \" \\n\")
      rm -f /tmp/dex-contracts-subaw-sha.$$
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
  run_surface "s5-runtime-probe" "hq-all" '
    source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/deploy_verify.sh"
    verify_service_runtime data-engine-x "https://api.dataengine.run"
  '
else
  echo "-- s5 (hq-all): SKIPPED (set MERGE_SHA to run deploy verify)"
  SKIP_COUNT=$((SKIP_COUNT+2))
fi

# --- summary ------------------------------------------------------------- #
echo ""
echo "==> Summary: PASS=$PASS_COUNT FAIL=$FAIL_COUNT SKIP=$SKIP_COUNT"
if (( FAIL_COUNT > 0 )); then
  exit 1
fi
echo "All requested surfaces verified."
