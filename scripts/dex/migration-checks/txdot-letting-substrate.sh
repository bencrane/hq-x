#!/usr/bin/env bash
# txdot-letting-substrate.sh — verification harness for the
# txdot-letting-substrate cycle (8 surfaces).
#
# Migration cycle: TXDOT letting bid-tabulation history → R2 → Lance + Polaris
#                  txstate.txdot_letting_lance + bridges.sba_txdot_winners_lance
# Directive:       /Users/benjamincrane/Desktop/hq/directives/2026-05-18-hq-all-txdot-letting-substrate.md
# Contract:        ~/Desktop/hq/scope-status/txdot-letting-substrate/contract.md
# Predecessor:     none (first cycle of txstate substrate)
#
# Run AFTER PR merge AND, for s7+s8, AFTER the operator has run the post-merge
# config commands (see directive §"## Audit plan" surfaces s7, s8).
#
# Flags:
#   --surface s<N>   run a single surface only (s1 .. s8)
#   --repo <name>    filter by repo (only hq-all here; no-op)
#
# Exit 0 iff all requested surfaces verified PASS. Exit 1 on any FAIL.

set -u
set -o pipefail

SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Source canonical helper library (resolves to apps/data-engine-x/scripts/_lib/dex.sh).
# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="${HOME}/hq-all"
DEX_ROOT="${REPO_ROOT}/apps/data-engine-x"
HQX_ROOT="${REPO_ROOT}/apps/hq-x"

FAILURES=()

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAILURES+=("$1"); }

should_run() {
  local id="$1" repo="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 1; fi
  if [[ -n "$REPO_FILTER"    && "$REPO_FILTER"    != "$repo" ]]; then return 1; fi
  return 0
}

echo "==> Verifying txdot-letting-substrate (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

# ----------------------------------------------------------------------
# s1 — migration: ops.txdot_letting_r2_ingest_runs + ops.data_sources rows
# ----------------------------------------------------------------------
if should_run "s1" "hq-all"; then
  echo "=== s1: migration — ops.txdot_letting_r2_ingest_runs + ops.data_sources ==="

  MIG_FILE=$(ls -1 "${DEX_ROOT}"/supabase/migrations/2026*_txdot_letting_observability.sql 2>/dev/null | head -n 1 || true)
  if [[ -n "${MIG_FILE}" && -f "${MIG_FILE}" ]]; then
    pass "migration file exists: ${MIG_FILE}"

    # Positive greps.
    for needle in \
        'CREATE TABLE IF NOT EXISTS ops.txdot_letting_r2_ingest_runs' \
        "'pending', 'running', 'completed', 'failed'" \
        "'no_change', 'skipped', 'dry_run'" \
        'INSERT INTO ops.data_sources' \
        "'txstate.txdot_letting_lance'" \
        "'bridges.sba_txdot_winners_lance'" \
        'ON CONFLICT (display_name)' \
        "'lance'" \
        "'active'" \
        "'data-engine-x'" \
        'CREATE INDEX IF NOT EXISTS' ; do
      if grep -Fq "${needle}" "${MIG_FILE}" 2>/dev/null; then
        pass "migration contains: ${needle}"
      else
        fail "migration missing: ${needle}"
      fi
    done

    # Negative greps — L50 (no `kind` column), no Volume-King source_* table, no _down.
    for forbidden in 'kind text' 'entities.source_txdot' 'DROP TABLE' '_down.sql' ; do
      if grep -Fq "${forbidden}" "${MIG_FILE}" 2>/dev/null; then
        fail "migration FORBIDDEN literal: ${forbidden}"
      else
        pass "migration negative-assert: '${forbidden}' absent"
      fi
    done
  else
    fail "migration file missing — expected apps/data-engine-x/supabase/migrations/2026*_txdot_letting_observability.sql"
  fi

  # Applied-state probe (only meaningful after apply_pending_migrations.sh has run).
  # Uses canonical dex_psql_query helper sourced via _lib-shim.sh (DEX_DOPPLER_PROJECT=hq-all).
  if command -v doppler >/dev/null 2>&1; then
    table_exists=$(dex_psql_query "SELECT to_regclass('ops.txdot_letting_r2_ingest_runs')::text" 2>/dev/null | tr -d '[:space:]')
    if [[ "${table_exists}" == "ops.txdot_letting_r2_ingest_runs" ]]; then
      pass "applied: ops.txdot_letting_r2_ingest_runs EXISTS in DEX_DB_URL_POOLED"
    else
      fail "applied: ops.txdot_letting_r2_ingest_runs DOES NOT EXIST (got '${table_exists}')"
    fi

    ds_count=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('txstate.txdot_letting_lance', 'bridges.sba_txdot_winners_lance')" 2>/dev/null | tr -d '[:space:]')
    if [[ "${ds_count}" == "2" ]]; then
      pass "applied: ops.data_sources has 2 expected rows"
    else
      fail "applied: ops.data_sources expected 2 rows, got '${ds_count}'"
    fi
  else
    echo "  (advisory) doppler not on PATH — skipping applied-state probe"
  fi
