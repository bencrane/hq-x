#!/usr/bin/env bash
# txdot-letting-substrate-rollback.sh — rollback harness for the
# txdot-letting-substrate cycle (8 surfaces).
#
# REVERSE order: s8 → s7 → s6 → s5 → s4 → s3 → s2 → s1.
#
# Migration cycle: TXDOT letting bid-tabulation history → R2 → Lance + Polaris
#                  txstate.txdot_letting_lance + bridges.sba_txdot_winners_lance
# Directive:       /Users/benjamincrane/Desktop/hq/directives/2026-05-18-hq-all-txdot-letting-substrate.md
#
# This script is DESTRUCTIVE for s7+s8 (Lance/R2/Polaris teardown). It does
# NOT execute git reverts for code/migration surfaces (s1..s6) — those are
# operator actions printed as instructions only. Use ONLY with operator
# confirmation.
#
# Flags:
#   --surface s<N>   roll back a single surface only
#   --repo <name>    filter by repo (only hq-all here; no-op)
#
# Exit 0 iff all requested rollbacks succeeded. Exit 1 on any failure.

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

# shellcheck source=/dev/null
source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="${HOME}/hq-all"
DEX_ROOT="${REPO_ROOT}/apps/data-engine-x"

FAILURES=()
pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1"; FAILURES+=("$1"); }

should_run() {
  local id="$1" repo="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 1; fi
  if [[ -n "$REPO_FILTER"    && "$REPO_FILTER"    != "$repo" ]]; then return 1; fi
  return 0
}

echo "==> ROLLBACK txdot-letting-substrate (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"
echo "    DESTRUCTIVE for s7+s8. Use only with operator confirmation."
echo

# ----------------------------------------------------------------------
# s8 — bridge runtime state (R2 + Polaris + optional ops.bridge_generation_runs)
# ----------------------------------------------------------------------
if should_run "s8" "hq-all"; then
  echo "=== s8 ROLLBACK — bridge runtime state ==="

  # 8a — Optional: stop running Modal app shell.
  if command -v modal >/dev/null 2>&1; then
    echo "  (advisory) modal app stop data-engine-x-sba-txdot-winners-lance (non-fatal if not running)"
    modal app stop data-engine-x-sba-txdot-winners-lance 2>/dev/null || true
  fi

  # 8b — Polaris DELETE.
  if cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 -c "
import os, requests
url = os.environ['POLARIS_PUBLIC_URL'].rstrip('/')
catalog = os.environ['POLARIS_DEFAULT_CATALOG_NAME']
tok = requests.post(f'{url}/api/catalog/v1/oauth/tokens',
    data={'grant_type':'client_credentials','client_id':os.environ['POLARIS_ROOT_PRINCIPAL_ID'],'client_secret':os.environ['POLARIS_ROOT_PRINCIPAL_SECRET'],'scope':'PRINCIPAL_ROLE:ALL'}).json()['access_token']
r = requests.delete(f'{url}/api/catalog/polaris/v1/{catalog}/namespaces/bridges/generic-tables/sba_txdot_winners_lance', headers={'Authorization':f'Bearer {tok}'})
print(f'polaris DELETE status={r.status_code}')
exit(0 if r.status_code in (200, 204, 404) else 1)
" 2>&1; then
    pass "s8 Polaris DELETE bridges/sba_txdot_winners_lance OK (or already absent)"
  else
    fail "s8 Polaris DELETE failed"
  fi

  # 8c — R2 prefix delete.
  if cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 -c "
import os, boto3
s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto')
paginator = s3.get_paginator('list_objects_v2')
keys = []
for page in paginator.paginate(Bucket='dex-raw-landing-zone', Prefix='polaris-warehouse/bridges/sba_txdot_winners_lance/'):
    keys.extend({'Key': obj['Key']} for obj in page.get('Contents', []))
while keys:
    batch, keys = keys[:1000], keys[1000:]
    s3.delete_objects(Bucket='dex-raw-landing-zone', Delete={'Objects': batch})
