#!/usr/bin/env bash
# Rollback harness for migration abs-15g-ingest.
#
# Runs each surface's rollback command in REVERSE order. Per directive
#   ~/Desktop/hq/directives/2026-05-12-abs-15g-ingest.md
# rollback strategy is forward-only `git revert <merge-SHA>` for code/migration
# surfaces; deploy rollback uses `modal app stop`; config rollback is a
# runtime SQL UPDATE.
#
# Usage:
#   abs-15g-ingest-rollback.sh                       # all surfaces, reverse order
#   abs-15g-ingest-rollback.sh --surface s7          # one surface only
#   abs-15g-ingest-rollback.sh --repo hq-all         # repo filter
#
# Sources _lib-shim.sh once at top.

set -euo pipefail

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

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

# REVERSE order: s7 first, s1 last.

# s7 — stop the Modal cron app + delete R2 Lance prefix (derivative)
rollback_surface "s7" "hq-all" 'doppler run --project hq-all --config prd -- bash -c "modal app stop data-engine-x-sec-edgar-form-abs-15g 2>&1 || echo \"(modal stop best-effort)\""'

# s6 — retire ops.data_sources rows
rollback_surface "s6" "hq-all" 'dex_psql_ddl "UPDATE ops.data_sources SET status='\''retired'\'' WHERE display_name IN ('\''sec_edgar_form_abs_15g'\'','\''sec_edgar_form_abs_15g_lance'\'')"'

# s5 — delete Lance derivative dataset from R2
rollback_surface "s5" "hq-all" 'doppler run --project hq-all --config prd -- bash -c "aws s3 rm s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/ --recursive --endpoint-url \$R2_ENDPOINT 2>&1 || echo \"(s3 rm best-effort)\""'

# s4 / s3 / s2 / s1 — code + migration rollback is `git revert <merge-SHA>` post-merge.
# Pre-merge: rollback is implicit (don't merge the PR). Post-merge: operator runs
# `git -C ~/hq-all revert <merge-SHA> && git push origin main` which strips all
# code surfaces in one commit. The forward-only migration policy plus IF NOT EXISTS
# guards make re-application after revert idempotent.
rollback_surface "s4" "hq-all" 'echo "s4 rollback: run \"git -C ~/hq-all revert <merge-SHA>\" — forward-only per apps/data-engine-x/supabase/migrations/README.md §Policy"'
rollback_surface "s3" "hq-all" 'echo "s3 rollback: covered by s4 revert (same merge commit)"'
rollback_surface "s2" "hq-all" 'echo "s2 rollback: covered by s4 revert (same merge commit)"'
rollback_surface "s1" "hq-all" 'echo "s1 rollback: covered by s4 revert (same merge commit; IF NOT EXISTS makes re-apply idempotent)"'

echo "Rollback complete."
