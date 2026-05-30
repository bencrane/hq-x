#!/usr/bin/env bash
# Rollback harness for fl-cilb-ingest-and-bridges (directive 2026-05-18).
# Runs surface rollbacks in REVERSE order: s6 -> s5 -> s4 -> s3 -> s2 -> s1.
#
# Flags:
#   --surface s<N>  roll back a single surface only
#   --repo <name>   filter by repo (only hq-all here, so this is a no-op)
#
# Note: s1-s4 + s5 code-surface rollbacks are operator-driven `git revert <merge-SHA>`
# (printed instructions only — not executable here). s6 performs real teardown
# (Polaris DELETE for 3 generic tables + R2 prefix delete for 3 Lance datasets +
# R2 prefix delete for raw fl-cilb/release=*/data.parquet + modal app stop).
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

echo "==> Rolling back fl-cilb-ingest-and-bridges (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

rollback_surface() {
  local id="$1" repo="$2" helper="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- $id ($repo): ROLLING BACK ($helper)"
  if bash "$HELPERS_DIR/$helper"; then
    echo "-- $id ($repo): ROLLBACK OK"
  else
    echo "-- $id ($repo): ROLLBACK FAIL" >&2
    return 1
  fi
}

# REVERSE order: s6 -> s5 -> s4 -> s3 -> s2 -> s1
rollback_surface 's6' 'hq-all' 'rollback-s6.sh'
rollback_surface 's5' 'hq-all' 'rollback-s5.sh'
rollback_surface 's4' 'hq-all' 'rollback-s4.sh'
rollback_surface 's3' 'hq-all' 'rollback-s3.sh'
rollback_surface 's2' 'hq-all' 'rollback-s2.sh'
rollback_surface 's1' 'hq-all' 'rollback-s1.sh'

echo "All requested rollbacks executed (post-merge git reverts must be run manually for s1-s5)."
