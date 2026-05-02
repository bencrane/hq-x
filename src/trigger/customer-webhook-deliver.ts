// customer_webhook.deliver — fire one customer-webhook delivery attempt.
// Trigger.dev calls hq-x's /internal/customer-webhooks/deliver with
// {delivery_id}; that endpoint computes the HMAC, POSTs to the
// customer's URL, and records the result back into the
// business.customer_webhook_deliveries row. On failure, it schedules
// the next retry on the row itself; the every-15-min reconciliation
// cron re-enqueues retries. Trigger.dev's task-level retry policy
// (max 3 attempts; trigger.config.ts) is a backstop for transient
// hq-x bugs (network, DB blip).

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type DeliverResult = {
  delivery_id: string;
  status: string;
  reason?: string;
  response_status?: number;
  skipped?: boolean;
};

export const customerWebhookDeliver = task({
  id: "customer_webhook.deliver",
  maxDuration: 60,
  run: async ({ delivery_id }: { delivery_id: string }) => {
    const result = await callHqx<DeliverResult>(
      "/internal/customer-webhooks/deliver",
      { delivery_id },
    );
    logger.info("customer_webhook delivery completed", result);
    return result;
  },
});
