// usaspending-contracts-delta-daily — daily prime-contract (FPDS) delta dispatch.
//
// Cron 06:00 UTC. POSTs the Modal endpoint for
// data-engine-x-usaspending-api-daily-delta::trigger_contracts_delta_via_http,
// which spawns run_api_daily_delta(target_date) in Modal and returns the call_id.
// FIRE-AND-FORGET: the ingest runs entirely in Modal; no compute in Trigger.dev.
// Replaces the native modal.Cron (Modal-cron -> Trigger.dev migration).
//
// target_date = yesterday UTC, computed here and passed explicitly (deterministic
// regardless of dispatch latency; matches the worker's prior-24h-window default).
// Unauthenticated like the warn_notices/txdot pilots; proxy-auth lands on the
// shared dispatcher before the broad rollout. URL stable unless app/fn renamed.

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_TRIGGER_URL =
  "https://bencrane--data-engine-x-usaspending-api-daily-delta-trig-df2672.modal.run";

export const usaspendingContractsDeltaDaily = schedules.task({
  id: "usaspending-contracts-delta.daily",
  cron: { pattern: "0 6 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_payload, { ctx }) => {
    const targetDate = new Date(Date.now() - 86_400_000).toISOString().slice(0, 10);
    const url = `${MODAL_TRIGGER_URL}?target_date=${encodeURIComponent(targetDate)}`;
    logger.info("usaspending-contracts-delta.daily: spawning Modal ingest", {
      url, target_date: targetDate, trigger_run_id: ctx.run.id,
    });
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" } });
    if (!resp.ok) {
      throw new Error(`Modal trigger_contracts_delta_via_http ${resp.status}: ${await resp.text()}`);
    }
    const data = (await resp.json()) as { call_id: string; target_date: string };
    logger.info("usaspending-contracts-delta.daily: Modal call spawned", {
      call_id: data.call_id, target_date: data.target_date, trigger_run_id: ctx.run.id,
    });
    return data;
  },
});
