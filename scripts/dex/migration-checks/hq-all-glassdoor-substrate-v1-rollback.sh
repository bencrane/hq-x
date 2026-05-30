#!/usr/bin/env bash
# Rollback harness for /scope cycle hq-all-glassdoor-substrate-v1.
#
# Refined by Stage 3.A audit subagent (2026-05-17 UTC) on top of the Stage 2
# validator scaffold. Mirrors hq-all-jsearch-ae-end-to-end-rollback.sh shape.
#
# Surface rollback order: REVERSE (s10 → s9 → s8 → s7 → s6 → s5 → s4 → s3 → s2 → s1).
#
# Per-surface rollback strategy:
#   s10 deploy      → `railway redeploy --service data-engine-x --deployment-id <prior-id>`
#                     (prior-id via `railway deployment list --service data-engine-x
#                     --limit 2 --json | jq -r '.[1].id'`). Alternative: git revert
#                     <merge-SHA> on main triggers auto-redeploy of the prior commit.
#   s9  config      → DELETE /api/catalog/polaris/v1/{catalog}/namespaces/{ns}/
#                     generic-tables/{name} × 5 (idempotent — 200/204/404 acceptable
#                     per inventory/POLARIS-CATALOG-CONVENTIONS.md §3).
#   s8  code        → git revert <merge-SHA> (LanceView append diff reverts clean;
#                     register_at_boot=False so no boot effect either way).
#   s7  code        → git revert <merge-SHA>; R2 prefix bridges/pdl_glassdoor_lance/
#                     orphaned (best-effort `aws s3 rm --recursive`). ops.bridges +
#                     ops.bridge_generation_runs rows STAY (forensic record).
#                     CRITICAL: do NOT touch ops.match_methods or
#                     ops.match_method_versions — domain_exact v1.0.0 is REUSED
#                     across 6+ existing bridges (FMCSA-PDL, SAM-PDL, etc.).
#   s6  code        → git revert <merge-SHA>; 4 Lance dataset R2 prefixes orphaned.
#   s5  code        → git revert <merge-SHA>; 4 R2 parquet prefixes orphaned.
#                     ops.glassdoor_ingest_runs rows for endpoint='r2_snapshot' STAY.
#   s4  code        → git revert <merge-SHA>; Postgres rows in entities.source_*
#                     STAY (idempotent on PK; re-running the cohort is safe).
#                     ops.glassdoor_ingest_runs run rows STAY.
#   s3  code        → git revert <merge-SHA>; the router import + main.py registration
#                     is reverted in the same commit.
#   s2  code        → git revert <merge-SHA>; service module reverted.
#   s1  migration   → git revert <merge-SHA>. Forward-only per migrations/README.md
#                     §"Policy"; IF NOT EXISTS makes re-application after revert
#                     idempotent. Tables stay (DROP not auto-issued); 5 data_sources
#                     rows STAY (operator may manually flip to status='retired' —
#                     printed for awareness).
#
# Note: NONE of the surfaces are destructive.
#   - Migration is additive (CREATE TABLE IF NOT EXISTS + INSERT ON CONFLICT DO UPDATE).
#   - Service + router + scripts are all NEW files.
#   - Lance writes use mode='overwrite' = idempotent on re-emit.
#   - Polaris registrations are GET-first idempotent.
#   - Postgres ledger rows survive forever as forensic record.
#
# Rollback gate (per /scope SKILL.md) is SATISFIED for every surface.
#
# Usage:
#   ./hq-all-glassdoor-substrate-v1-rollback.sh                  # print rollback steps for all surfaces
#   ./hq-all-glassdoor-substrate-v1-rollback.sh --surface s9     # one surface
#   POLARIS_DELETE=1 ./hq-all-glassdoor-substrate-v1-rollback.sh # actually DELETE Polaris entries
#   R2_RM=1 ./hq-all-glassdoor-substrate-v1-rollback.sh          # actually `aws s3 rm --recursive`

set -uo pipefail

