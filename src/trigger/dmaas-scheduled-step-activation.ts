// dmaas.scheduled_step_activation — durable-sleep executor for
// multi-step DMaaS campaigns. After step N completes, hq-x's
// step_scheduler.schedule_next_step persists a step_scheduled_activation
// activation_jobs row and enqueues this task with delay=delay_days*86400.
//
// Trigger.dev's wait.for() lets the run sleep across deploys / restarts.
// When the wait elapses we call /internal/dmaas/process-job which
// dispatches by job.kind and runs the standard step activation path.
// If the parent campaign or channel_campaign is paused/archived,
// hq-x cancels this run via Trigger.dev's run-cancel API which
// interrupts wait.for().

import { logger, task, wait } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type ProcessJobResult = {
  job_id: string;
  status: string;
  skipped?: boolean;
  reason?: string;
};

type Payload = {
  job_id: string;
  delay_seconds?: number;
};

export const dmaasScheduledStepActivation = task({
  id: "dmaas.scheduled_step_activation",
  // wait.for() can sleep for arbitrary durations; cap the run-level
  // budget at 30 days + buffer for the activation step itself.
  maxDuration: 30 * 24 * 60 * 60 + 1800,
  run: async ({ job_id, delay_seconds }: Payload, { ctx }) => {
    if (delay_seconds && delay_seconds > 0) {
      logger.info("dmaas.scheduled_step_activation sleeping", {
        job_id,
        delay_seconds,
      });
      await wait.for({ seconds: delay_seconds });
    }
    const result = await callHqx<ProcessJobResult>(
      "/internal/dmaas/process-job",
      { job_id, trigger_run_id: ctx.run.id },
    );
    logger.info("dmaas.scheduled_step_activation completed", result);
    return result;
  },
});
