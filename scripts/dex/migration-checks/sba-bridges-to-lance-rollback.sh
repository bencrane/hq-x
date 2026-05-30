#!/usr/bin/env bash
# Rollback harness for /scope cycle sba-bridges-to-lance.
#
# Runs surface rollbacks in REVERSE order (s13 → s12 → s11 → s10 → s9 → s8 →
# s7 → s6 → s6.5 → s5 → s4 → s3 → s2 → s1). For migration + code surfaces,
# rollback is `git revert <merge-SHA>` (forward-only per
# apps/data-engine-x/supabase/migrations/README.md §"Policy" — IF NOT EXISTS
# and ON CONFLICT DO UPDATE patterns make re-apply idempotent). For Polaris
# registrations (s9), DELETE per generic-table. For data backfill (s10),
# best-effort R2 prefix cleanup. For deploy (s13), `railway redeploy` to
# latest (the proper rollback for a non-latest deployment is to revert the
# merge commit and let auto-deploy redeploy the prior commit).
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

# ── s14 (smoke): nothing to roll back; matches are operator-cleanable ──── #
rollback_surface "s14" "hq-all" '
  echo "manual: business.matches rows from the smoke test can be cleaned with: DELETE FROM business.matches WHERE relationship_id = (SELECT relationship_id FROM business.matching_relationships WHERE name = \"capital_partner_bridge_match_v1\")"
'

# ── s13: Railway hq-x + data-engine-x — redeploy latest (true rollback = revert commit) ── #
# Reviewer-corrected: both services redeploy on merge so both can need rollback.
rollback_surface "s13" "hq-all" '
  echo "manual (per apps/data-engine-x/CLAUDE.md §\"Deploy targets\"):"
  echo "  Option 1 (preferred): git revert <merge-SHA> on main; Railway auto-deploys the prior commit for BOTH hq-x and data-engine-x."
  echo "  Option 2 (force redeploy each service):"
  echo "    doppler run --project hq-all --config prd -- railway redeploy --service hq-x --yes"
  echo "    doppler run --project hq-all --config prd -- railway redeploy --service data-engine-x --yes"
  echo "  Lookup prior deployment id (informational only — CLI 4.33.0 cannot target a specific deployment-id at redeploy time):"
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service hq-x --limit 2 --json | jq -r \".[1].id\""
  echo "    doppler run --project hq-all --config prd -- railway deployment list --service data-engine-x --limit 2 --json | jq -r \".[1].id\""
'

# ── s12: hq-x matching_relationships seed — DELETE row + git revert ───── #
rollback_surface "s12" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> + (optional) DELETE business row:"
  echo "  doppler run --project hq-all --config prd -- bash -c \"psql \\\"\\\$HQX_DB_URL_DIRECT\\\" -c \\\"DELETE FROM business.matching_relationships WHERE name=\\x27capital_partner_bridge_match_v1\\x27\\\"\""
'

# ── s11: Trigger.dev hq-x project — git revert removes task + companion endpoints ── #
# Reviewer-corrected: s11 lives in hq-x'"'"'s canonical Trigger.dev project
# (proj_khmvxxrpyloqmnivdetu) per memory app_responsibilities.md, NOT DEX'"'"'s.
# Git revert also removes the companion HTTP endpoints in both hq-x and DEX.
rollback_surface "s11" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA>; the hq-x Trigger.dev project redeploys without the sba-bridges-daily task on next merge."
  echo "  Companion endpoints removed by revert:"
  echo "    apps/hq-x/app/routers/internal/sba_bridges.py"
  echo "    apps/data-engine-x/app/routers/sba_bridges_internal_v1.py"
  echo "  Alternative (without revert): disable schedule via Trigger.dev dashboard at https://cloud.trigger.dev (hq-x project: proj_khmvxxrpyloqmnivdetu)"
'

