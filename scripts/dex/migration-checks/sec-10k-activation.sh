#!/usr/bin/env bash
# Verification harness for /scope sub-cycle sec-10k-activation.
#
# Runs every surface's verify command in declared order (s1 .. s5).
# Sources the canonical dex.sh helper via the vault shim.
# Exits 0 iff every check passes.
#
# Usage:
#   bash ~/hq-all/apps/data-engine-x/scripts/migration-checks/sec-10k-activation.sh [--repo hq-all]

set -euo pipefail

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# shellcheck source=/dev/null
source "$HOME/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || echo "$HOME/hq-all")"
SCRIPT_PATH="$REPO_ROOT/apps/data-engine-x/scripts/run_sec_edgar_form_10k_r2_ingest.py"
LANCE_EMIT_PATH="$REPO_ROOT/apps/data-engine-x/scripts/emit_sec_edgar_form_10k_lance.py"
MODAL_APP_PATH="$REPO_ROOT/apps/data-engine-x/modal/sec_edgar_form_10k_app.py"
DATA_SOURCES_MIG_GLOB="$REPO_ROOT/apps/data-engine-x/supabase/migrations/*_sec_edgar_form_10k_data_sources.sql"

echo "==> Verifying surfaces (filter: ${REPO_FILTER:-all})"

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

# --- s1: TARGET_RPS edit in ingest script -------------------------------- #
# Verifies DEFAULT_TARGET_RPS = 1 (not 2) at module level.
run_surface "s1" "hq-all" 'grep -E "^DEFAULT_TARGET_RPS = 1$" "$SCRIPT_PATH" >/dev/null'

# --- s2: Lance emitter file exists + parses ------------------------------ #
run_surface "s2" "hq-all" '[ -f "$LANCE_EMIT_PATH" ] && python3 -c "import ast; ast.parse(open(\"$LANCE_EMIT_PATH\").read())"'

# --- s3: ops.data_sources rows ------------------------------------------- #
# Migration applied → both rows exist with status='active'.
run_surface "s3" "hq-all" '
  count=$(dex_psql_query "SELECT COUNT(*) FROM ops.data_sources WHERE display_name IN ('"'"'sec_edgar_form_10k'"'"', '"'"'sec_edgar_form_10k_lance'"'"') AND status = '"'"'active'"'"'");
  [[ "$count" == "2" ]] && ls $DATA_SOURCES_MIG_GLOB >/dev/null 2>&1
'

# --- s4: Modal app file exists + parses --------------------------------- #
run_surface "s4" "hq-all" '[ -f "$MODAL_APP_PATH" ] && python3 -c "import ast; ast.parse(open(\"$MODAL_APP_PATH\").read())"'

# --- s5: Modal cron deployed + audit-row from Modal-driven invocation --- #
# Two-part check:
#  a) `modal app list --json` shows data-engine-x-sec-edgar-form-10k as deployed.
#  b) ops.sec_edgar_form_10k_r2_ingest_runs has ≥1 row inserted within the last 30min.
#
# Backfill volume-floor check (the bigger 50K floor) runs separately as a
# post-cycle verifier — at deploy-time the floor is 0 and shouldn't gate
# deploy-verifier. See cycle report for end-of-cycle floor outcome.
run_surface "s5" "hq-all" '
  modal app list --json 2>/dev/null | python3 -c "
import sys, json
apps = json.load(sys.stdin)
ok = any(\"sec-edgar-form-10k\" in (a.get(\"Description\") or \"\") for a in apps)
sys.exit(0 if ok else 1)
" || exit 1
  recent=$(dex_psql_query "SELECT COUNT(*) FROM ops.sec_edgar_form_10k_r2_ingest_runs WHERE started_at > now() - interval '"'"'30 minutes'"'"'");
  [[ "$recent" =~ ^[1-9][0-9]*$ ]]
'

echo "All requested surfaces verified."
