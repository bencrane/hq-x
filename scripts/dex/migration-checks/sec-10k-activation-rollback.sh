#!/usr/bin/env bash
# Rollback harness for /scope sub-cycle sec-10k-activation.
#
# Surfaces are rolled back in REVERSE declared order (s5 → s1).
#
# Usage:
#   bash ~/hq-all/apps/data-engine-x/scripts/migration-checks/sec-10k-activation-rollback.sh \
#        [--surface s1|s2|s3|s4|s5] [--repo hq-all]

set -euo pipefail

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
source "$HOME/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")"

echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# REVERSE order — last surface rolled back first.

# --- s5 rollback: stop Modal app ---------------------------------------- #
# `modal app stop` is idempotent — succeeds even if app not running.
rollback_surface "s5" "hq-all" '
  if modal app list --json 2>/dev/null | python3 -c "
import sys, json
apps = json.load(sys.stdin)
ok = any(\"sec-edgar-form-10k\" in (a.get(\"Description\") or \"\") for a in apps)
sys.exit(0 if ok else 1)
"; then
    modal app stop data-engine-x-sec-edgar-form-10k 2>&1 || true
  fi
  echo "Modal app stopped (or absent)."
'

# --- s4 rollback: remove Modal app file -------------------------------- #
# Code change → git revert post-merge. Pre-merge: delete the file.
rollback_surface "s4" "hq-all" '
  if [[ -f "$REPO_ROOT/apps/data-engine-x/modal/sec_edgar_form_10k_app.py" ]]; then
    echo "NOTE: Pre-merge — delete modal/sec_edgar_form_10k_app.py via git checkout HEAD or git rm. Post-merge — git revert <merge-SHA>."
  fi
  echo "s4 rollback annotated (git revert post-merge)."
'

# --- s3 rollback: ops.data_sources rows ------------------------------- #
# Migration applied → forward-only. Rollback is git revert <merge-SHA>; in
# the meantime, set status='retired' for the two rows so observability
# dashboards stop alerting on a not-yet-real source.
rollback_surface "s3" "hq-all" '
  dex_psql_ddl "UPDATE ops.data_sources SET status='"'"'retired'"'"', retired_at = COALESCE(retired_at, NOW()) WHERE display_name IN ('"'"'sec_edgar_form_10k'"'"', '"'"'sec_edgar_form_10k_lance'"'"')"
'

# --- s2 rollback: delete Lance emit script + Lance dataset prefix ---- #
rollback_surface "s2" "hq-all" '
  echo "NOTE: Pre-merge — git checkout HEAD apps/data-engine-x/scripts/emit_sec_edgar_form_10k_lance.py. Post-merge — git revert <merge-SHA>."
  echo "NOTE: Lance dataset at polaris-warehouse/sec_edgar/form_10k_lance/ — delete via:"
  echo "  doppler run --project hq-all --config prd -- bash -c '"'"'python3 -c \"import boto3, os; s3=boto3.client(\\\"s3\\\", endpoint_url=os.environ[\\\"R2_ENDPOINT\\\"], aws_access_key_id=os.environ[\\\"R2_ACCESS_KEY_ID\\\"], aws_secret_access_key=os.environ[\\\"R2_SECRET_ACCESS_KEY\\\"]); resp=s3.list_objects_v2(Bucket=\\\"dex-raw-landing-zone\\\", Prefix=\\\"polaris-warehouse/sec_edgar/form_10k_lance/\\\"); [s3.delete_object(Bucket=\\\"dex-raw-landing-zone\\\", Key=o[\\\"Key\\\"]) for o in resp.get(\\\"Contents\\\", [])]\"'"'"'"
'

# --- s1 rollback: restore TARGET_RPS = 2 in ingest script ------------ #
rollback_surface "s1" "hq-all" '
  echo "NOTE: Pre-merge — git checkout HEAD apps/data-engine-x/scripts/run_sec_edgar_form_10k_r2_ingest.py. Post-merge — git revert <merge-SHA>."
'

echo "Rollback complete (annotated). For post-merge code rollbacks, run git revert <merge-SHA>."
