#!/usr/bin/env bash
# Rollback harness for cycle `sec-dera-form-d-ingest` (2026-05-18).
#
# Authored 2026-05-18 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-18-sec-dera-form-d-ingest.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/2026-05-18-sec-dera-form-d-ingest-rollback.sh
#
# Reverse-order rollback per directive phase graph:
#   mod1 → p6..p1 → e6..e1 → r1 → c9..c1 → m2 → m1
#
# Code + migration surfaces use `git revert <merge-SHA>` — those rows in the
# table below produce ECHO-ONLY instructions to the operator; the harness does
# NOT auto-issue git revert. Surface rollback semantics:
#
#   Surface  External effect rolled back here     | Code revert needed?
#   m1       (none — DDL is forward-only)         | YES (git revert merge-SHA)
#   m2       optional DELETE 7 ops.data_sources   | YES (git revert merge-SHA)
#            rows (only with --allow-data-loss)
#   c1-c9    (none — code-only changes)           | YES (git revert merge-SHA)
#   r1       aws s3 rm sec-dera/form-d/ -recursive| no
#   e1-e6    aws s3 rm <lance prefix> -recursive  | no
#   p1-p6    Polaris DELETE Generic Table         | no
#   mod1     modal app stop                       | no
#
# CRITICAL SAFETY: this script requires explicit --surface or --all to avoid
# accidental "wipe everything." Default is print-help-and-exit.
#
# Usage:
#   ./2026-05-18-sec-dera-form-d-ingest-rollback.sh --surface mod1
#   ./2026-05-18-sec-dera-form-d-ingest-rollback.sh --all                # all 25 in reverse order
#   ./2026-05-18-sec-dera-form-d-ingest-rollback.sh --all --allow-data-loss
#       # required to DELETE the 7 ops.data_sources rows (m2 surface)
#   ./2026-05-18-sec-dera-form-d-ingest-rollback.sh --surface m2 --allow-data-loss
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
ROLLBACK_ALL=0
ALLOW_DATA_LOSS=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --all)     ROLLBACK_ALL=1; shift ;;
    --allow-data-loss) ALLOW_DATA_LOSS=1; shift ;;
    --help|-h)
      sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
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

echo "==> Rolling back sec-dera-form-d-ingest (surface=${SURFACE_FILTER:-ALL} allow_data_loss=$ALLOW_DATA_LOSS)"

FAIL_COUNT=0
PASS_COUNT=0
SKIP_COUNT=0

run_rollback() {
  local id="$1" cmd="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    SKIP_COUNT=$((SKIP_COUNT+1))
    return 0
  fi
  echo "-- $id: ROLLING BACK"
  if eval "$cmd"; then
    echo "-- $id: PASS"
    PASS_COUNT=$((PASS_COUNT+1))
  else
    echo "-- $id: FAIL" >&2
    FAIL_COUNT=$((FAIL_COUNT+1))
  fi
}

# --- pinned constants per audit ----------------------------------------- #
R2_BUCKET="dex-raw-landing-zone"
R2_FORM_D_PREFIX="sec-dera/form-d"
LANCE_DATASETS=(
  "form_d_submission_lance"
  "form_d_issuers_lance"
  "form_d_offering_lance"
  "form_d_recipients_lance"
  "form_d_related_persons_lance"
  "form_d_signatures_lance"
)
MODAL_APP_NAME="data-engine-x-sec-dera-form-d"
POLARIS_NAMESPACE="sec_dera"

# Helper: emit a code-revert advisory (script does NOT auto-issue git revert).
_emit_code_revert() {
  local surface="$1" path_hint="$2"
  echo "    ADVISORY: Code-only surface. Run:"
  echo "      git -C $HQ_ALL_ROOT log --oneline --all -- $path_hint | head -5"
  echo "      git -C $HQ_ALL_ROOT revert <merge-SHA-of-PR>"
  echo "    See migrations/README.md §\"Policy\" — forward-only, revert-as-rollback."
  return 0
}

# ====================================================================== #
# Phase 6 (reverse) — Modal stop
# ====================================================================== #

# ── mod1: stop the Modal app ──────────────────────────────────────────── #
run_rollback "mod1" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"'
  " || echo "    (modal app may already be stopped — non-fatal)"
'

# ====================================================================== #
# Phase 5 (reverse) — Polaris Generic Tables DELETE
# ====================================================================== #
# Per POLARIS-CATALOG-CONVENTIONS §8: DELETE Generic Table.
# Token + DELETE in one doppler bash -c call per surface.

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

run_rollback "p6" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_signatures_lance)'
"

