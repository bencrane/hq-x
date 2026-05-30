#!/usr/bin/env bash
# Rollback harness for /scope cycle abs-15g-parser-polish-and-backfill.
#
# Runs surface rollbacks in REVERSE order. Each surface's rollback is
# guarded; destructive ops (Lance prefix delete) require --confirm-destructive.
#
# Usage:
#   abs-15g-parser-polish-and-backfill-rollback.sh                       # all surfaces (s3 dry-run)
#   abs-15g-parser-polish-and-backfill-rollback.sh --confirm-destructive # actually delete Lance prefix
#   abs-15g-parser-polish-and-backfill-rollback.sh --surface s3 --confirm-destructive

set -euo pipefail

# shellcheck source=/dev/null
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

SURFACE_FILTER=""
REPO_FILTER=""
CONFIRM_DESTRUCTIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all}, confirm-destructive: $CONFIRM_DESTRUCTIVE)"

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

# REVERSE order — s3 first, then s2, then s1.

# s3 — Lance prefix delete (DESTRUCTIVE, --confirm-destructive required).
if [[ "$CONFIRM_DESTRUCTIVE" == "1" ]]; then
  rollback_surface "s3" "hq-all" 'doppler run --project hq-all --config prd -- bash -c "AWS_ACCESS_KEY_ID=\$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=\$R2_SECRET_ACCESS_KEY aws s3 rm s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/ --recursive --endpoint-url \$R2_ENDPOINT"'
else
  rollback_surface "s3" "hq-all" 'echo "DRY-RUN: would delete s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_abs_15g_lance/ (use --confirm-destructive to actually delete)"'
fi

# s2 — no-op (R2 data preserved per Source ingest invariant; chained `modal run` invocations
# each terminate on container timeout naturally). If operator wants to halt the recurring cron,
# run `modal app stop data-engine-x-sec-edgar-form-abs-15g` manually — this rollback does not
# touch the cron (it's the forward state for ABS-15G).
rollback_surface "s2" "hq-all" 'echo "s2 rollback is no-op: R2 data preserved; chained runs self-terminate. Run modal app stop manually if you want to halt the cron."'

# s1 — git revert post-merge per forward-only policy. This rollback is documentation;
# the actual revert is operator-driven.
rollback_surface "s1" "hq-all" 'echo "s1 rollback is git revert <merge-SHA> per apps/data-engine-x/supabase/migrations/README.md §Policy. Look up merge SHA in directive Execution log."'

echo "Rollback complete (or dry-run as above)."
