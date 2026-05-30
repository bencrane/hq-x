#!/usr/bin/env bash
# Rollback harness for hq-all Phase 0c.
#
# Reverse order:
#   1. Stop Modal cron app (data-engine-x-alerter-cron)
#   2. (After git revert of merge-SHA, Railway auto-redeploys without the new code)
#   3. Targeted DELETE/DROP of ops.alert_* tables + enum types
#
# Usage:
#   ./hq-all-phase-0c-alerting-atomic-ingest-rollback.sh        # full rollback
#   ./hq-all-phase-0c-alerting-atomic-ingest-rollback.sh --no-drop  # skip table DROPs
#   ./hq-all-phase-0c-alerting-atomic-ingest-rollback.sh --no-modal-stop  # skip Modal stop

set -uo pipefail

NO_DROP=0
NO_MODAL_STOP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-drop)         NO_DROP=1; shift 1 ;;
    --no-modal-stop)   NO_MODAL_STOP=1; shift 1 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

HQ_ALL="${HQ_ALL:-/Users/benjamincrane/hq-all}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$SCRIPT_DIR/_lib-shim.sh"

if [[ $NO_MODAL_STOP -eq 0 ]]; then
  echo "==> stopping Modal alerter cron"
  if command -v modal >/dev/null 2>&1; then
    modal app stop data-engine-x-alerter-cron 2>&1 || echo "  (modal app stop returned non-zero — may already be stopped)"
  else
    echo "  modal CLI not on PATH; skip"
  fi
fi

if [[ $NO_DROP -eq 0 ]]; then
  echo "==> dropping ops.alert_emissions, ops.alert_subscriptions, alert_* enums"
  dex_psql_ddl "
    DROP TABLE IF EXISTS ops.alert_emissions CASCADE;
    DROP TABLE IF EXISTS ops.alert_subscriptions CASCADE;
    DROP TYPE IF EXISTS alert_delivery_status;
    DROP TYPE IF EXISTS alert_channel;
    DROP TYPE IF EXISTS alert_kind;
  "
fi

echo "Phase 0c rollback complete (git revert of merge-SHA still required)"
