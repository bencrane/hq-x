#!/usr/bin/env bash
# DOF Property Sales — monthly refresh wrapper.
#
# Refreshes the rolling-12-month files for all 5 boroughs, and on the first
# run of each calendar month, also probes for a freshly-published annualized
# file for the prior calendar year. Idempotent — re-runs are no-ops.
#
# Designed to be invoked by an external scheduler (cron, Railway cron job,
# Conductor schedule, etc.). Self-contained; no Trigger.dev required.
#
#   crontab entry (run on the 5th of every month at 06:00 UTC):
#     0 6 5 * * /path/to/data-engine-x/scripts/refresh_dof_sales.sh
#
# Requires DOPPLER_TOKEN in the environment. The Doppler config provides
# DEX_DB_URL_POOLED for the connection.
#
# A Trigger.dev variant can be added later (template in
# docs/directives/EXECUTOR_DIRECTIVE_NYC_DOF_ANNUALIZED_SALES_INGEST_POSTMERGE.md);
# kept out of this directive because the existing Trigger.dev → DEX boundary
# auth is broken (see CLAUDE.md "Trigger.dev → DEX boundary gap").

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

LOG_DIR="${DOF_SALES_LOG_DIR:-/tmp/dof_sales_refresh}"
mkdir -p "$LOG_DIR"
TS="$(date -u +%Y%m%d_%H%M%S)"
LOG="${LOG_DIR}/refresh_${TS}.log"

PYTHON="${DOF_SALES_PYTHON:-python3}"

echo "[$(date -u +%FT%TZ)] DOF sales refresh started" | tee -a "$LOG"

echo "[$(date -u +%FT%TZ)] rolling-refresh (5 boroughs)" | tee -a "$LOG"
PYTHONPATH="$REPO_ROOT" doppler run -- "$PYTHON" scripts/run_dof_sales_ingest.py rolling-refresh \
  >>"$LOG" 2>&1

# Probe for new annualized file for prior year. The 'already_completed' check
# inside the script makes this a no-op if the file is already ingested.
PRIOR_YEAR="$(date -u -v-1y +%Y 2>/dev/null || date -u -d 'last year' +%Y)"
echo "[$(date -u +%FT%TZ)] annualized probe for ${PRIOR_YEAR}" | tee -a "$LOG"
for boro in manhattan bronx brooklyn queens staten_island; do
  PYTHONPATH="$REPO_ROOT" doppler run -- "$PYTHON" scripts/run_dof_sales_ingest.py annualized \
    --year "$PRIOR_YEAR" --borough "$boro" \
    >>"$LOG" 2>&1 || echo "  (annualized $PRIOR_YEAR/$boro skipped or failed — expected if file not yet published)" | tee -a "$LOG"
done

echo "[$(date -u +%FT%TZ)] DOF sales refresh complete (log=$LOG)" | tee -a "$LOG"