fi

# ----------------------------------------------------------------------
# s2 — code: Modal app txdot_letting_ingest_app.py
# ----------------------------------------------------------------------
if should_run "s2" "hq-all"; then
  echo "=== s2: code — apps/data-engine-x/modal/txdot_letting_ingest_app.py ==="
  S2_PATH="${DEX_ROOT}/modal/txdot_letting_ingest_app.py"

  if [[ -f "${S2_PATH}" ]]; then
    pass "script exists: ${S2_PATH}"

    for needle in \
        'modal.App("data-engine-x-txdot-letting-ingest")' \
        'def ingest_socrata_to_r2' \
        'de7b-7dna' \
        'rowsUpdatedAt' \
        'application/x-parquet' \
        'read_csv_auto' \
        'all_varchar=TRUE' \
        'WINDOWS-1252' \
        'memory=8192' \
        'txdot_letting_r2_ingest_runs' \
        'snapshot=' \
        'COMPRESSION ZSTD' ; do
      if grep -Fq "${needle}" "${S2_PATH}" 2>/dev/null; then
        pass "s2 contains: ${needle}"
      else
        fail "s2 missing: ${needle}"
      fi
    done

    # L42 negative-assert — BLOCKING.
    if grep -Fq 'ContentEncoding' "${S2_PATH}" 2>/dev/null; then
      fail "s2 FORBIDDEN literal: ContentEncoding (per L42 — R2 hook will reject)"
    else
      pass "s2 negative-assert: ContentEncoding absent (L42 compliant)"
    fi

    # L42 filename negative-assert.
    if grep -Fq '.parquet.zst' "${S2_PATH}" 2>/dev/null; then
      fail "s2 FORBIDDEN literal: .parquet.zst (per L42 — plain .parquet only)"
    else
      pass "s2 negative-assert: .parquet.zst absent (L42 compliant)"
    fi
  else
    fail "s2 missing: ${S2_PATH}"
  fi
fi

# ----------------------------------------------------------------------
# s3 — code: Pattern A Lance emit
# ----------------------------------------------------------------------
if should_run "s3" "hq-all"; then
  echo "=== s3: code — apps/data-engine-x/scripts/run_txdot_letting_lance_emit.py ==="
  S3_PATH="${DEX_ROOT}/scripts/run_txdot_letting_lance_emit.py"

  if [[ -f "${S3_PATH}" ]]; then
    pass "script exists: ${S3_PATH}"

    for needle in \
        'modal.App("data-engine-x-txdot-letting-lance-emit")' \
        'txdot_letting_lance' \
        'lance_commit_lock' \
        'py_normalize_entity' \
        'normalize_entity_name' \
        'controlling_project_id_ccsj' \
        'vendor_name_normalized' \
        'project_actual_let_date' \
        '900_000' \
        'LANCE_BYPASS_SPILLING' \
        'create_scalar_index' \
        'BTREE' \
        'all_varchar=TRUE' \
        'TRY_CAST' ; do
      if grep -Fq "${needle}" "${S3_PATH}" 2>/dev/null; then
        pass "s3 contains: ${needle}"
      else
        fail "s3 missing: ${needle}"
      fi
    done

    # L54 negative-assert.
    if grep -Fq 'LIST<VARCHAR>' "${S3_PATH}" 2>/dev/null; then
      fail "s3 FORBIDDEN literal: LIST<VARCHAR> (per L54 — use pipe-delimited VARCHAR)"
    else
      pass "s3 negative-assert: LIST<VARCHAR> absent (L54 compliant)"
    fi

    # register_at_boot=True must NOT be near the txdot view (s6 is lazy).
    if grep -B1 -A2 'txdot_letting_raw' "${DEX_ROOT}/app/services/lance_views.py" 2>/dev/null | grep -Fq 'register_at_boot=True'; then
      fail "s3+s6: LanceView for txdot_letting_raw has register_at_boot=True (must be False — 1M+ rows)"
    else
      pass "s3+s6 negative-assert: txdot_letting_raw LanceView is register_at_boot=False"
    fi
  else
    fail "s3 missing: ${S3_PATH}"
  fi
