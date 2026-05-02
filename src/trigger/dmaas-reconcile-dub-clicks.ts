// dmaas.reconcile_dub_clicks — daily sweep against Dub's analytics for
// active dmaas_dub_links. Surfaces click-count drift as drift events.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_07_UTC = "0 7 * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcileDubClicks = schedules.task({
  id: "dmaas.reconcile_dub_clicks",
  cron: CRON_DAILY_AT_07_UTC,
  maxDuration: 1200,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/dub",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.dub", result);
    return result;
  },
});