print(f'deleted {len(keys)} objects (batches processed)')
" 2>&1; then
    pass "s8 R2 delete polaris-warehouse/bridges/sba_txdot_winners_lance/ OK"
  else
    fail "s8 R2 delete failed"
  fi

  # 8d — Optional DELETE ops.bridge_generation_runs (UCC-CA rollback leaves these intact for
  # forensic trace; we follow that precedent — print instruction only).
  echo "  (advisory) leave ops.bridge_generation_runs rows intact for forensic trace (UCC-CA rollback precedent)"
  echo "      manual cleanup if desired: psql \"\$DEX_DB_URL_DIRECT\" -c \"DELETE FROM ops.bridge_generation_runs WHERE bridge_name='sba_txdot_winners'\""
  echo "  (advisory) leave ops.bridges + ops.match_methods + ops.match_method_versions rows intact"
  echo "      (idempotent UPSERTs by name/semver — re-runs reuse same UUIDs)"
fi

# ----------------------------------------------------------------------
# s7 — Lance dataset + R2 snapshot + Polaris registration
# ----------------------------------------------------------------------
if should_run "s7" "hq-all"; then
  echo "=== s7 ROLLBACK — txstate Lance + R2 snapshot + Polaris ==="

  # 7a — Polaris DELETE on txstate/txdot_letting_lance.
  if cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 -c "
import os, requests
url = os.environ['POLARIS_PUBLIC_URL'].rstrip('/')
catalog = os.environ['POLARIS_DEFAULT_CATALOG_NAME']
tok = requests.post(f'{url}/api/catalog/v1/oauth/tokens',
    data={'grant_type':'client_credentials','client_id':os.environ['POLARIS_ROOT_PRINCIPAL_ID'],'client_secret':os.environ['POLARIS_ROOT_PRINCIPAL_SECRET'],'scope':'PRINCIPAL_ROLE:ALL'}).json()['access_token']
r = requests.delete(f'{url}/api/catalog/polaris/v1/{catalog}/namespaces/txstate/generic-tables/txdot_letting_lance', headers={'Authorization':f'Bearer {tok}'})
print(f'polaris DELETE status={r.status_code}')
exit(0 if r.status_code in (200, 204, 404) else 1)
" 2>&1; then
    pass "s7 Polaris DELETE txstate/txdot_letting_lance OK (or already absent)"
  else
    fail "s7 Polaris DELETE failed"
  fi

  # 7b — R2 delete Lance dataset prefix.
  if cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 -c "
import os, boto3
s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto')
paginator = s3.get_paginator('list_objects_v2')
keys = []
for page in paginator.paginate(Bucket='dex-raw-landing-zone', Prefix='polaris-warehouse/txstate/txdot_letting_lance/'):
    keys.extend({'Key': obj['Key']} for obj in page.get('Contents', []))
total = len(keys)
while keys:
    batch, keys = keys[:1000], keys[1000:]
    s3.delete_objects(Bucket='dex-raw-landing-zone', Delete={'Objects': batch})
print(f'deleted {total} objects')
" 2>&1; then
    pass "s7 R2 delete polaris-warehouse/txstate/txdot_letting_lance/ OK"
  else
    fail "s7 R2 delete of Lance dataset failed"
  fi

  # 7c — R2 delete snapshot Parquet.
  if cd "${DEX_ROOT}" && doppler run --project hq-all --config prd -- python3 -c "
import os, boto3
s3 = boto3.client('s3', endpoint_url=os.environ['R2_ENDPOINT'],
    aws_access_key_id=os.environ['R2_ACCESS_KEY_ID'],
    aws_secret_access_key=os.environ['R2_SECRET_ACCESS_KEY'],
    region_name='auto')
paginator = s3.get_paginator('list_objects_v2')
keys = []
for page in paginator.paginate(Bucket='dex-raw-landing-zone', Prefix='txstate/txdot_letting/'):
    keys.extend({'Key': obj['Key']} for obj in page.get('Contents', []))
total = len(keys)
while keys:
    batch, keys = keys[:1000], keys[1000:]
    s3.delete_objects(Bucket='dex-raw-landing-zone', Delete={'Objects': batch})
