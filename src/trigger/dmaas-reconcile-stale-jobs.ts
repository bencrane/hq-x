// dmaas.reconcile_stale_jobs — daily sweep of activation_jobs in
// running > threshold and failed > dead_letter_delay. Wakes them
// up so customers don't see jobs stuck mid-flight.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_05_UTC = "0 5 * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcileStaleJobs = schedules.task({
  id: "dmaas.reconcile_stale_jobs",
  cron: CRON_DAILY_AT_05_UTC,
  maxDuration: 600,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/stale-jobs",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.stale_jobs", result);
    return result;
  },
});
