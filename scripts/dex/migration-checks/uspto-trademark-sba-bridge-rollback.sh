#!/usr/bin/env bash
# Rollback harness for /scope cycle
#   uspto-trademark-lance-and-sba-capital-matching-bridge.
#
# Runs surface rollbacks in REVERSE order (s9 → s8 → s7 → s6 → s5 → s4 →
# s3 → s2 → s1). For migration + code surfaces, rollback is `git revert
# <merge-SHA>` (forward-only per apps/data-engine-x/supabase/migrations/README.md
# §"Policy" — IF NOT EXISTS / ON CONFLICT DO UPDATE patterns make re-apply
# idempotent). For Polaris registrations (s6), DELETE per generic-table. For
# data backfill (s7), best-effort R2 prefix cleanup. For deploy (s9),
# `git revert <merge-SHA>` → Railway auto-redeploys the prior commit; manual
# `railway redeploy` is the fallback.
#
# Accepts:
#   --surface <id>    roll back a single surface
#   --repo <name>     roll back one repo's surfaces (only hq-all in this cycle)
#
# Sources the canonical helper library via the migration-checks shim.

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

# --- Polaris generic-table DELETE helper --------------------------------- #
# Usage: _polaris_delete_table <namespace> <table>
_polaris_delete_table() {
  local ns="$1" tbl="$2"
  doppler run --project hq-all --config prd -- bash -c "
    TOK=\$(curl -fsS -X POST \"\$POLARIS_PUBLIC_URL/api/catalog/v1/oauth/tokens\" \
      -d 'grant_type=client_credentials' \
      -d \"client_id=\$POLARIS_ROOT_PRINCIPAL_ID\" \
      -d \"client_secret=\$POLARIS_ROOT_PRINCIPAL_SECRET\" \
      -d 'scope=PRINCIPAL_ROLE:ALL' | jq -r .access_token)
    HTTP_CODE=\$(curl -s -o /dev/null -w '%{http_code}' \
      -X DELETE \"\$POLARIS_PUBLIC_URL/api/catalog/polaris/v1/\$POLARIS_DEFAULT_CATALOG_NAME/namespaces/$ns/generic-tables/$tbl\" \
      -H \"Authorization: Bearer \$TOK\")
    test \"\$HTTP_CODE\" = \"204\" || test \"\$HTTP_CODE\" = \"200\" || test \"\$HTTP_CODE\" = \"404\"
  "
}

# --- R2 prefix rm helper ------------------------------------------------- #
# Usage: _r2_rm_prefix <prefix-after-polaris-warehouse>
_r2_rm_prefix() {
  local prefix="$1"
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://dex-raw-landing-zone/polaris-warehouse/$prefix/ \
        --recursive --endpoint-url \$R2_ENDPOINT
  "
}

# REVERSE order — most-likely-to-need-rollback surfaces first.

# ── s9: Railway hq-x + data-engine-x — redeploy / revert merge ─────────── #
rollback_surface "s9" "hq-all" '
  echo "manual (per apps/data-engine-x/CLAUDE.md §\"Deploy targets\"):"
  echo "  Option 1 (preferred): git revert <merge-SHA> on main; Railway auto-deploys the prior commit for BOTH hq-x and data-engine-x."
  echo "  Option 2 (force redeploy each service):"
  echo "    doppler run --project hq-all --config prd -- railway redeploy --service hq-x --yes"
  echo "    doppler run --project hq-all --config prd -- railway redeploy --service data-engine-x --yes"
  echo "  Lookup prior deployment id (informational only — CLI 4.33.0 cannot target a specific deployment-id at redeploy time):"
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service hq-x --limit 2 --json | jq -r \".[1].id\""
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service data-engine-x --limit 2 --json | jq -r \".[1].id\""
'

# ── s8: Trigger.dev cron extension — git revert removes DEX script entry ── #
# The hq-x Trigger task file unchanged (no behavioural edit needed); revert
# of the DEX endpoint's SCRIPTS list is what backs out the USPTO bridge call.
rollback_surface "s8" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA>; DEX FastAPI redeploys without the build_bridge_uspto_sba_capital_matching_lance.py entry in apps/data-engine-x/app/routers/sba_bridges_internal_v1.py SCRIPTS list."
  echo "  No hq-x Trigger.dev disable required (the task itself was not modified — only the downstream DEX script list)."
  echo "  If the hq-x cron has fired since merge and produced a partial bridge row, the next daily run after the revert will skip the USPTO step; pre-existing rows in polaris-warehouse/bridges/uspto_sba_capital_matching_lance/ can be cleaned via s7 rollback below."
'

# ── s7: backfill — best-effort R2 prefix delete ────────────────────────── #
# In reverse dependency order: bridge first, then USPTO sources.
# ops.bridge_generation_runs ledger rows are NOT cleaned (audit history is forward-only).
rollback_surface "s7" "hq-all" '
  _r2_rm_prefix "bridges/uspto_sba_capital_matching_lance"        || true
  _r2_rm_prefix "uspto/correspondent_domrep_attorney_lance"       || true
  _r2_rm_prefix "uspto/case_file_owner_lance"                     || true
  _r2_rm_prefix "uspto/case_file_lance"                           || true
  echo "Note: ops.bridge_generation_runs ledger rows for bridge_name=uspto_sba_capital_matching are NOT cleaned (audit history is forward-only)."
'

# ── s6: Polaris generic-table registrations — DELETE per table ─────────── #
rollback_surface "s6" "hq-all" '
  _polaris_delete_table "bridges" "uspto_sba_capital_matching_lance"     &&
  _polaris_delete_table "uspto"   "correspondent_domrep_attorney_lance"  &&
  _polaris_delete_table "uspto"   "case_file_owner_lance"                &&
  _polaris_delete_table "uspto"   "case_file_lance"
'

# ── s5: ops.data_sources migration — git revert + UPSERT inverse ───────── #
rollback_surface "s5" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA>. Forward-only per migrations/README.md §\"Policy\"; the UPSERT pattern + ON CONFLICT DO UPDATE makes re-application after revert idempotent."
  echo "  If you need to retire the new Lance rows manually:"
  echo "    UPDATE ops.data_sources SET status=\"retired\", retired_at=NOW() WHERE display_name IN (\"uspto_case_file_lance\",\"uspto_case_file_owner_lance\",\"uspto_correspondent_domrep_attorney_lance\",\"uspto_sba_capital_matching_lance\");"
'

# ── s4: USPTO × SBA capital-matching bridge — git revert + R2 cleanup ──── #
rollback_surface "s4" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes build_bridge_uspto_sba_capital_matching_lance.py)."
  _r2_rm_prefix "bridges/uspto_sba_capital_matching_lance" || true
'

# ── s3: USPTO correspondent Lance — git revert + R2 cleanup ────────────── #
rollback_surface "s3" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_uspto_correspondent_lance.py)."
  _r2_rm_prefix "uspto/correspondent_domrep_attorney_lance" || true
'

# ── s2: USPTO case_file_owner Lance — git revert + R2 cleanup ──────────── #
rollback_surface "s2" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_uspto_case_file_owner_lance.py)."
  _r2_rm_prefix "uspto/case_file_owner_lance" || true
'

# ── s1: USPTO case_file Lance — git revert + R2 cleanup ────────────────── #
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_uspto_case_file_lance.py)."
  _r2_rm_prefix "uspto/case_file_lance" || true
'

echo ""
echo "Rollback complete (manual git-revert steps printed above; R2 + Polaris cleanup attempted)."
