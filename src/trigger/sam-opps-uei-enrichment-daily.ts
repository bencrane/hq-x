// sam-opps-uei-enrichment-daily — daily SAM.gov opportunities UEI enrichment dispatch.
//
// Cron 13:00 UTC (~1h after the active-opps feed at 12:00). POSTs the Modal
// endpoint for data-engine-x-sam-opps-api-uei-enrichment::trigger_smart_enrichment_via_http,
// which spawns run_smart_enrichment() in Modal and returns the call_id.
// FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev. Replaces the
// native modal.Cron. No param — the worker auto-walks its enrichment window.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-sam-opps-api-uei-enrichment-trig-8b6af7.modal.run";

export const samOppsUeiEnrichmentDaily = schedules.task({
  id: "sam-opps-uei.daily",
  cron: { pattern: "0 13 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("sam-opps-uei.daily"))) return SKIPPED_DISABLED;
    logger.info("sam-opps-uei.daily: spawning Modal ingest", {
      url: MODAL_TRIGGER_URL, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(MODAL_TRIGGER_URL, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_smart_enrichment_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string };
    logger.info("sam-opps-uei.daily: Modal call spawned", {
      call_id: data.call_id, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
