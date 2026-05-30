// sam-construction-opps-sized-daily — daily SAM construction expected-award-size dataset.
//
// Cron 16:00 UTC. POSTs the Modal endpoint for
// data-engine-x-sam-construction-opps-sized::trigger_construction_refresh_via_http,
// which spawns daily_refresh() in Modal (builds the sized-band Lance dataset) and
// returns the call_id. FIRE-AND-FORGET: ingest runs in Modal; no compute in
// Trigger.dev. Replaces the native modal.Cron. No param.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-sam-construction-opps-sized-trig-961d58.modal.run";

export const samConstructionOppsSizedDaily = schedules.task({
  id: "sam-construction-opps-sized.daily",
  cron: { pattern: "0 16 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("sam-construction-opps-sized.daily"))) return SKIPPED_DISABLED;
    logger.info("sam-construction-opps-sized.daily: spawning Modal ingest", {
      url: MODAL_TRIGGER_URL, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(MODAL_TRIGGER_URL, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_construction_refresh_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string };
    logger.info("sam-construction-opps-sized.daily: Modal call spawned", {
      call_id: data.call_id, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