fi

# ----------------------------------------------------------------------
# s4 — code: Pattern B bridge generator
# ----------------------------------------------------------------------
if should_run "s4" "hq-all"; then
  echo "=== s4: code — apps/data-engine-x/scripts/build_bridge_sba_txdot_winners_lance.py ==="
  S4_PATH="${DEX_ROOT}/scripts/build_bridge_sba_txdot_winners_lance.py"

  if [[ -f "${S4_PATH}" ]]; then
    pass "script exists: ${S4_PATH}"

    for needle in \
        'BRIDGE_NAME = "sba_txdot_winners"' \
        'DATASET_SLUG = "sba_txdot_winners_lance"' \
        'METHOD_NAME = "legal_name_state_exact_tx"' \
        'METHOD_SEMVER = "1.0.0"' \
        'COLLISION_THRESHOLD = 50' \
        'MIN_ROWS_MATCHED = 25' \
        'lance_commit_lock' \
        'borrstate' \
        'low_bidder_flag' \
        'vendor_name_normalized' \
        'register_match_method(' \
        'register_match_method_version(' \
        'register_bridge(' \
        'start_bridge_run(' \
        'complete_bridge_run(' \
        'fail_bridge_run(' \
        'modal.App("data-engine-x-sba-txdot-winners-lance")' \
        'create_scalar_index' ; do
      if grep -Fq "${needle}" "${S4_PATH}" 2>/dev/null; then
        pass "s4 contains: ${needle}"
      else
        fail "s4 missing: ${needle}"
      fi
    done

    # L21 STRICT negative-assert — no _ca or _fl method-name strings (copy-paste guard).
    if grep -qE "'legal_name_state_exact_ca'|'legal_name_state_exact_fl'" "${S4_PATH}" 2>/dev/null; then
      fail "s4 FORBIDDEN: legal_name_state_exact_ca / _fl appears as a quoted string (L21 STRICT — copy-paste guard)"
    else
      pass "s4 negative-assert: legal_name_state_exact_ca / _fl absent as quoted strings (L21 STRICT compliant)"
    fi

    # Modal kwarg shape (contract §17 B.a) — NOT argparse.
    if grep -qE "^import argparse|^from argparse" "${S4_PATH}" 2>/dev/null; then
      fail "s4 FORBIDDEN: argparse import (use Modal local_entrypoint kwarg shape per contract §17 B.a)"
    else
      pass "s4 negative-assert: argparse absent (Modal local_entrypoint kwarg shape)"
    fi

    if grep -qE "def main\(.*apply" "${S4_PATH}" 2>/dev/null; then
      pass "s4 local_entrypoint signature 'def main(...apply...)' present (contract §17 B.a)"
    else
      fail "s4 local_entrypoint must be 'def main(apply: bool = False)' per contract §17 B.a"
    fi
  else
    fail "s4 missing: ${S4_PATH}"
  fi
fi

# ----------------------------------------------------------------------
# s5 — code: Trigger.dev monthly cron
# ----------------------------------------------------------------------
if should_run "s5" "hq-all"; then
  echo "=== s5: code — apps/hq-x/src/trigger/txdot-letting-monthly.ts ==="
  S5_PATH="${HQX_ROOT}/src/trigger/txdot-letting-monthly.ts"

  if [[ -f "${S5_PATH}" ]]; then
    pass "script exists: ${S5_PATH}"

    for needle in \
        'id: "txdot-letting.monthly"' \
        'cron: "0 8 1 * *"' \
        'data-engine-x-txdot-letting-ingest' \
        'ingest_socrata_to_r2' \
        'MODAL_TOKEN_ID' \
        'MODAL_TOKEN_SECRET' \
        'schedules.task' ; do
      if grep -Fq "${needle}" "${S5_PATH}" 2>/dev/null; then
        pass "s5 contains: ${needle}"
      else
        fail "s5 missing: ${needle}"
      fi
    done

    # Auth-model negative-assert (per DEX CLAUDE.md §"Auth model").
    if grep -Fq 'getSystemM2MToken' "${S5_PATH}" 2>/dev/null; then
      fail "s5 FORBIDDEN: getSystemM2MToken (deprecated boundary per DEX CLAUDE.md §'Auth model')"
    else
      pass "s5 negative-assert: getSystemM2MToken absent (auth model compliant)"
    fi
  else
    fail "s5 missing: ${S5_PATH}"
  fi

  # Confirm hq-x trigger project id is canonical (validator already confirmed).
  CFG="${HQX_ROOT}/trigger.config.ts"
  if [[ -f "${CFG}" ]] && grep -q 'proj_khmvxxrpyloqmnivdetu' "${CFG}"; then
    pass "s5 hq-x trigger.config.ts pinned to proj_khmvxxrpyloqmnivdetu"
  else
    fail "s5 hq-x trigger.config.ts missing or wrong project id (expected proj_khmvxxrpyloqmnivdetu)"
  fi
