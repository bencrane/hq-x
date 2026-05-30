// usaspending-monthly-refresh — monthly Full-archive refresh dispatch.
//
// Cron: 16th of each month, 06:00 UTC. POSTs the Modal endpoint for
// data-engine-x-usaspending-monthly-ingest::trigger_monthly_refresh_via_http,
// which spawns run_monthly_refresh() in Modal (current fiscal years) and returns
// the call_id. FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev.
// Replaces the native modal.Cron (Modal-cron -> Trigger.dev migration). No date
// param — the worker derives the target fiscal years itself.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-usaspending-monthly-ingest-trigg-b57558.modal.run";

export const usaspendingMonthlyRefresh = schedules.task({
  id: "usaspending-monthly-refresh.monthly",
  cron: { pattern: "0 6 16 * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("usaspending-monthly-refresh.monthly"))) return SKIPPED_DISABLED;
    logger.info("usaspending-monthly-refresh.monthly: spawning Modal ingest", {
      url: MODAL_TRIGGER_URL, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(MODAL_TRIGGER_URL, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_monthly_refresh_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string };
    logger.info("usaspending-monthly-refresh.monthly: Modal call spawned", {
      call_id: data.call_id, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
