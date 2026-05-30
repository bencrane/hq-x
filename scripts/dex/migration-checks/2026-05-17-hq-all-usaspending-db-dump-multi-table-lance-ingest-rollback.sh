#!/usr/bin/env bash
# Rollback harness for /scope cycle hq-all-usaspending-db-dump-multi-table-lance-ingest.
#
# Frozen by validator (2026-05-17 UTC). Mirrors
# hq-all-usaspending-subawards-lance-emit-rollback.sh (direct precedent — PR #471).
#
# Runs surface rollbacks in REVERSE order (s8 -> s7 -> s6 -> s5 -> s4 -> s3 ->
# s2 -> s1). All surfaces are forward-only per apps/data-engine-x/supabase/
# migrations/README.md §"Policy" — code/migration/endpoint rollbacks are
# `git revert <merge-SHA>` on hq-all main. ON CONFLICT DO UPDATE + IF NOT
# EXISTS make re-apply idempotent. Polaris DELETE is idempotent (200/204/404).
# Railway rollback: `railway redeploy --service data-engine-x --deployment-id <prior-id>`.
#
# Note: NONE of the surfaces here are destructive — all writes are NEW data
# (1 new Modal app, 1 new parser library, 1 new CLI wrapper, 1 new ledger
# table, 12 new ops.data_sources rows, 12 new Polaris registrations, 12 new
# LanceView entries, 12 new Lance datasets). The rollback gate per /scope
# SKILL.md is satisfied for every surface.
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

# 12 target tables — same list as the verifier
TARGET_TABLES=(
  subaward awards transaction_normalized transaction_fpds transaction_fabs
  recipient_lookup recipient_profile references_location toptier_agency
  subtier_agency agency references_cfda
)

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

# --- R2 prefix rm helper (best-effort orphan cleanup; safe to fail) ------ #
# Usage: _r2_rm_prefix <prefix-after-bucket>
_r2_rm_prefix() {
  local prefix="$1"
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://dex-raw-landing-zone/$prefix/ \
        --recursive --endpoint-url \$R2_ENDPOINT
  "
}

# REVERSE order (s8 -> s1) — most-recent surface first.

# ── s8: Railway data-engine-x — redeploy prior deployment id ─────────── #
rollback_surface "s8" "hq-all" '
  echo "manual (per apps/data-engine-x/CLAUDE.md §\"Deploy targets\"):"
  echo "  Lookup prior deployment id:"
  echo "    cd $HQ_ALL_ROOT/apps/data-engine-x && doppler run --project hq-all --config prd -- railway deployment list --service data-engine-x --limit 2 --json | jq -r \".[1].id\""
  echo "  Redeploy prior:"
  echo "    cd $HQ_ALL_ROOT/apps/data-engine-x && doppler run --project hq-all --config prd -- railway redeploy --service data-engine-x --deployment-id <prior-id>"
  echo ""
  echo "  Preferred path: git -C $HQ_ALL_ROOT revert <merge-SHA> on main; Railway auto-deploys the prior commit."
'

# ── s7: CLI wrapper — git revert ────────────────────────────────────── #
rollback_surface "s7" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes scripts/run_usaspending_db_dump_cycle.py)."
'

# ── s6: Polaris generic-table registrations — DELETE x 12 ────────────── #
for tbl in "${TARGET_TABLES[@]}"; do
  rollback_surface "s6-${tbl}" "hq-all" "
    _polaris_delete_table usaspending ${tbl}_lance || true
  "
done

# ── s5: ops.data_sources migration — git revert (forward-only) ──────── #
rollback_surface "s5" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes supabase/migrations/{ts}_usaspending_lance_data_sources.sql)."
  echo "  Forward-only per migrations/README.md §\"Policy\". ON CONFLICT (display_name) DO UPDATE makes re-apply idempotent."
  echo "  Note: the 12 ops.data_sources rows are NOT auto-deleted on revert. To manually retire them:"
  echo "    UPDATE ops.data_sources SET status='"'"'retired'"'"', retired_at=NOW() WHERE display_name LIKE '"'"'usaspending_%_lance'"'"' AND display_name IN ('"'"'usaspending_subaward_lance'"'"', '"'"'usaspending_awards_lance'"'"', '"'"'usaspending_transaction_normalized_lance'"'"', '"'"'usaspending_transaction_fpds_lance'"'"', '"'"'usaspending_transaction_fabs_lance'"'"', '"'"'usaspending_recipient_lookup_lance'"'"', '"'"'usaspending_recipient_profile_lance'"'"', '"'"'usaspending_references_location_lance'"'"', '"'"'usaspending_toptier_agency_lance'"'"', '"'"'usaspending_subtier_agency_lance'"'"', '"'"'usaspending_agency_lance'"'"', '"'"'usaspending_references_cfda_lance'"'"');"
'

# ── s4: ledger migration — git revert + manual table-keep note ───────── #
rollback_surface "s4" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes supabase/migrations/{ts}_usaspending_db_dump_ingest_ledger.sql)."
  echo "  Forward-only. Table stays per CREATE TABLE IF NOT EXISTS idempotency; rows persist as audit history."
  echo "  To drop the table (operator-fired only; destroys audit history):"
  echo "    DROP TABLE IF EXISTS ops.usaspending_db_dump_ingest_runs;"
'

# ── s3: lance_views.py — git revert (12 LanceView entries appended) ──── #
rollback_surface "s3" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes 12 usaspending_<table>_lance_raw LanceView entries from apps/data-engine-x/app/services/lance_views.py)."
  echo "  Append-only diff; revert is clean. All 12 new entries carry register_at_boot=False so the live FastAPI boot path is unaffected pre-revert."
'

# ── s2: parser library + tests — git revert + R2 cleanup x 12 ────────── #
rollback_surface "s2" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes scripts/_lib/pg_copy_text_parser.py + tests/test_pg_copy_text_parser.py)."
'

# ── s1: Modal app + Lance datasets — git revert + R2 cleanup x 12 ───── #
rollback_surface "s1" "hq-all" '
  echo "manual: git -C $HQ_ALL_ROOT revert <merge-SHA> (removes modal/usaspending_db_dump_to_r2_and_lance.py)."
  echo "  Cleanup 12 Lance dataset prefixes (best-effort; idempotent):"
'
for tbl in "${TARGET_TABLES[@]}"; do
  rollback_surface "s1-r2-${tbl}" "hq-all" "
    _r2_rm_prefix polaris-warehouse/usaspending/${tbl}_lance || true
  "
done

# ── s1 extra: Modal Volume cache cleanup (optional; operator-fired) ───── #
rollback_surface "s1-volume-cache" "hq-all" '
  echo "optional: Modal Volume usaspending-db-dump-cache holds the downloaded pg_dump archive."
  echo "  If the dump byte stream is suspect / corrupted, operator can delete via:"
  echo "    modal volume delete usaspending-db-dump-cache --confirm"
  echo "  Next cycle will re-download. NB: 30-50GB download is non-trivial — only delete if necessary."
'

echo ""
echo "Rollback complete (manual git-revert steps printed; R2 + Polaris cleanup attempted)."
echo ""
echo "NB: All 8 surfaces are NEW data — none mutate or destroy prior state. Re-application after revert is idempotent by construction (IF NOT EXISTS / ON CONFLICT DO UPDATE / GET-first Polaris / mode='overwrite' Lance write)."
