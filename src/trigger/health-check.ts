// hqx.health_check — once a day, call hq-x's /internal/scheduler/tick
// endpoint to prove the Trigger.dev → hq-x round-trip is healthy.
//
// A green run proves:
//   (1) Trigger.dev cloud can reach hq-x at HQX_API_BASE_URL,
//   (2) the static TRIGGER_SHARED_SECRET matches on both sides,
//   (3) hq-x's /internal/* router and shared-secret dependency work.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_14_UTC = "0 14 * * *";

type TickResponse = {
  status: string;
  received_at: string;
};

export const hqxHealthCheck = schedules.task({
  id: "hqx.health_check",
  cron: CRON_DAILY_AT_14_UTC,
  maxDuration: 60,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<TickResponse>("/internal/scheduler/tick", {
      trigger_run_id: ctx.run.id,
    });
    logger.info("hq-x tick acknowledged", result);
    return result;
  },
});