fi

# ----------------------------------------------------------------------
# s6 — code: LanceView entry append
# ----------------------------------------------------------------------
if should_run "s6" "hq-all"; then
  echo "=== s6: code — apps/data-engine-x/app/services/lance_views.py ==="
  S6_PATH="${DEX_ROOT}/app/services/lance_views.py"

  if [[ -f "${S6_PATH}" ]]; then
    pass "lance_views.py exists"

    if grep -Fq 'name="txdot_letting_raw"' "${S6_PATH}" 2>/dev/null; then
      pass "s6 contains: name=\"txdot_letting_raw\""
    else
      fail "s6 missing: name=\"txdot_letting_raw\""
    fi

    if grep -Fq 's3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance' "${S6_PATH}" 2>/dev/null; then
      pass "s6 contains: txstate Lance URI"
    else
      fail "s6 missing: txstate Lance URI"
    fi

    # The new LanceView entry MUST have register_at_boot=False.
    if grep -B1 -A4 'txdot_letting_raw' "${S6_PATH}" 2>/dev/null | grep -Fq 'register_at_boot=False'; then
      pass "s6 txdot_letting_raw entry uses register_at_boot=False (1M+ row laziness)"
    else
      fail "s6 txdot_letting_raw entry missing register_at_boot=False"
    fi
  else
    fail "s6 missing: ${S6_PATH}"
  fi
fi

# ----------------------------------------------------------------------
# s7 — config: R2 + Lance + Polaris first-run (post-merge state)
# ----------------------------------------------------------------------
if should_run "s7" "hq-all"; then
  echo "=== s7: runtime state — R2 object + Lance dataset + Polaris registration ==="

  # 7a — R2 object present. Wrap in `uv run --with boto3` since system python3
  # lacks boto3 (and pylance for 7b/8a). uv resolves on first run, caches after.
  echo "  (probing R2 head_object for snapshot Parquet) ..."
  r2_present=$(cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- uv run --with boto3 python3 -c "
import os, boto3, sys
try:
    s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'],
        aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
        region_name='auto')
    snapshots = s3.list_objects_v2(Bucket='dex-raw-landing-zone', Prefix='txstate/txdot_letting/snapshot=').get('Contents', [])
    print('present' if snapshots else 'missing')
except Exception as e:
    print(f'error: {e}')
