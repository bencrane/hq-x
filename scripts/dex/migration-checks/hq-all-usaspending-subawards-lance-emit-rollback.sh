#!/usr/bin/env bash
# Rollback harness for /scope cycle hq-all-usaspending-subawards-lance-emit.
#
# Authored by Stage 3.A audit subagent (2026-05-16 UTC). Mirrors
# hq-all-sam-pdl-usaspending-bridge-lance-emit-rollback.sh (direct precedent — PR #469).
#
# Runs surface rollbacks in REVERSE order (s6 → s5 → s4 → s3 → s2 → s1). All
# surfaces are forward-only per apps/data-engine-x/supabase/migrations/README.md
# §"Policy" — for code + migration + endpoint surfaces, rollback is
# `git revert <merge-SHA>` on hq-all main (the `ON CONFLICT DO UPDATE` +
# `IF NOT EXISTS` make re-apply idempotent). For Polaris registration (s5),
# DELETE is idempotent (200/204/404 all acceptable). For Railway deploy (s6),
# redeploy the prior deployment id via the validator-fixed monitoring/rollback
# command pair.
#
# Note: NONE of the surfaces here are destructive — all writes are NEW data
# (2 new emit scripts, 2 new Lance datasets, 2 new ops rows, 2 new Polaris
# registrations, 2 new view entries). The rollback gate per /scope SKILL.md
# is satisfied for every surface.
#
# Accepts:
#   --surface <id>    roll back a single surface
#   --repo <name>     roll back one repo's surfaces (only hq-all in this cycle)

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
# DELETE returns 200/204 on success, 404 if already absent — all acceptable.
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

# --- R2 prefix rm helper (best-effort orphan cleanup; safe to fail) ----- #
# Usage: _r2_rm_prefix <prefix-after-bucket>
_r2_rm_prefix() {
  local prefix="$1"
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://dex-raw-landing-zone/$prefix/ \
        --recursive --endpoint-url \$R2_ENDPOINT
  "
}

# REVERSE order (s6 → s1) — most-recent surface first.

# ── s6: Railway data-engine-x — redeploy prior deployment id ──────────── #
rollback_surface "s6" "hq-all" '
  echo "manual (per apps/data-engine-x/CLAUDE.md §\"Deploy targets\" — validator-fixed):"
  echo "  Lookup prior deployment id (validator-fixed monitoring command):"
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service data-engine-x --limit 2 --json | jq -r \".[1].id\""
  echo "  Redeploy prior:"
  echo "    doppler run --project hq-all --config prd -- railway redeploy --service data-engine-x --deployment-id <prior-id>"
  echo ""
  echo "  Preferred path: git -C $HQ_ALL_ROOT revert <merge-SHA> on main; Railway auto-deploys the prior commit."
'

# ── s5: Polaris generic-table registrations — DELETE x 2 ──────────────── #
# Idempotent (200/204/404 all acceptable). Two namespaces.tables to clean up.
rollback_surface "s5-contract" "hq-all" '
  _polaris_delete_table "usaspending" "contract_subawards_lance" || true
'
rollback_surface "s5-assistance" "hq-all" '
  _polaris_delete_table "usaspending" "assistance_subawards_lance" || true
'

# ── s4: lance_views.py — git revert (2 LanceView entries appended) ────── #
rollback_surface "s4" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes both usaspending_contract_subawards_lance_raw and usaspending_assistance_subawards_lance_raw LanceView entries from apps/data-engine-x/app/services/lance_views.py)."
  echo "  Append-only diff; revert is clean. Both new entries carry register_at_boot=False so the live FastAPI boot path is unaffected pre-revert."
'

# ── s3: ops.data_sources migration — git revert (forward-only) ──────── #
rollback_surface "s3" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes supabase/migrations/{ts}_usaspending_subawards_lance_data_sources.sql)."
  echo "  Forward-only per migrations/README.md §\"Policy\". ON CONFLICT (display_name) DO UPDATE makes re-apply idempotent."
  echo "  Note: the 2 ops.data_sources rows are NOT auto-deleted on revert (forward-only DDL). To manually retire them:"
  echo "    UPDATE ops.data_sources SET status='"'"'retired'"'"', retired_at=NOW() WHERE display_name IN ('"'"'contract_subawards_lance'"'"', '"'"'assistance_subawards_lance'"'"');"
'

# ── s2: assistance subawards emit script — git revert + R2 cleanup ───── #
rollback_surface "s2" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes run_usaspending_assistance_subawards_lance_emit.py)."
  _r2_rm_prefix "polaris-warehouse/usaspending/assistance_subawards_lance" || true
  echo "  NB: the upstream r2://dex-raw-landing-zone/usaspending/assistance_subawards/year=2026/data.parquet snapshot is intentionally NOT cleaned up — it is the source dataset (one-shot CSV ingest 2026-05-09)."
'

# ── s1: contract subawards emit script — git revert + R2 cleanup ─────── #
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes run_usaspending_contract_subawards_lance_emit.py)."
  _r2_rm_prefix "polaris-warehouse/usaspending/contract_subawards_lance" || true
  echo "  NB: the upstream r2://dex-raw-landing-zone/usaspending/contract_subawards/year=2026/data.parquet snapshot is intentionally NOT cleaned up — it is the source dataset (one-shot CSV ingest 2026-05-09)."
'

echo ""
echo "Rollback complete (manual git-revert steps printed; R2 + Polaris cleanup attempted)."
echo ""
echo "NB: All 6 surfaces are NEW data — none mutate or destroy prior state. Re-application after revert is idempotent by construction (IF NOT EXISTS / ON CONFLICT DO UPDATE / GET-first Polaris / mode='overwrite' Lance write)."