# --- locate canonical hq-all checkout + source helpers ------------------- #
# Nested-worktree mode: when invoked from within a worktree, prefer the
# worktree path; otherwise fall back to the parent checkout.
_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_worktree_root="${_script_dir%/apps/data-engine-x/scripts/migration-checks}"
if [[ -f "$_worktree_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$_worktree_root/apps/data-engine-x/scripts/_lib/dex.sh"
  HQ_ALL_ROOT="$_worktree_root"
else
  for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
    if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
      export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
      HQ_ALL_ROOT="$_root"
      break
    fi
  done
fi
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
if [[ -z "${MERGE_SHA:-}" ]]; then
  echo "   (MERGE_SHA env var unset — git-revert commands will print <merge-SHA> as a placeholder.)"
fi

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

MERGE_SHA_PLACEHOLDER="${MERGE_SHA:-<merge-SHA>}"

# REVERSE order (s10 → s1) — most-recent surface first.

# ── s10: Railway data-engine-x — redeploy prior deployment id ──────────── #
rollback_surface "s10" "hq-all" '
  echo "manual (per apps/data-engine-x/CLAUDE.md §\"Deploy targets\"):"
  echo "  Lookup prior deployment id:"
  echo "    doppler run --project hq-all --config prd -- bash -c \\"
  echo "      \"railway deployment list --service data-engine-x --limit 2 --json | jq -r .[1].id\""
  echo "  Redeploy prior:"
  echo "    doppler run --project hq-all --config prd -- bash -c \\"
  echo "      \"railway redeploy --service data-engine-x --deployment-id <prior-id>\""
  echo ""
  echo "  Preferred path: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' on main; Railway auto-deploys the prior commit."
'

# ── s9: Polaris generic-table registrations × 5 — DELETE ──────────────── #
# Idempotent (200/204/404 all acceptable per POLARIS-CATALOG-CONVENTIONS.md §3).
rollback_surface "s9" "hq-all" '
  echo "Polaris DELETE × 5 (idempotent; safe to re-run):"
  if [[ -n "${POLARIS_DELETE:-}" ]]; then
    _polaris_delete_table "glassdoor" "company_search_lance"      || true
    _polaris_delete_table "glassdoor" "company_overview_lance"    || true
    _polaris_delete_table "glassdoor" "company_salaries_lance"    || true
    _polaris_delete_table "glassdoor" "company_salaries_v2_lance" || true
    _polaris_delete_table "bridges"   "pdl_glassdoor_lance"       || true
  else
    echo "   (POLARIS_DELETE unset — printing commands only)"
    echo "   _polaris_delete_table glassdoor company_search_lance"
    echo "   _polaris_delete_table glassdoor company_overview_lance"
    echo "   _polaris_delete_table glassdoor company_salaries_lance"
    echo "   _polaris_delete_table glassdoor company_salaries_v2_lance"
    echo "   _polaris_delete_table bridges   pdl_glassdoor_lance"
  fi
'

# ── s8: lance_views.py — git revert (5 LanceView entries appended) ───── #
rollback_surface "s8" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes 5 LanceView entries appended to apps/data-engine-x/app/services/lance_views.py)."
  echo "  Append-only diff; revert is clean. All 5 new entries carry register_at_boot=False so the live FastAPI boot path is unaffected pre-revert."
'

# ── s7: pdl_glassdoor_lance Pattern B bridge — git revert + R2 + ops.bridges ── #
rollback_surface "s7" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes build_bridge_pdl_glassdoor_lance.py)."
  if [[ -n "${R2_RM:-}" ]]; then
    _r2_rm_prefix "polaris-warehouse/bridges/pdl_glassdoor_lance" || true
  else
    echo "  (R2_RM unset — R2 prefix kept; cost ~\$0/mo. Set R2_RM=1 to recursively remove.)"
  fi
  echo "  NB: ops.bridges + ops.bridge_generation_runs rows for pdl_glassdoor_lance are LEFT IN PLACE (forensic record)."
  echo "  CRITICAL: do NOT touch ops.match_methods or ops.match_method_versions —"
  echo "    domain_exact v1.0.0 is REUSED across 6+ existing bridges (FMCSA-PDL, SAM-PDL, UCC-PDL, JSearch-PDL)."
  echo "    s7 only registered the bridge instance row, not the match-method version row."
  echo "  To manually retire the bridge row (only if you want full unwind):"
  echo "    doppler run --project hq-all --config prd -- bash -c \"psql \\\"\$DEX_DB_URL_DIRECT\\\" -c \\\"DELETE FROM ops.bridge_generation_runs WHERE bridge_id IN (SELECT bridge_id FROM ops.bridges WHERE bridge_name='\''pdl_glassdoor_lance'\''); DELETE FROM ops.bridges WHERE bridge_name='\''pdl_glassdoor_lance'\'';\\\"\""
'

# ── s6: 4 × Lance emit scripts — git revert + R2 cleanup × 4 ─────────── #
rollback_surface "s6" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes 4 run_glassdoor_<endpoint>_lance_emit.py scripts)."
  if [[ -n "${R2_RM:-}" ]]; then
    _r2_rm_prefix "polaris-warehouse/glassdoor/company_search_lance"      || true
    _r2_rm_prefix "polaris-warehouse/glassdoor/company_overview_lance"    || true
    _r2_rm_prefix "polaris-warehouse/glassdoor/company_salaries_lance"    || true
    _r2_rm_prefix "polaris-warehouse/glassdoor/company_salaries_v2_lance" || true
  else
    echo "  (R2_RM unset — 4 Lance dataset R2 prefixes kept; cost ~\$0/mo. Set R2_RM=1 to recursively remove.)"
    echo "    s3://dex-raw-landing-zone/polaris-warehouse/glassdoor/{company_search,company_overview,company_salaries,company_salaries_v2}_lance/"
  fi
'

