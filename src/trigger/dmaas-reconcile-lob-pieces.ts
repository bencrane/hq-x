// dmaas.reconcile_lob_pieces — daily sweep against Lob's source of
// truth for active direct_mail steps. Surfaces any piece-count drift
// (dropped webhooks, etc.) as drift events; gap-fill handled inside
// the projector.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_06_UTC = "0 6 * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcileLobPieces = schedules.task({
  id: "dmaas.reconcile_lob_pieces",
  cron: CRON_DAILY_AT_06_UTC,
  maxDuration: 1200,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/lob",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.lob", result);
    return result;
  },
});
