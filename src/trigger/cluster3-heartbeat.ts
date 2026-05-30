// cluster3.heartbeat — hourly synthetic dispatch through the orchestrator.
//
// Proves the chain (webhook event → orchestrator → classifier → dispatch
// → composer → verdict → ledger insert → email_message) is alive end-
// to-end. Stub mode by default — no real Anthropic, no real EB sends.
// The point is shape, not realism.
//
// Failing heartbeats fire a critical Telegram alert via hq-x's alert
// helper. Stale heartbeats (no pass in last 2h) also alert.
//
// Cadence: hourly. Adjust if the operator wants tighter SLA.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";
import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const CRON_HOURLY = "0 * * * *";

export const cluster3Heartbeat = schedules.task({
  id: "cluster3.heartbeat",
  cron: CRON_HOURLY,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("cluster3.heartbeat"))) return SKIPPED_DISABLED;
    const result = await callHqx<{
      heartbeat: { status: string; duration_ms?: number; reason?: string };
      staleness: { last_pass?: string | null; age_seconds?: number };
    }>(
      "/internal/cluster3/heartbeat",
      {},
    );
    logger.info("cluster3.heartbeat", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
