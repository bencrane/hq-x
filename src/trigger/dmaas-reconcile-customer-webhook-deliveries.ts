// dmaas.reconcile_customer_webhook_deliveries — every 15 min, re-fire
// pending customer-webhook deliveries past their next_retry_at. Backstop
// for cases where the original Trigger.dev task lost its run state or
// the initial enqueue from emit_event() returned an error.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_EVERY_15_MIN = "*/15 * * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcileCustomerWebhookDeliveries = schedules.task({
  id: "dmaas.reconcile_customer_webhook_deliveries",
  cron: CRON_EVERY_15_MIN,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/customer-webhook-deliveries",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.customer_webhook_deliveries", result);
    return result;
  },
});
