// dmaas.process_activation_job — durable executor for the async DMaaS
// campaign-activation flow. The hq-x route POST /api/v1/dmaas/campaigns
// writes a row to business.activation_jobs and enqueues this task with
// {job_id}. We call back into hq-x's /internal/dmaas/process-job, which
// dispatches by job.kind, runs the pipeline (campaign + cc + step +
// recipients + Lob upload + Dub mint), and persists status to the same
// row.
//
// Trigger.dev's task-level retry policy (max 3 attempts; trigger.config.ts)
// handles transient hq-x failures (network, DB blip). Deterministic
// business-logic failures inside hq-x are persisted as job.status=failed
// without re-raising, so they don't burn retries.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type ProcessJobResult = {
  job_id: string;
  status: string;
  skipped?: boolean;
  reason?: string;
  error?: string;
};

export const dmaasProcessActivationJob = task({
  id: "dmaas.process_activation_job",
  maxDuration: 1800,
  run: async ({ job_id }: { job_id: string }, { ctx }) => {
    const result = await callHqx<ProcessJobResult>(
      "/internal/dmaas/process-job",
      { job_id, trigger_run_id: ctx.run.id },
    );
    logger.info("dmaas process_activation_job completed", result);
    return result;
  },
});
