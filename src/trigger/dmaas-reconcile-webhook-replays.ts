// dmaas.reconcile_webhook_replays — daily sweep of webhook_events rows
// stuck in non-terminal status >1h. V1 surfaces drift only; auto-replay
// is a future workstream.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_08_UTC = "0 8 * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcileWebhookReplays = schedules.task({
  id: "dmaas.reconcile_webhook_replays",
  cron: CRON_DAILY_AT_08_UTC,
  maxDuration: 600,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/webhook-replays",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.webhook_replays", result);
    return result;
  },
});
