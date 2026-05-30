#!/usr/bin/env bash
# HMDA LAR legacy-era backfill — wraps run_hmda_lar_legacy_r2_ingest.py
# over 2007-2017 (CFPB historic mirror).
#
# Sibling to run_hmda_lar_multi_year.sh (modern 2018+ via FFIEC CFPB
# snapshot). Same idempotency basis (HEAD Last-Modified) and same audit
# table (ops.hmda_r2_ingest_runs). Stops on first failure; re-running
# picks up where it left off.
#
# Coverage scope: 2007-2017 only. Pre-2007 sits in the FFIEC archive at
# ffiec.gov/hmdarawdata/, which is bot-protected by Cloudflare and not
# addressable from this script.
#
# Usage:
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_legacy_multi_year.sh
#
#   # Subset:
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_legacy_multi_year.sh 2017 2016
#
#   # Smoke (50k rows per year):
#   doppler run --project hq-all --config prd -- \
#     bash apps/data-engine-x/scripts/run_hmda_lar_legacy_multi_year.sh --max-rows 50000

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INGEST="${SCRIPT_DIR}/run_hmda_lar_legacy_r2_ingest.py"

if [[ ! -f "$INGEST" ]]; then
  echo "FATAL: per-year legacy ingest script not found at $INGEST" >&2
  exit 1
fi

EXTRA_ARGS=()
YEARS=()
SKIP_IF_UNCHANGED=1
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
      SKIP_IF_UNCHANGED=0
      shift
      ;;
    20[0-1][0-9])
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
  YEARS=(2007 2008 2009 2010 2011 2012 2013 2014 2015 2016 2017)
fi

echo "=== HMDA LAR legacy-era backfill (CFPB historic mirror) ==="
echo "years: ${YEARS[*]}"
echo "extra args: ${EXTRA_ARGS[*]:-(none)}"
echo

START_TS=$(date +%s)
SUMMARY=()

for YEAR in "${YEARS[@]}"; do
  echo "------------------------------------------------------------------"
  echo "  LAR-legacy ${YEAR} — starting at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "------------------------------------------------------------------"
  YEAR_T0=$(date +%s)
  if uv run python3 "$INGEST" "$YEAR" "${EXTRA_ARGS[@]}"; then
    YEAR_DUR=$(( $(date +%s) - YEAR_T0 ))
    SUMMARY+=("${YEAR}  OK   wall=${YEAR_DUR}s")
    echo "  LAR-legacy ${YEAR} done in ${YEAR_DUR}s"
  else
    YEAR_DUR=$(( $(date +%s) - YEAR_T0 ))
    SUMMARY+=("${YEAR}  FAIL wall=${YEAR_DUR}s")
    echo "  LAR-legacy ${YEAR} FAILED after ${YEAR_DUR}s — aborting backfill" >&2
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
