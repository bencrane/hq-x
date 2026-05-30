// sam-opps-archived-weekly — weekly SAM.gov archived-opportunities snapshot dispatch.
//
// Cron 14:00 UTC every Monday. POSTs the Modal endpoint for
// data-engine-x-sam-opps-archived-weekly::trigger_archived_snapshot_via_http,
// which spawns run_archived_snapshot() in Modal (current FY) and returns the
// call_id. FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev.
// Replaces the native modal.Cron. No param — the worker derives the current FY.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-sam-opps-archived-weekly-trigger-72f881.modal.run";

export const samOppsArchivedWeekly = schedules.task({
  id: "sam-opps-archived.weekly",
  cron: { pattern: "0 14 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("sam-opps-archived.weekly"))) return SKIPPED_DISABLED;
    logger.info("sam-opps-archived.weekly: spawning Modal ingest", {
      url: MODAL_TRIGGER_URL, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(MODAL_TRIGGER_URL, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_archived_snapshot_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string };
    logger.info("sam-opps-archived.weekly: Modal call spawned", {
      call_id: data.call_id, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
