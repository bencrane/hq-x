// clusters.outbound_recovery_sweep — every 15 minutes, scan Cluster 1
// + Cluster 2 step recipients stuck in 'scheduled' past threshold and
// fire alerts as appropriate. Observability sweep, no auto-recovery.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_EVERY_15_MIN = "*/15 * * * *";

export const clustersOutboundRecoverySweep = schedules.task({
  id: "clusters.outbound_recovery_sweep",
  cron: CRON_EVERY_15_MIN,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<{
      status: string;
      duration_ms: number;
      per_cluster: Record<string, unknown>;
    }>("/internal/clusters/outbound-recovery-sweep", {});
    logger.info("clusters.outbound_recovery_sweep", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
