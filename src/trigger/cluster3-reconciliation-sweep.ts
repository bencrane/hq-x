// cluster3.reconciliation_sweep — daily poll of EmailBison for replies
// we may have missed via dropped webhooks. Backfills synthetic events
// for any EB reply that doesn't have a corresponding 'replied' row in
// our email_message_events, then re-invokes the orchestrator.
//
// Cadence: daily at 04 UTC (slot mirrors the dmaas-reconcile-* family).

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_04_UTC = "0 4 * * *";

export const cluster3ReconciliationSweep = schedules.task({
  id: "cluster3.reconciliation_sweep",
  cron: CRON_DAILY_AT_04_UTC,
  maxDuration: 1800,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<{
      status: string;
      scanned: number;
      already: number;
      backfilled: number;
      campaigns: number;
      duration_ms?: number;
      errors?: unknown[];
    }>(
      "/internal/cluster3/reconciliation-sweep",
      { lookback_hours: 48, per_campaign_limit: 200 },
      { timeoutMs: 1_800_000 },
    );
    logger.info("cluster3.reconciliation_sweep", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
