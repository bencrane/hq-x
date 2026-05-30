// cluster3.recovery_sweep — finds stuck queued lead_transfers and
// retries (or fails after max attempts).
//
// Stuck-queue scenarios:
//   * hq-x crashed mid-dispatch
//   * Anthropic outage during composer
//   * Trigger.dev send_intro task lost / never executed
//
// Cadence: every 5 minutes. Tight cadence keeps recovery latency low.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";
import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const CRON_EVERY_5_MIN = "*/5 * * * *";

export const cluster3RecoverySweep = schedules.task({
  id: "cluster3.recovery_sweep",
  cron: CRON_EVERY_5_MIN,
  maxDuration: 300,
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("cluster3.recovery_sweep"))) return SKIPPED_DISABLED;
    const result = await callHqx<{
      candidates: number;
      retried: number;
      succeeded: number;
      abandoned: number;
      errors: unknown[];
    }>(
      "/internal/cluster3/recovery-sweep",
      {},
    );
    logger.info("cluster3.recovery_sweep", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