run_rollback "p5" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_related_persons_lance)'
"

run_rollback "p4" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_recipients_lance)'
"

run_rollback "p3" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_offering_lance)'
"

run_rollback "p2" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_issuers_lance)'
"

run_rollback "p1" "
  doppler run --project hq-all --config prd -- bash -c '$(_polaris_delete_table form_d_submission_lance)'
"

# ====================================================================== #
# Phase 4 (reverse) — Lance datasets aws s3 rm
# ====================================================================== #
# R2 cred-mapping: AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY
# are required so the AWS CLI can authenticate against R2 (per precedent
# scripts/migration-checks/abs-15g-ingest.sh + ucc-ca-master-ingest.sh).
# Without this mapping, every `aws s3 rm` aborts with "Unable to locate credentials".

run_rollback "e6" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_signatures_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

run_rollback "e5" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_related_persons_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

run_rollback "e4" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_recipients_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

run_rollback "e3" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_offering_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

run_rollback "e2" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_issuers_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

run_rollback "e1" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/polaris-warehouse/sec_dera/form_d_submission_lance/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

# ====================================================================== #
# Phase 3 (reverse) — R2 backfill aws s3 rm
# ====================================================================== #
# Wipes the entire sec-dera/form-d/ prefix. ops.sec_dera_form_d_r2_ingest_runs
# rows remain in Postgres as the audit trail of "what was here before rollback."

run_rollback "r1" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
    aws s3 rm s3://'"$R2_BUCKET"'/'"$R2_FORM_D_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT
  "
'

# ====================================================================== #
# Phase 2 (reverse) — Code surfaces (advisory: git revert merge-SHA)
# ====================================================================== #

run_rollback "c9" '_emit_code_revert "c9" "apps/data-engine-x/app/services/lance_views.py"'
run_rollback "c8" '_emit_code_revert "c8" "apps/data-engine-x/scripts/run_sec_dera_form_d_signatures_lance_emit.py"'
run_rollback "c7" '_emit_code_revert "c7" "apps/data-engine-x/scripts/run_sec_dera_form_d_related_persons_lance_emit.py"'
run_rollback "c6" '_emit_code_revert "c6" "apps/data-engine-x/scripts/run_sec_dera_form_d_recipients_lance_emit.py"'
run_rollback "c5" '_emit_code_revert "c5" "apps/data-engine-x/scripts/run_sec_dera_form_d_offering_lance_emit.py"'
run_rollback "c4" '_emit_code_revert "c4" "apps/data-engine-x/scripts/run_sec_dera_form_d_issuers_lance_emit.py"'
run_rollback "c3" '_emit_code_revert "c3" "apps/data-engine-x/scripts/run_sec_dera_form_d_submission_lance_emit.py apps/data-engine-x/scripts/_lib/lance_emit.py"'
run_rollback "c2" '_emit_code_revert "c2" "apps/data-engine-x/modal/sec_dera_form_d_app.py"'
run_rollback "c1" '_emit_code_revert "c1" "apps/data-engine-x/scripts/run_sec_dera_form_d_r2_ingest.py"'

# ====================================================================== #
# Phase 1 (reverse) — Migration rollback
# ====================================================================== #

# ── m2: optional DELETE of 7 ops.data_sources rows ────────────────────── #
# Requires --allow-data-loss. Otherwise prints code-revert advisory only.
run_rollback "m2" '
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    Deleting 7 ops.data_sources rows (--allow-data-loss)"
    dex_psql_ddl "DELETE FROM ops.data_sources WHERE display_name LIKE '\''sec_dera.form_d_%_lance'\'' OR display_name = '\''sec-dera/form-d'\''"
  else
    echo "    SKIP: m2 DELETE requires --allow-data-loss. Code-only rollback advisory:"
    _emit_code_revert "m2" "apps/data-engine-x/supabase/migrations/*_sec_dera_form_d_data_sources_7_rows.sql"
  fi
'

# ── m1: ops.sec_dera_form_d_r2_ingest_runs is forward-only ────────────── #
# Per migrations/README.md §"Policy" — DROP TABLE is NOT done as rollback.
# Code revert via git revert merge-SHA + future re-merge is idempotent via
# CREATE TABLE IF NOT EXISTS. Audit ledger rows are preserved.
run_rollback "m1" '_emit_code_revert "m1" "apps/data-engine-x/supabase/migrations/*_sec_dera_form_d_r2_ingest_runs.sql"'

# ====================================================================== #
echo ""
echo "==> SUMMARY: $PASS_COUNT pass / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
