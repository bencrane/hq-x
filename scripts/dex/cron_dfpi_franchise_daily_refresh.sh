#!/usr/bin/env bash
# Daily refresh entrypoint for the DFPI Franchise Registry ingest.
#
# Why script-only and not Trigger.dev:
#   - The directive recommends Trigger.dev. Deferred for now because the
#     Trigger.dev → DEX Python M2M auth boundary is currently broken
#     (CLAUDE.md "Open boundary gap (2026-04-29)"). Wiring up a Trigger.dev
#     orchestrator while that gap exists would require either re-issuing
#     M2M tokens that DEX no longer validates, or reaching past the gap
#     with a super-admin API key — both add backend complexity for what is
#     a sub-minute, single-table ingest.
#   - The ACRIS / DOF Sales sibling ingests run on the same cron pattern.
#     ops.dfpi_ingest_runs gives Trigger.dev-equivalent observability.
#   - When the auth boundary is fixed, swapping to a Trigger.dev task is
#     pure wiring — drop in a `refreshDfpiMvs` task that calls a thin
#     `/api/internal/dfpi/refresh` endpoint, keep this script for backfill.
#
# Crontab usage (daily at 09:00 UTC — DFPI publishes filings throughout the
# business day on Pacific time, so any UTC time after 06:00 is fresh):
#
#   0 9 * * *  /path/to/scripts/cron_dfpi_franchise_daily_refresh.sh \
#                  >> /var/log/dfpi_refresh.log 2>&1
#
# Exit status:
#   0 = ingest succeeded (or no_change), MV refresh succeeded
#   1 = ingest failed
#   2 = ingest succeeded but MV refresh failed

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

if ! doppler configure --scope . 2>/dev/null | grep -q 'data-engine-x'; then
  echo "ERROR: Doppler not pinned to data-engine-x in $REPO_ROOT — run 'doppler setup --no-interactive' first." >&2
  exit 1
fi

echo "=== DFPI franchise refresh starting at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# Ingest. --skip-if-unchanged short-circuits with a no_change audit row when
# Solr numFound has not advanced since the prior successful run.
echo "--- running ingest (--skip-if-unchanged) ---"
if ! doppler run -- python3 scripts/run_dfpi_franchise_ingest.py --skip-if-unchanged; then
  echo "FAILED: dfpi ingest" >&2
  exit 1
fi

# MV refresh. CONCURRENTLY requires the unique index that mig
# 20260501042256_mv_dfpi_franchisors.sql defines. Order matters:
# Phase 3 depends on Phase 2.
echo "--- refreshing mv_dfpi_franchisors ---"
if ! doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -v ON_ERROR_STOP=1 -c "REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_dfpi_franchisors;"'; then
  echo "FAILED: mv_dfpi_franchisors refresh" >&2
  exit 2
fi

echo "--- refreshing mv_sba_dfpi_franchise_match ---"
if ! doppler run -- bash -c 'psql "$DEX_DB_URL_DIRECT" -v ON_ERROR_STOP=1 -c "REFRESH MATERIALIZED VIEW CONCURRENTLY entities.mv_sba_dfpi_franchise_match;"'; then
  echo "FAILED: mv_sba_dfpi_franchise_match refresh" >&2
  exit 2
fi

echo "=== DFPI franchise refresh finished at $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
exit 0
