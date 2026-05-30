#!/usr/bin/env bash
# Rollback harness for /scope cycle usaspending-pipeline-remediation.
#
# Standard CLAUDE.md §"Migration policy" rollback: revert merge SHA + push.
# Manual DB-row rollback for the 2 forward-only INSERTs/UPDATEs is documented
# below — operator runs ONLY if a revert-then-redeploy isn't enough.

set -euo pipefail

cat <<'EOF'
USAspending pipeline remediation — rollback procedure (2026-05-13).

PRIMARY (preferred): git revert the merge SHA + push.
  - data-engine-x Railway service auto-redeploys.
  - Modal apps remain deployed BUT will fail on the next run because
    psycopg connections will resolve to the reverted state. To fully remove
    the new Modal apps, manually run:
        cd ~/hq-all/apps/data-engine-x && \
            doppler run --project hq-all --config prd -- \
            modal app stop data-engine-x-usaspending-daily-verify && \
            modal app stop data-engine-x-usaspending-weekly-coverage

PRIMARY (DB-side cleanup, only if necessary):

  -- Surface s3: un-retire RW MV catalog rows. Note: this restores
  -- 'active' status; the rows still don't have live RW MVs behind them.
  -- Likely a misuse — keep RW retired and forward-fix.
  UPDATE ops.data_sources
     SET status      = 'active',
         retired_at  = NULL,
         format      = 'rw_mv'
   WHERE display_name IN ('mv_federal_contract_leads', 'mv_usaspending_contracts_typed')
     AND status      = 'retired'
     AND format      = 'deprecated_rw_mv';

  -- Surface s6: remove the 4 USAspending material declarations.
  DELETE FROM ops.material_attribute_declarations
   USING ops.data_sources s
   WHERE ops.material_attribute_declarations.source_id = s.source_id
     AND s.display_name = 'usaspending_contracts_lance'
     AND attribute_name IN ('recipient_uei', 'total_obligated_amount',
                            'period_of_performance_end_date', 'naics_code')
     AND declared_by = 'usaspending-pipeline-remediation/2026-05-13';

  -- Surface s12: remove the 2 USAspending alert subscriptions.
  DELETE FROM ops.alert_subscriptions
   USING ops.data_sources s
   WHERE ops.alert_subscriptions.source_id = s.source_id
     AND s.display_name = 'usaspending_contracts_lance'
     AND channel = 'telegram'
     AND recipient = '1766428207'
     AND alert_kind IN ('ingest_failed', 'cohort_drift');

  -- Surface s2 backfill rollback: delete the backfilled parquet (returns
  -- to poison-deleted state — the 8,183 contracts are NOT recovered any
  -- other way, so prefer NOT to do this).
  -- aws s3 rm s3://dex-raw-landing-zone/usaspending/contracts/api-delta/date=2026-05-11/data.parquet --endpoint-url $R2_ENDPOINT

NOTES:
  - Surface s4/s5 (comment-only changes) are NOT separately reversible; revert merge SHA covers them.
  - Surface s7 detector resolver: revert leaves USAspending un-wired (back to zero events for USAspending).
  - Surface s10/s11 Modal apps: revert removes source files; `modal app stop` is required to fully disable.
  - Surface s14 deploy auto-rolls-back when Railway picks up the reverted SHA.
EOF
