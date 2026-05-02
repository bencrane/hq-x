// exa.process_research_job — durable executor for an async Exa research
// run. The hq-x route POST /api/v1/exa/jobs writes a row to
// business.exa_research_jobs and enqueues this task with {job_id}. We
// call back into hq-x's /internal/exa/jobs/{job_id}/process, which runs
// the right Exa endpoint, persists the raw payload to either DB based
// on the job's destination flag, and transitions status.
//
// Trigger.dev's task-level retry policy (max 3 attempts; trigger.config.ts)
// handles transient hq-x / Exa failures (network, DB blip). Deterministic
// Exa errors (4xx, schema problems) are persisted as job.status=failed
// without re-raising, so they don't burn retries.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type ProcessJobResult = {
  job_id: string;
  status: string;
  result_ref?: string;
  error?: string;
  skipped?: boolean;
  reason?: string;
};

export const exaProcessResearchJob = task({
  id: "exa.process_research_job",
  // Research polls cap at ~10 minutes inside the client; 1800 gives
  // headroom for retries plus DEX persistence latency.
  maxDuration: 1800,
  run: async ({ job_id }: { job_id: string }, { ctx }) => {
    const result = await callHqx<ProcessJobResult>(
      `/internal/exa/jobs/${job_id}/process`,
      { trigger_run_id: ctx.run.id },
    );
    logger.info("exa process_research_job completed", result);
    return result;
  },
});
