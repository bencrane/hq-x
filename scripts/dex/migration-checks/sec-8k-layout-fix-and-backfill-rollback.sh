#!/usr/bin/env bash
# Rollback harness for /scope cycle sec-8k-layout-fix-and-backfill.
#
# Runs surface rollbacks in REVERSE order. s7's Lance-prefix delete is guarded
# behind --confirm-destructive (default DRY-RUN). s6 rollback is `modal app stop`
# which is recoverable via redeploy. s6a is a no-op gate (no code change made).
# s6b rollback is documented ACCEPTED LOSS (derivative smoke data).
#
# Per directive ~/Desktop/hq/directives/2026-05-13-sec-8k-layout-fix-and-backfill.md.
#
# Usage:
#   ./sec-8k-layout-fix-and-backfill-rollback.sh                              # DRY-RUN: print intended actions
#   ./sec-8k-layout-fix-and-backfill-rollback.sh --confirm-destructive        # actually run rollbacks
#   ./sec-8k-layout-fix-and-backfill-rollback.sh --surface s7 --confirm-destructive

set -euo pipefail

# shellcheck source=./_lib-shim.sh
source "$(dirname "${BASH_SOURCE[0]}")/_lib-shim.sh"

SURFACE_FILTER=""
CONFIRM_DESTRUCTIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface)             SURFACE_FILTER="$2"; shift 2 ;;
    --confirm-destructive) CONFIRM_DESTRUCTIVE=1; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

DRY_RUN_PREFIX=""
if [[ "$CONFIRM_DESTRUCTIVE" == "0" ]]; then
  DRY_RUN_PREFIX="DRY-RUN: "
  echo "==> DRY-RUN (pass --confirm-destructive to actually run rollbacks)"
else
  echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all})"
fi

rollback_surface() {
  local id="$1" cmd="$2"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  echo "-- rollback $id: ${DRY_RUN_PREFIX}$cmd"
  if [[ "$CONFIRM_DESTRUCTIVE" == "1" ]]; then
    if eval "$cmd"; then
      echo "-- rollback $id: OK"
    else
      echo "-- rollback $id: FAILED" >&2
      return 1
    fi
  fi
}

# REVERSE order — s7 first (derivative Lance dataset), then s6, then s6b/s6a (no-op).

# --- s7 rollback: delete Lance prefix ----------------------------------- #
# Lance dataset is derivative; safe to delete and re-emit. Guarded behind
# --confirm-destructive.
rollback_surface "s7" '
  doppler run --project hq-all --config prd -- bash -c "
    AWS_ACCESS_KEY_ID=\"\$R2_ACCESS_KEY_ID\" \
    AWS_SECRET_ACCESS_KEY=\"\$R2_SECRET_ACCESS_KEY\" \
    aws s3 rm --recursive --endpoint-url \"\$R2_ENDPOINT\" s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_8k_lance/
  "
'

# --- s6 rollback: stop Modal app (halts further runs; data preserved) --- #
rollback_surface "s6" '
  cd "$REPO_ROOT/apps/data-engine-x" && \
  doppler run --project hq-all --config prd -- modal app stop data-engine-x-sec-edgar-form-8k
'

# --- s6b rollback: ACCEPTED LOSS ---------------------------------------- #
rollback_surface "s6b" '
  echo "  s6b rollback is ACCEPTED LOSS (derivative smoke data; re-ingest by re-running smoke)"
'

# --- s6a rollback: no-op (s6a is a code-or-noop gate; if no code change, nothing to revert) -- #
rollback_surface "s6a" '
  echo "  s6a is a no-op gate; if no code change was made this is a no-op"
'

echo "Rollback complete."
