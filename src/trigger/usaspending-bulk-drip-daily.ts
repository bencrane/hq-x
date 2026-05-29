// usaspending-bulk-drip-daily — daily bulk-archive drip dispatch.
//
// Cron 05:00 UTC. POSTs the Modal endpoint for
// data-engine-x-usaspending-daily-ingest::trigger_daily_drip_via_http, which
// spawns run_daily_drip(feed_date) in Modal and returns the call_id.
// FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev.
// Replaces the native modal.Cron (Modal-cron -> Trigger.dev migration).

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-usaspending-daily-ingest-trigger-2048d8.modal.run";

export const usaspendingBulkDripDaily = schedules.task({
  id: "usaspending-bulk-drip.daily",
  cron: { pattern: "0 5 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const feedDate = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
    const url = `${MODAL_TRIGGER_URL}?feed_date=${encodeURIComponent(feedDate)}`;
    logger.info("usaspending-bulk-drip.daily: spawning Modal ingest", {
      url, feed_date: feedDate, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_daily_drip_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string; feed_date: string };
    logger.info("usaspending-bulk-drip.daily: Modal call spawned", {
      call_id: data.call_id, feed_date: data.feed_date, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
