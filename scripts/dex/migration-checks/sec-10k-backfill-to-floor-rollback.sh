#!/usr/bin/env bash
# Rollback harness for /scope cycle sec-10k-backfill-to-floor.
#
# Surfaces (rolled back in REVERSE order):
#   s5 — delete Lance prefix s3://.../form_10k_lance/ (DRY-RUN unless --confirm-destructive)
#   s4 — stop Modal app data-engine-x-sec-edgar-form-10k (data in R2 preserved — append-only ingest)
#
# Per directive ~/Desktop/hq/directives/2026-05-13-sec-10k-backfill-to-floor.md.
#
# Usage:
#   ./sec-10k-backfill-to-floor-rollback.sh                       # DRY-RUN, all surfaces
#   ./sec-10k-backfill-to-floor-rollback.sh --confirm-destructive # actually delete Lance + stop Modal
#   ./sec-10k-backfill-to-floor-rollback.sh --surface s4          # one surface

set -euo pipefail

CONFIRM=0
SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm-destructive) CONFIRM=1; shift ;;
    --surface)             SURFACE_FILTER="$2"; shift 2 ;;
    --repo)                REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if (( CONFIRM )); then
  MODE="APPLY (destructive)"
else
  MODE="DRY-RUN"
fi

echo "==> Rolling back surfaces for sec-10k-backfill-to-floor (surface=${SURFACE_FILTER:-all} repo=${REPO_FILTER:-all} mode=${MODE})"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER"    && "$REPO_FILTER"    != "$repo" ]]; then return 0; fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id"   ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING ($MODE)"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# s5 — Lance prefix delete
rollback_lance_form_10k() {
  if (( CONFIRM )); then
    doppler run --project hq-all --config prd -- bash -c '
      aws s3 rm --recursive --endpoint-url "$R2_ENDPOINT" \
        s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/
    '
  else
    echo "DRY-RUN: would run: aws s3 rm --recursive --endpoint-url \$R2_ENDPOINT s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_10k_lance/"
  fi
}

# s4 — Stop Modal app (data preserved — R2 ingest is append-only; rolling back data destroys real work)
rollback_modal_form_10k() {
  if (( CONFIRM )); then
    modal app stop data-engine-x-sec-edgar-form-10k
  else
    echo "DRY-RUN: would run: modal app stop data-engine-x-sec-edgar-form-10k"
    echo "DRY-RUN: R2 data at s3://dex-raw-landing-zone/sec-edgar/form-10k/ is PRESERVED (append-only ingest)."
  fi
}

# REVERSE order: s5 then s4
rollback_surface "s5" "hq-all" "rollback_lance_form_10k"
rollback_surface "s4" "hq-all" "rollback_modal_form_10k"

echo "Rollback complete."
