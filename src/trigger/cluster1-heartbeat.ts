// cluster1.heartbeat — hourly synthetic Cluster 1 outbound heartbeat.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";
import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const CRON_HOURLY = "5 * * * *";

export const cluster1Heartbeat = schedules.task({
  id: "cluster1.heartbeat",
  cron: CRON_HOURLY,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("cluster1.heartbeat"))) return SKIPPED_DISABLED;
    const result = await callHqx<{
      heartbeat: { status: string; duration_ms?: number; reason?: string };
      staleness: { last_pass?: string | null; age_seconds?: number };
    }>("/internal/clusters/cluster1/heartbeat", {});
    logger.info("cluster1.heartbeat", { ...result, trigger_run_id: ctx.run.id });
    return result;
  },
});
