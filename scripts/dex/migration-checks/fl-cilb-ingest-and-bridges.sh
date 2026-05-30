#!/usr/bin/env bash
# Verification harness for fl-cilb-ingest-and-bridges (directive 2026-05-18).
# Runs all 6 surface verify commands. Exits 0 only if all requested pass.
#
# Surfaces:
#   s1 = apps/data-engine-x/modal/fl_cilb_daily_app.py (Modal daily ingest)
#   s2 = apps/data-engine-x/scripts/run_fl_cilb_lance_emit.py (Pattern A Lance emit)
#   s3 = apps/data-engine-x/scripts/build_bridge_sba_fl_cilb_lance.py (Pattern B bridge — REUSER)
#   s4 = apps/data-engine-x/scripts/build_bridge_fl_cilb_sunbiz_lance.py (Pattern B bridge — REUSER)
#   s5 = apps/data-engine-x/supabase/migrations/<UTC-TS>_fl_cilb_data_sources_and_bridges.sql
#   s6 = operator bootstrap (Modal deploy + R2 release Parquet + 3 Lance + Polaris + 2 bridge runs)
#
# Flags:
#   --surface s<N>  run a single surface only
#   --repo <name>   filter by repo (only hq-all here, so this is a no-op)
#
# Surface ordering: s1 -> s2 -> s3 -> s4 -> s5 -> s6 (declared order).
set -euo pipefail

source "$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

HELPERS_DIR="$HOME/Desktop/hq-all/apps/data-engine-x/scripts/migration-checks/fl-cilb-ingest-and-bridges-helpers"

echo "==> Verifying fl-cilb-ingest-and-bridges (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

run_surface() {
  local id="$1" repo="$2" helper="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- $id ($repo): RUNNING ($helper)"
  if bash "$HELPERS_DIR/$helper"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

run_surface 's1' 'hq-all' 'verify-s1.sh'
run_surface 's2' 'hq-all' 'verify-s2.sh'
run_surface 's3' 'hq-all' 'verify-s3.sh'
run_surface 's4' 'hq-all' 'verify-s4.sh'
run_surface 's5' 'hq-all' 'verify-s5.sh'
run_surface 's6' 'hq-all' 'verify-s6.sh'

echo "All requested surfaces verified."