# ── s5: R2 snapshot script — git revert + R2 cleanup × 4 ─────────────── #
rollback_surface "s5" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes run_glassdoor_r2_snapshot.py)."
  if [[ -n "${R2_RM:-}" ]]; then
    _r2_rm_prefix "glassdoor/company_search"      || true
    _r2_rm_prefix "glassdoor/company_overview"    || true
    _r2_rm_prefix "glassdoor/company_salaries"    || true
    _r2_rm_prefix "glassdoor/company_salaries_v2" || true
  else
    echo "  (R2_RM unset — 4 R2 snapshot prefixes kept; cost ~\$0/mo. Set R2_RM=1 to recursively remove.)"
    echo "    s3://dex-raw-landing-zone/glassdoor/{company_search,company_overview,company_salaries,company_salaries_v2}/snapshot=YYYY-MM-DD/"
  fi
  echo "  NB: ops.glassdoor_ingest_runs rows with endpoint='\''r2_snapshot'\'' are LEFT IN PLACE (forensic record)."
'

# ── s4: resolution + hydration one-shots — git revert; Postgres rows STAY ── #
rollback_surface "s4" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes scripts/run_glassdoor_{resolution,hydration}_oneshot.py)."
  echo "  IMPORTANT: Postgres rows in entities.source_glassdoor_* stay (idempotent on PK; re-running the cohort is safe)."
  echo "  ops.glassdoor_ingest_runs rows for the 4 service endpoints also stay (forensic record of the ~50-credit cohort run)."
  echo "  Re-running the same cohort after revert is safe (ON CONFLICT DO UPDATE refreshes drifted fields only)."
'

# ── s3: glassdoor_v1 router + app/main.py registration — git revert ──── #
rollback_surface "s3" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes app/routers/glassdoor_v1.py + reverts include_router line in app/main.py)."
  echo "  Reverting the router line in main.py before redeploying the service is what actually rolls back the production URL surface."
'

# ── s2: glassdoor_ingest service module — git revert ──────────────────── #
rollback_surface "s2" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes app/services/glassdoor_ingest.py)."
  echo "  s4 + s3 both import this module, so the revert must happen together (single PR-revert covers all 3 surfaces atomically)."
'

# ── s1: migration — git revert; tables + data_sources rows STAY ─────── #
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert -m 1 '"$MERGE_SHA_PLACEHOLDER"' (removes supabase/migrations/{ts}_glassdoor_substrate_v1.sql)."
  echo "  Forward-only per migrations/README.md §\"Policy\"."
  echo "  IF NOT EXISTS makes re-application after revert idempotent."
  echo ""
  echo "  Side effects (forward-only — operator chooses whether to unwind):"
  echo "    - 4 entities.source_glassdoor_* tables remain (DROP not auto-issued)."
  echo "    - ops.glassdoor_ingest_runs table remains."
  echo "    - 5 ops.data_sources rows remain (manual retire path printed below)."
  echo ""
  echo "  Manual full-unwind (run only if you want zero downstream artifacts):"
  echo "    doppler run --project hq-all --config prd -- bash -c \"psql \\\"\$DEX_DB_URL_DIRECT\\\" -c \\\""
  echo "      UPDATE ops.data_sources SET status='\''retired'\'', retired_at=NOW()"
  echo "        WHERE display_name IN ('\''glassdoor_company_search_lance'\'',"
  echo "                               '\''glassdoor_company_overview_lance'\'',"
  echo "                               '\''glassdoor_company_salaries_lance'\'',"
  echo "                               '\''glassdoor_company_salaries_v2_lance'\'',"
  echo "                               '\''bridges_pdl_glassdoor_lance'\'');"
  echo "      DROP TABLE IF EXISTS ops.glassdoor_ingest_runs;"
  echo "      DROP TABLE IF EXISTS entities.source_glassdoor_company_salaries_v2;"
  echo "      DROP TABLE IF EXISTS entities.source_glassdoor_company_salaries;"
  echo "      DROP TABLE IF EXISTS entities.source_glassdoor_company_overview;"
  echo "      DROP TABLE IF EXISTS entities.source_glassdoor_company_search;"
  echo "    \\\"\""
'

echo ""
echo "==> Rollback steps printed (and Polaris/R2 actions taken if POLARIS_DELETE=1 / R2_RM=1)."
echo ""
echo "NB: All 10 surfaces are NEW data — none mutate or destroy prior state."
echo "    Re-application after revert is idempotent by construction:"
echo "      IF NOT EXISTS (DDL)"
echo "      ON CONFLICT (display_name) DO UPDATE (ops.data_sources)"
echo "      ON CONFLICT (PK) DO UPDATE WHERE IS DISTINCT FROM EXCLUDED (source tables)"
echo "      GET-first Polaris register"
echo "      mode='overwrite' Lance write"
echo ""
echo "Rollback gate per /scope SKILL.md: SATISFIED for every surface."
