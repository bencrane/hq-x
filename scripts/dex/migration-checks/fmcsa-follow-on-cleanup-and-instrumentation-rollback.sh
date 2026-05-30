#!/usr/bin/env bash
# Rollback harness for /scope cycle fmcsa-follow-on-cleanup-and-instrumentation.
#
# Authored by Stage 3.A audit subagent (2026-05-13 UTC).
# Documents the rollback path for each surface. Most rollback is `git revert <merge-SHA>`
# (which the operator runs manually); this script documents the order and any
# non-git rollback (Modal app rollback + DB row delete).

set -euo pipefail

for _root in "$HOME/hq-all" "$HOME/Desktop/hq-all"; do
  if [[ -f "$_root/apps/data-engine-x/scripts/_lib/dex.sh" ]]; then
    export DEX_LIB_PATH="$_root/apps/data-engine-x/scripts/_lib/dex.sh"
    HQ_ALL_ROOT="$_root"
    break
  fi
done
if [[ -z "${DEX_LIB_PATH:-}" ]]; then
  echo "FAIL: cannot locate a hq-all checkout with apps/data-engine-x/scripts/_lib/dex.sh" >&2
  exit 2
fi

# shellcheck source=/dev/null
source "$HQ_ALL_ROOT/apps/data-engine-x/scripts/migration-checks/_lib-shim.sh"

FMCSA_SOURCE_ID="3a24978f-3a80-4a7f-928a-fc9fed290f54"
TELEGRAM_RECIPIENT="1766428207"

MERGE_SHA="${MERGE_SHA:-}"
if [[ -z "$MERGE_SHA" ]]; then
  echo "FAIL: MERGE_SHA env required for rollback (the merge commit to revert)" >&2
  exit 2
fi

echo "==> ROLLBACK fmcsa-follow-on-cleanup-and-instrumentation (MERGE_SHA=$MERGE_SHA)"

cat <<EOF

ROLLBACK STEPS (operator-driven; this script documents the order):

1. Code rollback (Railway auto-deploys on git revert push):
     cd ~/hq-all && git revert --no-edit $MERGE_SHA
     git push origin main
   This reverts s1 (path strings), s3 (coverage script), s5 (cost script),
   s6 (docs). Railway redeploys automatically on the new main SHA.

2. Modal app rollback (manual):
     doppler run --project hq-all --config prd -- modal app rollback data-engine-x-fmcsa-daily-verify
     doppler run --project hq-all --config prd -- modal app rollback data-engine-x-fmcsa-weekly-coverage
   If apps were never deployed before the cycle, use:
     doppler run --project hq-all --config prd -- modal app stop data-engine-x-fmcsa-daily-verify
     doppler run --project hq-all --config prd -- modal app stop data-engine-x-fmcsa-weekly-coverage

3. DB row rollback (s7 alert_subscriptions inserts are idempotent —
   forward-only is fine; deleting is operator-judgment):

     # Only if operator decides to also remove the alert rows:
     doppler run --project hq-all --config prd -- psql "\$DEX_DB_URL_DIRECT" -c "
       DELETE FROM ops.alert_subscriptions
        WHERE source_id = '$FMCSA_SOURCE_ID'
          AND channel = 'telegram'
          AND recipient = '$TELEGRAM_RECIPIENT'
          AND alert_kind IN ('ingest_failed','cohort_drift')
          AND created_at >= '2026-05-13T00:00:00Z'
     "

4. Heartbeat row cleanup (s2 invocations wrote rows to data_source_ingest_runs):
   No cleanup required — these are append-only observability rows. Operator
   may DELETE WHERE run_metadata->>'writer' = 'fmcsa-daily-verify' if desired.

EOF

# Print current state to help operator confirm rollback is needed.
echo "==> Current alert_subscriptions rows for this cycle (delete candidate):"
dex_psql_query "SELECT alert_id, alert_kind, created_at FROM ops.alert_subscriptions WHERE source_id = '$FMCSA_SOURCE_ID' AND channel = 'telegram' AND recipient = '$TELEGRAM_RECIPIENT' AND alert_kind IN ('ingest_failed','cohort_drift')" || true

echo ""
echo "==> Current heartbeat rows (informational):"
dex_psql_query "SELECT COUNT(*) FROM ops.data_source_ingest_runs WHERE source_id = '$FMCSA_SOURCE_ID' AND run_metadata ->> 'writer' = 'fmcsa-daily-verify'" || true

echo ""
echo "==> Rollback playbook printed. Operator executes manually."
exit 0
