#!/usr/bin/env bash
# Rollback harness for cycle `sec-bdc-soi-html-parse-v2` (2026-05-22).
#
# Authored 2026-05-22 by Stage 3.A migration auditor per directive:
#   /Users/benjamincrane/Desktop/hq/directives/2026-05-22-sec-bdc-soi-html-parse-v2.md
#
# CANONICAL IN-REPO PATH (executor MUST copy this file into the hq-all checkout
# when opening the PR):
#   ~/hq-all/apps/data-engine-x/scripts/migration-checks/2026-05-22-sec-bdc-soi-html-parse-v2-rollback.sh
#
# Reverse-order rollback per directive surface graph:
#   s7 → s6 → s5 → s4 → s3 → s2 → s1
# (r1/e1-e6 are verify-only — no rollback; data-artifact cleanup folded into s3.)
#
# Code + migration surfaces are forward-only (migrations/README.md §"Policy"):
# rollback = `git revert <merge-SHA>`. Those rows produce ECHO-ONLY advisories;
# the harness does NOT auto-issue git revert. Surface rollback semantics:
#
#   Surface  External effect rolled back here          | Code revert needed?
#   s1       (none — DDL is forward-only)              | YES (git revert merge-SHA)
#            optional DELETE catalog row                |
#   s2       (none — CREATE TABLE forward-only)        | YES (git revert merge-SHA)
#            optional DROP only with --allow-data-loss |
#   s3       aws s3 rm sec-bdc/soi-parsed-v2/ (R2)     | YES (git revert merge-SHA)
#            Note: v1 sec-bdc/soi-parsed/ stays intact.|
#   s4       (none — classifier module, code-only)    | YES (git revert merge-SHA)
#   s5       (none — sample-audit harness, code-only) | YES (git revert merge-SHA)
#   s6       (none — modal app code, code-only)       | YES (git revert merge-SHA)
#   s7       modal app stop data-engine-x-bdc-soi-    | no
#            parse-v2                                 |
#
# CRITICAL SAFETY: this script requires explicit --surface or --all to avoid an
# accidental "wipe everything." Default is print-help-and-exit. R2 deletes (s3)
# and the Postgres catalog/table DROP (s1/s2) require --allow-data-loss.
#
# Usage:
#   ./2026-05-22-sec-bdc-soi-html-parse-v2-rollback.sh --surface s7
#   ./2026-05-22-sec-bdc-soi-html-parse-v2-rollback.sh --all
#   ./2026-05-22-sec-bdc-soi-html-parse-v2-rollback.sh --all --allow-data-loss
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
      sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'
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

echo "==> Rolling back sec-bdc-soi-html-parse-v2 (surface=${SURFACE_FILTER:-ALL} repo=${REPO_FILTER:-all} allow_data_loss=$ALLOW_DATA_LOSS)"

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
R2_SOI_PARSED_V2_PREFIX="sec-bdc/soi-parsed-v2"
MODAL_APP_NAME="data-engine-x-bdc-soi-parse-v2"
SOURCE_SLUG="bdc_soi_parsed_v2"
LEDGER_TABLE="bdc_soi_parsed_v2_runs"

# Helper: emit a code-revert advisory (script does NOT auto-issue git revert).
_emit_code_revert() {
  local surface="$1" path_hint="$2"
  echo "    ADVISORY ($surface): forward-only surface — rollback is git revert of the merge commit."
  echo "      git -C $HQ_ALL_ROOT log --oneline --all -- $path_hint | head -5"
  echo "      git -C $HQ_ALL_ROOT revert <merge-SHA-of-PR>"
  echo "    See supabase/migrations/README.md §\"Policy\" — forward-only, revert-as-rollback."
  return 0
}

# ====================================================================== #
# s7 (reverse first) — Modal stop
# ====================================================================== #
# Also folds in r1/e1-e6 data-artifact cleanup note: the R2 v2 data those
# checks verify is wiped by s3 below — no separate r1/e1-e6 step.
rollback_surface "s7" "$SURFACE_REPO" '
  doppler run --project hq-all --config prd -- bash -c "
    modal app stop '"$MODAL_APP_NAME"'
  " || echo "    (modal app may already be stopped / never deployed — non-fatal)"
'

