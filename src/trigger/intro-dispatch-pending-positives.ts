// intro.dispatch_pending_positives — periodic sweep of
// email_reply_classifications looking for `classification='positive' AND
// intro_fired_at IS NULL`. For each, enqueues an `intro.send_intro` run.
//
// v1: schedule registered but kept idle by checking
// INTRO_DISPATCH_ENABLED env var. Operator flips it on once positive-reply
// classification is being written by some upstream source (manual psql,
// inbox-orchestrator agent, EmailBison rule). Until then, you can curl
// /internal/customer-activation/fire-intro yourself for one-off testing.
//
// Cadence: every 5 minutes. Keep latency low so positive replies don't
// sit unintro'd.

import { logger, schedules, tasks } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_EVERY_5_MIN = "*/5 * * * *";

type PendingItem = {
  classification_id: string;
  email_message_id: string;
  channel_campaign_step_id: string;
  channel_campaign_id: string;
  leg2_initiative_id: string;
};

type PendingResult = {
  items: PendingItem[];
  limit: number;
};

export const introDispatchPendingPositives = schedules.task({
  id: "intro.dispatch_pending_positives",
  cron: CRON_EVERY_5_MIN,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    if (process.env.INTRO_DISPATCH_ENABLED !== "true") {
      logger.info("intro.dispatch skipped — INTRO_DISPATCH_ENABLED not 'true'");
      return { enabled: false, dispatched: 0 };
    }

    const pending = await callHqx<PendingResult>(
      "/internal/customer-activation/pending-positive-replies",
      { limit: 50 },
    );

    let dispatched = 0;
    for (const item of pending.items) {
      try {
        await tasks.trigger("intro.send_intro", {
          leg2_initiative_id: item.leg2_initiative_id,
          email_message_id: item.email_message_id,
          source: "schedule",
        });
        dispatched += 1;
      } catch (err) {
        logger.error("intro.dispatch failed to enqueue send_intro", {
          item,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }
    logger.info("intro.dispatch", {
      enabled: true,
      pending: pending.items.length,
      dispatched,
    });
    return { enabled: true, dispatched, pending: pending.items.length };
  },
});
