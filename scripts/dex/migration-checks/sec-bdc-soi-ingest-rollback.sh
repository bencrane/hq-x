#!/usr/bin/env bash
# Rollback harness for cycle `sec-bdc-soi-ingest` (2026-05-20).
#
# Authored 2026-05-20 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-20-sec-bdc-soi-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/sec-bdc-soi-ingest-rollback.sh
#
# Reverse-order rollback per directive surface graph:
#   s9 → s8 → s7 → s6 → s5 → s4 → s3 → s2 → s1
# (r1/e1/e2 are verify-only — no rollback; included only as data-artifact
#  cleanup folded into s9.)
#
# Code + migration surfaces are forward-only (migrations/README.md §"Policy"):
# rollback = `git revert <merge-SHA>`. Those rows produce ECHO-ONLY advisories;
# the harness does NOT auto-issue git revert. Surface rollback semantics:
#
#   Surface  External effect rolled back here          | Code revert needed?
#   s1       (none — DDL is forward-only)              | YES (git revert merge-SHA)
#            optional DELETE catalog row + note that   |
#            the view recreation reverts with the SQL  |
#   s2       (none — CREATE TABLE forward-only)        | YES (git revert merge-SHA)
#            optional DROP only with --allow-data-loss |
#   s3       aws s3 rm sec-bdc/soi/ + sec-bdc/txt/ +   | YES (git revert merge-SHA)
#            sec-bdc/<datasets tables>/ (R2 raw)       |
#   s4       aws s3 rm sec-bdc/soi-parsed/ (R2 parsed) | YES (git revert merge-SHA)
#   s5       aws s3 rm polaris-warehouse/sec_bdc/      | YES (git revert merge-SHA)
#            soi_lance/ (the Lance dataset)            |
#   s6       (none — code-only change)                 | YES (git revert merge-SHA)
#   s7       Polaris DELETE generic-table soi_lance    | no
#   s8       (none — code-only change)                 | YES (git revert merge-SHA)
#   s9       modal app stop data-engine-x-sec-bdc-soi  | no
#
# CRITICAL SAFETY: this script requires explicit --surface or --all to avoid an
# accidental "wipe everything." Default is print-help-and-exit. R2 deletes
# (s3/s4/s5) and the Postgres catalog/table DROP (s1/s2) require --allow-data-loss.
#
# Usage:
#   ./sec-bdc-soi-ingest-rollback.sh --surface s9
#   ./sec-bdc-soi-ingest-rollback.sh --all                       # all 9 reverse-order
#   ./sec-bdc-soi-ingest-rollback.sh --all --allow-data-loss     # also wipe R2 + DROP Postgres
#   ./sec-bdc-soi-ingest-rollback.sh --surface s5 --allow-data-loss
#
# Doppler idiom per CLAUDE.md §"Doppler shell gotcha": doppler run -- bash -c '...'

set -euo pipefail

# --- locate canonical hq-all checkout + source DEX helpers --------------- #
if [[ -n "${HQ_ALL_ROOT:-}" && -f "$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
  export DEX_LIB_PATH="$HQ_ALL_ROOT/apps/data-engine-x/scripts/_lib/dex.sh"
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

APP_DIR="$HQ_ALL_ROOT/apps/data-engine-x"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

# --- CLI parsing --------------------------------------------------------- #
SURFACE_FILTER=""
REPO_FILTER=""
ROLLBACK_ALL=0
ALLOW_DATA_LOSS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    --all)     ROLLBACK_ALL=1; shift ;;
    --allow-data-loss) ALLOW_DATA_LOSS=1; shift ;;
    --help|-h)
      sed -n '2,52p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$SURFACE_FILTER" && "$ROLLBACK_ALL" -eq 0 ]]; then
  echo "FAIL: must pass --surface <id> or --all." >&2
  echo "      See --help for safety semantics." >&2
  exit 2
fi

echo "==> Rolling back sec-bdc-soi-ingest (surface=${SURFACE_FILTER:-ALL} repo=${REPO_FILTER:-all} allow_data_loss=$ALLOW_DATA_LOSS)"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

