#!/usr/bin/env bash
# Daily refresh entrypoint for the NYC HPD + DOB Socrata bundle.
#
# Decision (post-merge note): cron-only via this wrapper rather than
# Trigger.dev, matching the sibling ACRIS ingest's choice. Reasoning:
#   - Trigger.dev -> DEX boundary is currently broken (CLAUDE.md "Trigger.dev
#     -> DEX boundary gap, queued work, 2026-04-29"); TS tasks mint M2M
#     tokens that DEX no longer validates.
#   - With --skip-if-unchanged, daily polling is cheap: 5 small metadata
#     fetches; the heavy HPD Violations pull only runs on the days HPD
#     publishes (daily-ish per declared cadence).
#   - ops.nyc_opendata_ingest_runs gives the same observability surface a
#     Trigger.dev orchestrator would; an eventual TS wrapper is purely a
#     wiring change.
#
# Crontab usage (daily at 07:00 UTC, after the publishers' overnight runs):
#
#   0 7 * * *  /path/to/scripts/cron_nyc_hpd_dob_daily_refresh.sh \
#                  >> /var/log/nyc_hpd_dob_refresh.log 2>&1
#
# Exit status:
#   0 = all datasets succeeded (including no_change)
#   1 = at least one dataset failed (see ops.nyc_opendata_ingest_runs)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doppler must be pinned to data-engine-x/prd in this directory.
if ! doppler configure --scope . 2>/dev/null | grep -q 'data-engine-x'; then
  echo "ERROR: Doppler not pinned to data-engine-x in $REPO_ROOT — run 'doppler setup --no-interactive' first." >&2
  exit 1
fi

echo "=== NYC HPD/DOB daily refresh starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

failures=0
for dataset in \
  hpd-registrations \
  hpd-contacts \
  hpd-violations \
  dob-ecb-violations \
  dob-permits
do
  echo "--- refreshing ${dataset} ---"
  if ! doppler run -- python3 scripts/run_nyc_opendata_socrata_ingest.py "$dataset" --skip-if-unchanged; then
    echo "FAILED: $dataset" >&2
    failures=$((failures + 1))
  fi
done

echo "=== NYC HPD/DOB daily refresh finished at $(date -u +%Y-%m-%dT%H:%M:%SZ) (failures=$failures) ==="
exit $((failures > 0 ? 1 : 0))