" 2>&1 | tail -1)
  if [[ "${r2_present}" == "present" ]]; then
    pass "s7 R2 snapshot Parquet present under txstate/txdot_letting/snapshot=*"
  else
    fail "s7 R2 snapshot probe returned: ${r2_present}"
  fi

  # 7b — Lance dataset count + BTREE indexes (per p2 — count BEFORE polaris check).
  # System python3 lacks pylance; wrap with `uv run --with pylance` for ephemeral env.
  echo "  (probing Lance dataset + BTREE indices — takes ~10s) ..."
  lance_check=$(cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- uv run --with pylance python3 -c "
import os, lance, sys
opts = {'aws_endpoint': os.environ['R2_ENDPOINT'], 'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'], 'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'], 'aws_region': 'us-east-1', 'aws_virtual_hosted_style_request': 'false'}
ds = lance.dataset('s3://dex-raw-landing-zone/polaris-warehouse/txstate/txdot_letting_lance', storage_options=opts)
n = ds.count_rows()
idx = ds.list_indices()
cols = sorted({(i['fields'][0] if isinstance(i.get('fields'), list) and i['fields'] else i.get('name','?')) for i in idx})
print(f'count={n}')
print(f'indexes={cols}')
" 2>&1)
  echo "${lance_check}" | sed 's/^/  /'
  lance_n=$(echo "${lance_check}" | grep '^count=' | head -1 | sed 's/count=//')
  if [[ -n "${lance_n}" && "${lance_n}" =~ ^[0-9]+$ && "${lance_n}" -ge 900000 ]]; then
    pass "s7 Lance count_rows=${lance_n} >= floor 900_000"
  else
    fail "s7 Lance count_rows='${lance_n}' < floor 900_000"
  fi
  for col in controlling_project_id_ccsj vendor_name_normalized project_actual_let_date; do
    if echo "${lance_check}" | grep -q "${col}"; then
      pass "s7 Lance BTREE on ${col} (or typed sibling) present"
    else
      fail "s7 Lance BTREE on ${col} (or typed sibling) missing"
    fi
  done

  # 7c — Polaris generic-table registration (--check-only after count_rows passes).
  # Uses exit-code semantics (init_polaris_lance_generic.py:208/210: exit 0 on registered, exit 1 on NOT REGISTERED).
  # Capturing stdout+stderr for diagnostics on FAIL.
  polaris_output=$(cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 scripts/init_polaris_lance_generic.py --namespace txstate --table txdot_letting_lance --check-only 2>&1)
  polaris_rc=$?
  if [[ "${polaris_rc}" -eq 0 ]]; then
    pass "s7 Polaris generic-table txstate/txdot_letting_lance registered (check-only exit 0)"
  else
    fail "s7 Polaris check-only exit ${polaris_rc} — last lines: $(echo "${polaris_output}" | tail -2 | tr '\n' ' | ')"
  fi

  # 7d — ops.data_sources row (5-col shape, status=active). Uses dex_psql_query helper.
  ds_row=$(dex_psql_query "SELECT format||'|'||status||'|'||owner_app FROM ops.data_sources WHERE display_name='txstate.txdot_letting_lance'" 2>/dev/null | tr -d '[:space:]')
  if [[ "${ds_row}" == "lance|active|data-engine-x" ]]; then
    pass "s7 ops.data_sources row = (lance, active, data-engine-x)"
  else
    fail "s7 ops.data_sources row = '${ds_row}' (expected 'lance|active|data-engine-x')"
  fi

  # 7e — ops.txdot_letting_r2_ingest_runs latest row status. Uses dex_psql_query helper.
  latest_status=$(dex_psql_query "SELECT status FROM ops.txdot_letting_r2_ingest_runs ORDER BY started_at DESC NULLS LAST LIMIT 1" 2>/dev/null | tr -d '[:space:]')
  if [[ "${latest_status}" == "completed" ]]; then
    pass "s7 ops.txdot_letting_r2_ingest_runs latest status=completed"
  else
    fail "s7 ops.txdot_letting_r2_ingest_runs latest status='${latest_status}' (expected completed)"
  fi
fi