# ====================================================================== #
# s6 (reverse) — Modal app code (advisory: git revert merge-SHA)
# ====================================================================== #
rollback_surface "s6" "$SURFACE_REPO" '_emit_code_revert "s6" "apps/data-engine-x/modal/bdc_soi_parse_v2_app.py"'

# ====================================================================== #
# s5 (reverse) — sample-audit harness (advisory: git revert merge-SHA)
# ====================================================================== #
rollback_surface "s5" "$SURFACE_REPO" '_emit_code_revert "s5" "apps/data-engine-x/scripts/audit_bdc_soi_parse_v2_sample.py"'

# ====================================================================== #
# s4 (reverse) — classifier module (advisory: git revert merge-SHA)
# ====================================================================== #
rollback_surface "s4" "$SURFACE_REPO" '_emit_code_revert "s4" "apps/data-engine-x/scripts/_lib/bdc_soi_classifier.py"'

# ====================================================================== #
# s3 (reverse) — parser code revert + R2 sec-bdc/soi-parsed-v2/ wipe
# ====================================================================== #
# Wipes the v2 R2 prefix ONLY — v1 sec-bdc/soi-parsed/ MUST remain intact
# (this cycle does not touch v1 per `## Out of scope` L130-141).
# R2 cred-mapping AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID required for `aws s3 rm`.
rollback_surface "s3" "$SURFACE_REPO" '
  _emit_code_revert "s3" "apps/data-engine-x/scripts/parse_sec_bdc_soi_html_v2.py"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    Wiping R2 prefix sec-bdc/soi-parsed-v2/ (--allow-data-loss)"
    doppler run --project hq-all --config prd -- bash -c "
      AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY \
      aws s3 rm s3://'"$R2_BUCKET/$R2_SOI_PARSED_V2_PREFIX"'/ --recursive --endpoint-url=\$R2_ENDPOINT
    "
  else
    echo "    SKIP: R2 soi-parsed-v2/ wipe requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s2 (reverse) — ops.bdc_soi_parsed_v2_runs ledger table
# ====================================================================== #
# Forward-only per migrations/README.md §"Policy" — DROP TABLE is NOT the
# rollback. git revert merge-SHA + future re-merge is idempotent via
# CREATE TABLE IF NOT EXISTS. A DROP is offered ONLY under --allow-data-loss.
# NOTE: s2 also recreated ops.data_source_catalog_status (the view UNION ALL
# branch lives in s2's migration, not s1's — see `## Audit plan §"Ordering
# correction"`). The git revert of s2's merge restores the view to its
# pre-cycle definition.
rollback_surface "s2" "$SURFACE_REPO" '
  _emit_code_revert "s2" "apps/data-engine-x/supabase/migrations/*_bdc_soi_parsed_v2_runs.sql"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    DROP ops.'"$LEDGER_TABLE"' (--allow-data-loss)"
    dex_psql_ddl "DROP TABLE IF EXISTS ops.'"$LEDGER_TABLE"' CASCADE"
  else
    echo "    SKIP: DROP TABLE requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
# s1 (reverse last) — ops.data_source_catalog row bdc_soi_parsed_v2
# ====================================================================== #
# Forward-only. git revert merge-SHA re-applies idempotently
# (INSERT ... ON CONFLICT (source_slug) DO NOTHING). The catalog-row DELETE is
# offered ONLY under --allow-data-loss. The data_source_catalog_status view
# branch is reverted by s2's revert (the branch SQL lives in s2's migration).
rollback_surface "s1" "$SURFACE_REPO" '
  _emit_code_revert "s1" "apps/data-engine-x/supabase/migrations/*_bdc_soi_parsed_v2_data_source.sql"
  if [[ "$ALLOW_DATA_LOSS" -eq 1 ]]; then
    echo "    DELETE ops.data_source_catalog row source_slug='"$SOURCE_SLUG"' (--allow-data-loss)"
    dex_psql_ddl "DELETE FROM ops.data_source_catalog WHERE source_slug='\'"$SOURCE_SLUG"\''"
  else
    echo "    SKIP: catalog-row DELETE requires --allow-data-loss (code-revert advisory above stands)."
  fi
'

# ====================================================================== #
echo ""
echo "==> ROLLBACK SUMMARY: $PASS_COUNT ok / $FAIL_COUNT fail / $SKIP_COUNT skip"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  exit 1
fi