# repo for every surface in this single-repo cycle.
SURFACE_REPO="data-engine-x"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_SEC_BDC_PREFIX="sec-bdc"
MODAL_APP_NAME="data-engine-x-sec-bdc-soi"
POLARIS_NAMESPACE="sec_bdc"
POLARIS_TABLE="soi_lance"

# Helper: emit a code-revert advisory (script does NOT auto-issue git revert).
_emit_code_revert() {
  local surface="$1" path_hint="$2"
  echo "    ADVISORY ($surface): forward-only surface — rollback is git revert of the merge commit."
  echo "      git -C $HQ_ALL_ROOT log --oneline --all -- $path_hint | head -5"
  echo "      git -C $HQ_ALL_ROOT revert <merge-SHA-of-PR>"
  echo "    See supabase/migrations/README.md §\"Policy\" — forward-only, revert-as-rollback."
  return 0
}

# Helper: Polaris generic-table DELETE (token + DELETE in one doppler bash -c).
# Per POLARIS-CATALOG-CONVENTIONS §3: 200/204/404 all count as success.
_polaris_delete_table() {
  local table="$1"
  cat <<'BASH_EOF'
    set -e
    TOKEN=$(curl -s -X POST "$POLARIS_PUBLIC_URL/api/catalog/v1/oauth/tokens" \
      -d "grant_type=client_credentials&client_id=$POLARIS_ROOT_PRINCIPAL_ID&client_secret=$POLARIS_ROOT_PRINCIPAL_SECRET&scope=PRINCIPAL_ROLE:ALL" \
      | jq -r .access_token)
    if [[ -z "$TOKEN" || "$TOKEN" = "null" ]]; then
      echo "FAIL: could not obtain Polaris token"
      exit 1
    fi
BASH_EOF
  cat <<BASH_EOF
    HTTP=\$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \\
      "\$POLARIS_PUBLIC_URL/api/catalog/polaris/v1/\$POLARIS_DEFAULT_CATALOG_NAME/namespaces/$POLARIS_NAMESPACE/generic-tables/$table" \\
      -H "Authorization: Bearer \$TOKEN")
    case "\$HTTP" in
      200|204|404) echo "DELETE $table → HTTP \$HTTP (ok)" ;;
      *) echo "FAIL: DELETE $table → HTTP \$HTTP"; exit 1 ;;
    esac
BASH_EOF
}

# ====================================================================== #
# s9 (reverse first) — Modal stop
# ====================================================================== #
# Also folds in r1/e1/e2 data-artifact cleanup note: the R2 raw + Lance data
# those checks verify is wiped by s3/s4/s5 below — no separate r1/e1/e2 step.
rollback_surface "s9" "$SURFACE_REPO" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"'
  " || echo "    (modal app may already be stopped / never deployed — non-fatal)"
'

# ====================================================================== #
# s8 (reverse) — Modal app code (advisory: git revert merge-SHA)
# ====================================================================== #
rollback_surface "s8" "$SURFACE_REPO" '_emit_code_revert "s8" "apps/data-engine-x/modal/sec_bdc_soi_app.py"'

# ====================================================================== #
# s7 (reverse) — Polaris generic-table DELETE
# ====================================================================== #
rollback_surface "s7" "$SURFACE_REPO" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table "$POLARIS_TABLE")'
"

# ====================================================================== #
# s6 (reverse) — lance_views.py entry (advisory: git revert merge-SHA)
# ====================================================================== #
rollback_surface "s6" "$SURFACE_REPO" '_emit_code_revert "s6" "apps/data-engine-x/app/services/lance_views.py"'

# ====================================================================== #
# s5 (reverse) — Lance dataset aws s3 rm + code revert advisory
# ====================================================================== #
# R2 cred-mapping AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID is required so the AWS CLI
# authenticates against R2 (precedent sec-dera-fsds-ingest-rollback.sh).
rollback_surface "s5" "$SURFACE_REPO" '
  _emit_code_revert "s5" "apps/data-engine-x/scripts/run_sec_bdc_soi_lance_emit.py"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    Wiping Lance dataset polaris-warehouse/sec_bdc/soi_lance/ (--allow-data-loss)"
    doppler run --project hq-all --config prd -- bash -c "
      AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_bdc/soi_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
    "
  else
    echo "    SKIP: Lance-dataset wipe requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s4 (reverse) — R2 parsed-attributes prefix aws s3 rm + code revert advisory
