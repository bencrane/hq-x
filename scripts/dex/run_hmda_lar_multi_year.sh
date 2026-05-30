#!/usr/bin/env bash
# HMDA LAR multi-year backfill — wraps run_hmda_r2_ingest.py over 2020-2024.
#
# Idempotent: each year's ingest skips if FFIEC's HEAD Last-Modified hasn't
# advanced past the prior run (HEAD-based short-circuit in the per-year script).
#
# Stops on first failure. Re-running picks up where it left off.
#
# Usage:
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_multi_year.sh
#
#   # Subset of years:
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_multi_year.sh 2020 2021
#
#   # Smoke (50k rows per year):
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_multi_year.sh --max-rows 50000

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INGEST="${SCRIPT_DIR}/run_hmda_r2_ingest.py"

if [[ ! -f "$INGEST" ]]; then
  echo "FATAL: per-year ingest script not found at $INGEST" >&2
  exit 1
fi

EXTRA_ARGS=()
YEARS=()
SKIP_IF_UNCHANGED=1  # default ON — re-runs short-circuit when FFIEC is unchanged
while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-rows)
      EXTRA_ARGS+=(--max-rows "$2")
      shift 2
      ;;
    --dry-run)
      EXTRA_ARGS+=(--dry-run)
      shift
      ;;
    --force)
      SKIP_IF_UNCHANGED=0  # re-ingest even if Last-Modified is unchanged
      shift
      ;;
    20[0-9][0-9])
      YEARS+=("$1")
      shift
      ;;
    *)
      echo "FATAL: unrecognized arg: $1" >&2
      exit 1
      ;;
  esac
done

if [[ $SKIP_IF_UNCHANGED -eq 1 ]]; then
  EXTRA_ARGS+=(--skip-if-unchanged)
fi

if [[ ${#YEARS[@]} -eq 0 ]]; then
  YEARS=(2020 2021 2022 2023 2024)
fi

echo "=== HMDA LAR multi-year backfill ==="
echo "years: ${YEARS[*]}"
echo "extra args: ${EXTRA_ARGS[*]:-(none)}"
echo

START_TS=$(date +%s)
SUMMARY=()

for YEAR in "${YEARS[@]}"; do
  echo "------------------------------------------------------------------"
  echo "  LAR ${YEAR} — starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "------------------------------------------------------------------"
  YEAR_T0=$(date +%s)
  if uv run python3 "$INGEST" lar "$YEAR" "${EXTRA_ARGS[@]}"; then
    YEAR_DUR=$(( $(date +%s) - YEAR_T0 ))
    SUMMARY+=("${YEAR}  OK   wall=${YEAR_DUR}s")
    echo "  LAR ${YEAR} done in ${YEAR_DUR}s"
  else
    YEAR_DUR=$(( $(date +%s) - YEAR_T0 ))
    SUMMARY+=("${YEAR}  FAIL wall=${YEAR_DUR}s")
    echo "  LAR ${YEAR} FAILED after ${YEAR_DUR}s — aborting backfill" >&2
    echo
    echo "=== summary (partial) ==="
    printf '  %s\n' "${SUMMARY[@]}"
    exit 1
  fi
  echo
done

TOTAL_DUR=$(( $(date +%s) - START_TS ))
echo "=== summary ==="
printf '  %s\n' "${SUMMARY[@]}"
echo "  total wall=${TOTAL_DUR}s"
