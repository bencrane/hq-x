// usaspending-assistance-delta-daily — daily assistance (FABS) delta dispatch.
//
// Cron 07:00 UTC (staggered 1h after contracts). POSTs the Modal endpoint for
// data-engine-x-usaspending-api-daily-assistance-delta::trigger_assistance_delta_via_http,
// which spawns run_api_daily_assistance_delta(target_date) in Modal and returns
// the call_id. FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev.
// Replaces the native modal.Cron (Modal-cron -> Trigger.dev migration).

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-usaspending-api-daily-assistance-fe6b50.modal.run";

export const usaspendingAssistanceDeltaDaily = schedules.task({
  id: "usaspending-assistance-delta.daily",
  cron: { pattern: "0 7 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const targetDate = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
    const url = `${MODAL_TRIGGER_URL}?target_date=${encodeURIComponent(targetDate)}`;
    logger.info("usaspending-assistance-delta.daily: spawning Modal ingest", {
      url, target_date: targetDate, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_assistance_delta_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string; target_date: string };
    logger.info("usaspending-assistance-delta.daily: Modal call spawned", {
      call_id: data.call_id, target_date: data.target_date, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