# ── s10: backfill — best-effort R2 prefix delete + Polaris DELETE ──────── #
rollback_surface "s10" "hq-all" '
  # In reverse dependency order: bridges first, then borrowers/lenders, then loans.
  _r2_rm_prefix "bridges/usaspending_sba_borrower_lance" || true
  _r2_rm_prefix "bridges/sam_sba_borrower_lance"         || true
  _r2_rm_prefix "bridges/pdl_sba_borrower_lance"         || true
  _r2_rm_prefix "pdl/free_companies_lance"               || true
  _r2_rm_prefix "sam_gov/entities_lance"                 || true
  _r2_rm_prefix "sba/lenders_lance"                      || true
  _r2_rm_prefix "sba/borrowers_lance"                    || true
  _r2_rm_prefix "sba/loans_lance"                        || true
  echo "Note: ops.bridge_generation_runs ledger rows are NOT cleaned (audit history is forward-only)."
'

# ── s9: Polaris generic-table registrations — DELETE per table ─────────── #
rollback_surface "s9" "hq-all" '
  _polaris_delete_table "bridges" "usaspending_sba_borrower_lance" &&
  _polaris_delete_table "bridges" "sam_sba_borrower_lance"         &&
  _polaris_delete_table "bridges" "pdl_sba_borrower_lance"         &&
  _polaris_delete_table "pdl"     "free_companies_lance"           &&
  _polaris_delete_table "sam_gov" "entities_lance"                 &&
  _polaris_delete_table "sba"     "sba_lenders_lance"              &&
  _polaris_delete_table "sba"     "sba_borrowers_lance"            &&
  _polaris_delete_table "sba"     "sba_loans_lance"
'

# ── s8: ops.data_sources migration — git revert + UPSERT inverse ───────── #
rollback_surface "s8" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA>. Forward-only per migrations/README.md §\"Policy\"; the UPSERT pattern + ON CONFLICT DO UPDATE makes re-application after revert idempotent."
  echo "  If you need to retire the new Lance rows manually:"
  echo "    UPDATE ops.data_sources SET status=\"retired\", retired_at=NOW() WHERE display_name IN (\"sba_loans_lance\",\"sba_borrowers_lance\",\"sba_lenders_lance\",\"sam_entities_lance\",\"pdl_free_companies_lance\",\"pdl_sba_borrower_lance\",\"sam_sba_borrower_lance\",\"usaspending_sba_borrower_lance\");"
  echo "  Re-activate the legacy bridges row:"
  echo "    UPDATE ops.data_sources SET status=\"needs_triage\", retired_at=NULL WHERE display_name=\"bridges\";"
'

# ── s7/s6/s5/s6.5: code rewrites + new scripts — git revert ────────────── #
rollback_surface "s7" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes build_bridge_usaspending_sba_borrower.py)."
  _r2_rm_prefix "bridges/usaspending_sba_borrower_lance" || true
'
rollback_surface "s6" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (restores parquet output path for build_bridge_sam_sba_borrower.py)."
  _r2_rm_prefix "bridges/sam_sba_borrower_lance" || true
'
rollback_surface "s6.5" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_sam_entities_lance.py)."
  _r2_rm_prefix "sam_gov/entities_lance" || true
'
rollback_surface "s5" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (restores parquet output path for build_bridge_pdl_sba_borrower.py)."
  _r2_rm_prefix "bridges/pdl_sba_borrower_lance" || true
'

# ── s4/s3/s2/s1: new Lance-emit scripts — git revert + R2 cleanup ──────── #
rollback_surface "s4" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_pdl_free_companies_lance.py)."
  _r2_rm_prefix "pdl/free_companies_lance" || true
'
rollback_surface "s3" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_sba_lenders_lance.py)."
  _r2_rm_prefix "sba/lenders_lance" || true
'
rollback_surface "s2" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_sba_borrowers_lance.py)."
  _r2_rm_prefix "sba/borrowers_lance" || true
'
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes emit_sba_loans_lance.py)."
  _r2_rm_prefix "sba/loans_lance" || true
'

echo ""
echo "Rollback complete (manual git-revert steps printed above; R2 + Polaris cleanup attempted)."