print(f'deleted {total} snapshot objects')
" 2>&1; then
    pass "s7 R2 delete txstate/txdot_letting/snapshot=*/data.parquet OK"
  else
    fail "s7 R2 delete of snapshot Parquet failed"
  fi

  echo "  (advisory) leave ops.txdot_letting_r2_ingest_runs rows intact for forensic trace"
  echo "  (advisory) leave ops.data_sources rows intact (idempotent UPSERT — re-apply harmless)"
fi

# ----------------------------------------------------------------------
# s6 — code: LanceView entry (git revert)
# ----------------------------------------------------------------------
if should_run "s6" "hq-all"; then
  echo "=== s6 ROLLBACK — LanceView entry (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # removes the LANCE_VIEWS entry"
  echo "  (or manually drop the LanceView(name=\"txdot_letting_raw\", ...) entry from"
  echo "   apps/data-engine-x/app/services/lance_views.py and commit/push)"
fi

# ----------------------------------------------------------------------
# s5 — code: Trigger.dev cron (git revert)
# ----------------------------------------------------------------------
if should_run "s5" "hq-all"; then
  echo "=== s5 ROLLBACK — Trigger.dev cron (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # removes apps/hq-x/src/trigger/txdot-letting-monthly.ts"
  echo "  (Trigger.dev v3 auto-deploys on next main merge — cron will stop firing)"
fi

# ----------------------------------------------------------------------
# s4 — code: Pattern B bridge generator (git revert)
# ----------------------------------------------------------------------
if should_run "s4" "hq-all"; then
  echo "=== s4 ROLLBACK — bridge script (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # removes apps/data-engine-x/scripts/build_bridge_sba_txdot_winners_lance.py"
  echo "  (runtime artifact cleanup handled in s8 rollback)"
fi

# ----------------------------------------------------------------------
# s3 — code: Pattern A Lance emit script (git revert)
# ----------------------------------------------------------------------
if should_run "s3" "hq-all"; then
  echo "=== s3 ROLLBACK — Lance emit script (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # removes apps/data-engine-x/scripts/run_txdot_letting_lance_emit.py"
  echo "  (runtime artifact cleanup handled in s7 rollback)"
fi

# ----------------------------------------------------------------------
# s2 — code: Modal ingest app (git revert)
# ----------------------------------------------------------------------
if should_run "s2" "hq-all"; then
  echo "=== s2 ROLLBACK — Modal ingest app (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # removes apps/data-engine-x/modal/txdot_letting_ingest_app.py"
  echo "  (optional: modal app stop data-engine-x-txdot-letting-ingest; modal app delete data-engine-x-txdot-letting-ingest)"
fi

# ----------------------------------------------------------------------
# s1 — migration (git revert)
# ----------------------------------------------------------------------
if should_run "s1" "hq-all"; then
  echo "=== s1 ROLLBACK — migration (git revert post-merge) ==="
  echo "  git -C ${REPO_ROOT} revert <merge-SHA-of-PR>      # restores prior schema state"
  echo "  (IF NOT EXISTS semantics in apps/data-engine-x/supabase/migrations/README.md §'Policy'"
  echo "   make re-apply idempotent — apply_pending_migrations.sh re-applies cleanly post-revert)"
  echo ""
  echo "  Manual cleanup if desired (NOT required for re-apply):"
  echo "    psql \"\$DEX_DB_URL_DIRECT\" -c \"DELETE FROM ops.data_sources WHERE display_name IN ('txstate.txdot_letting_lance','bridges.sba_txdot_winners_lance')\""
  echo "    psql \"\$DEX_DB_URL_DIRECT\" -c \"DROP TABLE IF EXISTS ops.txdot_letting_r2_ingest_runs CASCADE\""
fi

# ----------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------
echo
if [[ ${#FAILURES[@]} -eq 0 ]]; then
  echo "ROLLBACK OK: all requested rollbacks executed (post-merge git reverts must be run manually for s1-s6)."
  exit 0
else
  echo "ROLLBACK FAIL: ${#FAILURES[@]} step(s) failed:"
  for f in "${FAILURES[@]}"; do echo "  - $f"; done
  exit 1
fi