# ----------------------------------------------------------------------
# s8 — config: bridge emit + Polaris registration
# ----------------------------------------------------------------------
if should_run "s8" "hq-all"; then
  echo "=== s8: runtime state — bridge Lance dataset + Polaris + ops.bridges + L21 ==="

  # 8a — Lance bridge dataset count + BTREE.
  # System python3 lacks pylance; wrap with `uv run --with pylance` for ephemeral env.
  echo "  (probing bridge Lance dataset) ..."
  bridge_check=$(cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- uv run --with pylance python3 -c "
import os, lance
opts = {'aws_endpoint': os.environ['R2_ENDPOINT'], 'aws_access_key_id': os.environ['R2_ACCESS_KEY_ID'], 'aws_secret_access_key': os.environ['R2_SECRET_ACCESS_KEY'], 'aws_region': 'us-east-1', 'aws_virtual_hosted_style_request': 'false'}
ds = lance.dataset('s3://dex-raw-landing-zone/polaris-warehouse/bridges/sba_txdot_winners_lance', storage_options=opts)
n = ds.count_rows()
idx = ds.list_indices()
cols = sorted({(i['fields'][0] if isinstance(i.get('fields'), list) and i['fields'] else i.get('name','?')) for i in idx})
print(f'count={n}')
print(f'indexes={cols}')
" 2>&1)
  echo "${bridge_check}" | sed 's/^/  /'
  bridge_n=$(echo "${bridge_check}" | grep '^count=' | head -1 | sed 's/count=//')
  if [[ -n "${bridge_n}" && "${bridge_n}" =~ ^[0-9]+$ && "${bridge_n}" -ge 25 ]]; then
    pass "s8 bridge Lance count_rows=${bridge_n} >= floor 25"
  else
    fail "s8 bridge Lance count_rows='${bridge_n}' < floor 25"
  fi
  if echo "${bridge_check}" | grep -q 'sba_legal_name_normalized'; then
    pass "s8 bridge Lance BTREE on sba_legal_name_normalized present"
  else
    fail "s8 bridge Lance BTREE on sba_legal_name_normalized missing"
  fi

  # 8b — Polaris generic-table bridge registration.
  # Uses exit-code semantics (init_polaris_lance_generic.py:208/210: exit 0 on registered, exit 1 on NOT REGISTERED).
  polaris_output=$(cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 scripts/init_polaris_lance_generic.py --namespace bridges --table sba_txdot_winners_lance --check-only 2>&1)
  polaris_rc=$?
  if [[ "${polaris_rc}" -eq 0 ]]; then
    pass "s8 Polaris bridges/sba_txdot_winners_lance registered (check-only exit 0)"
  else
    fail "s8 Polaris check-only exit ${polaris_rc} — last lines: $(echo "${polaris_output}" | tail -2 | tr '\n' ' | ')"
  fi

  # 8c — ops.match_methods has _tx row. Uses dex_psql_query helper.
  tx_method_count=$(dex_psql_query "SELECT COUNT(*) FROM ops.match_methods WHERE method_name='legal_name_state_exact_tx'" 2>/dev/null | tr -d '[:space:]')
  if [[ "${tx_method_count}" == "1" ]]; then
    pass "s8 ops.match_methods has 1 row for legal_name_state_exact_tx"
  else
    fail "s8 ops.match_methods row count for legal_name_state_exact_tx = '${tx_method_count}' (expected 1)"
  fi

  # 8d — ops.match_method_versions has (tx, 1.0.0). Uses dex_psql_query helper.
  tx_version_count=$(dex_psql_query "SELECT COUNT(*) FROM ops.match_method_versions mmv JOIN ops.match_methods mm USING(match_method_id) WHERE mm.method_name='legal_name_state_exact_tx' AND mmv.semver='1.0.0'" 2>/dev/null | tr -d '[:space:]')
  if [[ "${tx_version_count}" == "1" ]]; then
    pass "s8 ops.match_method_versions has (legal_name_state_exact_tx, 1.0.0)"
  else
    fail "s8 ops.match_method_versions count = '${tx_version_count}' (expected 1)"
  fi

  # 8e — L21 STRICT negative-assert: _ca + _fl method-rows still present and unchanged.
  preserved_count=$(dex_psql_query "SELECT COUNT(*) FROM ops.match_methods WHERE method_name IN ('legal_name_state_exact_ca', 'legal_name_state_exact_fl')" 2>/dev/null | tr -d '[:space:]')
  if [[ "${preserved_count}" == "2" ]]; then
    pass "s8 L21 STRICT: ops.match_methods _ca + _fl rows preserved (count=2)"
  else
    fail "s8 L21 STRICT VIOLATION: ops.match_methods _ca + _fl row count = '${preserved_count}' (expected 2)"
  fi

  # 8f — ops.bridges has sba_txdot_winners row. Uses dex_psql_query helper.
  bridge_count=$(dex_psql_query "SELECT COUNT(*) FROM ops.bridges WHERE bridge_name='sba_txdot_winners'" 2>/dev/null | tr -d '[:space:]')
  if [[ "${bridge_count}" == "1" ]]; then
    pass "s8 ops.bridges has 1 row for sba_txdot_winners"
  else
    fail "s8 ops.bridges row count = '${bridge_count}' (expected 1)"
  fi

  # 8g — ops.bridge_generation_runs latest completed row with rows_matched >= 25.
  # rows_matched is a bigint COLUMN (not a JSONB key); ops.bridge_generation_runs
  # also has bridge_name as a direct text column (not via bridge_id join).
  run_check=$(dex_psql_query "SELECT status||'|'||COALESCE(rows_matched::text, '0') FROM ops.bridge_generation_runs WHERE bridge_name='sba_txdot_winners' ORDER BY started_at DESC LIMIT 1" 2>/dev/null | tr -d '[:space:]')
  run_status="${run_check%%|*}"
  run_rm="${run_check##*|}"
  if [[ "${run_status}" == "completed" && -n "${run_rm}" && "${run_rm}" =~ ^[0-9]+$ && "${run_rm}" -ge 25 ]]; then
    pass "s8 ops.bridge_generation_runs latest status=completed rows_matched=${run_rm}"
  else
    fail "s8 ops.bridge_generation_runs latest row '${run_check}' (expected completed with rows_matched>=25)"
  fi
fi

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "PASS: all requested surfaces verified"
  exit 0
else
  echo "FAIL: ${#FAILURES[@]} check(s) failed:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
