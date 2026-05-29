// sam-opps-active-daily — daily SAM.gov active-opportunities snapshot dispatch.
//
// Cron 12:00 UTC. POSTs the Modal endpoint for
// data-engine-x-sam-opps-active-daily::trigger_active_snapshot_via_http, which
// spawns run_active_snapshot(snapshot_date) in Modal and returns the call_id.
// FIRE-AND-FORGET: ingest runs in Modal; no compute in Trigger.dev. Replaces the
// native modal.Cron. snapshot_date = today UTC (the worker's prior cron default).

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-sam-opps-active-daily-trigger-ac-af4da8.modal.run";

export const samOppsActiveDaily = schedules.task({
  id: "sam-opps-active.daily",
  cron: { pattern: "0 12 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const snapshotDate = new Date().toISOString().slice(0, 10);
    const url = `${MODAL_TRIGGER_URL}?snapshot_date=${encodeURIComponent(snapshotDate)}`;
    logger.info("sam-opps-active.daily: spawning Modal ingest", {
      url, snapshot_date: snapshotDate, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_active_snapshot_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string; snapshot_date: string };
    logger.info("sam-opps-active.daily: Modal call spawned", {
      call_id: data.call_id, snapshot_date: data.snapshot_date, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