# ====================================================================== #
rollback_surface "s4" "$SURFACE_REPO" '
  _emit_code_revert "s4" "apps/data-engine-x/scripts/parse_sec_bdc_soi_html.py"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    Wiping R2 prefix sec-bdc/soi-parsed/ (--allow-data-loss)"
    doppler run --project hq-all --config prd -- bash -c "
      AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET"'/'"$R2_SEC_BDC_PREFIX"'/soi-parsed/ --recursive --endpoint-url=\$R2_ENDPOINT
    "
  else
    echo "    SKIP: R2 soi-parsed/ wipe requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s3 (reverse) — R2 raw prefixes aws s3 rm + code revert advisory
# ====================================================================== #
# Wipes the entire sec-bdc/ raw tree EXCEPT soi-parsed/ (which s4 owns) — i.e.
# soi/ + txt/ + every datasets/ table prefix (sub/tag/num/pre/cal/non). A blanket
# `aws s3 rm sec-bdc/ --recursive` would also catch soi-parsed/; the per-prefix
# loop keeps surface ownership clean if s4 is rolled back independently. Run
# AFTER s4 in --all mode so a blanket-vs-scoped overlap is moot.
# ops.sec_bdc_soi_ingest_runs rows remain in Postgres as the audit trail.
rollback_surface "s3" "$SURFACE_REPO" '
  _emit_code_revert "s3" "apps/data-engine-x/scripts/run_sec_bdc_soi_r2_ingest.py"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    Wiping R2 raw prefixes under sec-bdc/ (soi, txt, datasets tables) — keeps soi-parsed/ for s4 (--allow-data-loss)"
    doppler run --project hq-all --config prd -- bash -c "
      set -e
      for TBL in soi txt sub tag num pre cal non; do
        AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
        aws s3 rm s3://'"$R2_BUCKET"'/'"$R2_SEC_BDC_PREFIX"'/\$TBL/ --recursive --endpoint-url=\$R2_ENDPOINT || true
      done
    "
  else
    echo "    SKIP: R2 raw wipe requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s2 (reverse) — ops.sec_bdc_soi_ingest_runs ledger table
# ====================================================================== #
# Forward-only per migrations/README.md §"Policy" — DROP TABLE is NOT the
# rollback. git revert merge-SHA + future re-merge is idempotent via
# CREATE TABLE IF NOT EXISTS. A DROP is offered ONLY under --allow-data-loss
# for the case where the operator wants the schema fully clean pre-re-merge.
# NOTE: s2 also recreated ops.data_source_catalog_status (the view branch lives
# in s2's migration, not s1's — see ## Audit plan §"Ordering correction"). The
# git revert of s2's merge restores the view to its pre-cycle definition.
rollback_surface "s2" "$SURFACE_REPO" '
  _emit_code_revert "s2" "apps/data-engine-x/supabase/migrations/*_sec_bdc_soi_ingest_runs.sql"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    DROP ops.sec_bdc_soi_ingest_runs (--allow-data-loss)"
    dex_psql_ddl "DROP TABLE IF EXISTS ops.sec_bdc_soi_ingest_runs CASCADE"
  else
    echo "    SKIP: DROP TABLE requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s1 (reverse last) — ops.data_source_catalog row sec_bdc_soi
# ====================================================================== #
# Forward-only. git revert merge-SHA re-applies idempotently
# (INSERT ... ON CONFLICT (source_slug) DO NOTHING). The catalog-row DELETE is
# offered ONLY under --allow-data-loss. The data_source_catalog_status view
# branch is reverted by s2's revert (the branch SQL lives in s2's migration).
rollback_surface "s1" "$SURFACE_REPO" '
  _emit_code_revert "s1" "apps/data-engine-x/supabase/migrations/*_sec_bdc_soi_data_source.sql"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    DELETE ops.data_source_catalog row source_slug=sec_bdc_soi (--allow-data-loss)"
    dex_psql_ddl "DELETE FROM ops.data_source_catalog WHERE source_slug='\''sec_bdc_soi'\''"
  else
    echo "    SKIP: catalog-row DELETE requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
