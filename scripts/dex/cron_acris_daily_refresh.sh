#!/usr/bin/env bash
# Daily incremental refresh entrypoint for ACRIS ingest.
#
# Decision (post-merge note §10): we run script-only via cron rather than
# Trigger.dev, because:
#   - the bulk fact tables (16M-46M rows) blow past Trigger.dev's task
#     duration ceiling on the initial backfill and during heavy refresh days;
#   - daily incremental loads are bounded (10k-100k rows/day across all 10
#     fact tables) and complete in a few minutes;
#   - ops.acris_ingest_runs (mig 146) gives the same observability surface
#     a Trigger.dev orchestrator would, so the eventual TS wrapper is purely
#     a wiring change.
#
# Crontab usage (daily at 06:00 UTC, after NYC DOF's overnight publish):
#
#   0 6 * * *  /path/to/scripts/cron_acris_daily_refresh.sh \
#                  >> /var/log/acris_refresh.log 2>&1
#
# Exit status:
#   0 = all datasets succeeded
#   1 = at least one dataset failed (check ops.acris_ingest_runs for detail)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doppler must be pinned to data-engine-x/prd in this directory.
# Verify pinning before doing anything.
if ! doppler configure --scope . 2>/dev/null | grep -q 'data-engine-x'; then
  echo "ERROR: Doppler not pinned to data-engine-x in $REPO_ROOT — run 'doppler setup --no-interactive' first." >&2
  exit 1
fi

echo "=== ACRIS daily refresh starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

failures=0
for dataset in \
  rp-master rp-legals rp-parties rp-references rp-remarks \
  pp-master pp-legals pp-parties pp-references pp-remarks
do
  echo "--- refreshing ${dataset} ---"
  if ! doppler run -- python3 scripts/run_acris_incremental_refresh.py "$dataset"; then
    echo "FAILED: $dataset" >&2
    failures=$((failures + 1))
  fi
done

# Lookup tables refresh weekly (or on-demand) — they barely change. Drop
# this loop into a separate weekly cron if you want; here we run it daily
# because the cost is ~5 seconds total.
echo "--- refreshing lookups ---"
if ! doppler run -- python3 scripts/run_acris_full_backfill.py lookup-codes; then
  echo "FAILED: lookups" >&2
  failures=$((failures + 1))
fi

echo "=== ACRIS daily refresh finished at $(date -u +%Y-%m-%dT%H:%M:%SZ) (failures=$failures) ==="
exit $((failures > 0 ? 1 : 0))
