// warn-notices-daily — daily WARN Act notices ingest dispatch (Modal compute).
//
// Cron: 13:30 UTC daily. POSTs the Modal-issued stable Web Function URL for
// data-engine-x-warn-notices::trigger_refresh_via_http, which spawns
// daily_refresh(snapshot_date) in Modal and returns the call_id immediately.
//
// FIRE-AND-FORGET: the ingest + Lance emit run entirely inside Modal; this task
// only dispatches and returns. NO compute happens in Trigger.dev (this is the
// pilot for the Modal-cron -> Trigger.dev scheduling migration; the whole point
// is that Trigger schedules, Modal computes). snapshot_date is computed here and
// passed explicitly so the ingested date is deterministic regardless of dispatch
// latency.
//
// Auth: the Modal Web Function is public, matching txdot-letting-monthly — the
// Big Local News upstream is itself public. Proxy-auth (Modal-Key/Modal-Secret)
// is added on the shared dispatcher before the broader migration rollout; this
// single-feed pilot intentionally mirrors the existing txdot pattern.
//
// Web Function URL stability: the URL persists as long as the Modal app name +
// function name + workspace don't change. If renamed, re-deploy Modal + update.
//
// Migration note: while this pilot is validated, the native modal.Cron on
// data-engine-x-warn-notices::daily_refresh remains live (warn is idempotent —
// overwrites the same snapshot — so a brief dual-schedule overlap is harmless).
// The modal.Cron is removed in a follow-up change once this schedule is
// confirmed firing.

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-warn-notices-trigger-refresh-via-http.modal.run";

export const warnNoticesDaily = schedules.task({
  id: "warn-notices.daily",
  cron: { pattern: "30 13 * * *", timezone: "UTC" }, // after BLN's ~23:50 UTC nightly publish
  maxDuration: 120, // dispatch only — POST + JSON parse; the ingest runs in Modal
  run: async (_payload, { ctx }) => {
    const snapshotDate = new Date().toISOString().slice(0, 10); // YYYY-MM-DD UTC
    const url = `${MODAL_TRIGGER_URL}?snapshot_date=${encodeURIComponent(snapshotDate)}`;

    logger.info("warn-notices.daily: spawning Modal ingest", {
      url,
      snapshot_date: snapshotDate,
      trigger_run_id: ctx.run.id,
    });

    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`Modal trigger_refresh_via_http ${resp.status}: ${text}`);
    }

    const data = (await resp.json()) as {
      call_id: string;
      snapshot_date: string;
    };
    logger.info("warn-notices.daily: Modal call spawned", {
      call_id: data.call_id,
      snapshot_date: data.snapshot_date,
      trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
