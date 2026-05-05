// exa.process_webset_job — durable executor for an async Exa Websets run.
// The hq-x route POST /api/v1/exa/websets writes a row to
// business.exa_webset_jobs and enqueues this task with {job_id}. We call
// back into hq-x's /internal/exa/websets/{job_id}/process, which:
//   1. Creates the webset on Exa (using dex_run_id as externalId).
//   2. Polls Exa until completion.
//   3. Fetches all items and persists them to DEX exa.* tables.
//   4. Marks the job succeeded or failed.
//
// maxDuration is generous (3600s = 1h) because the Exa poll loop can
// take up to 15 minutes, and DEX persistence adds latency. The internal
// endpoint is idempotent on terminal jobs so retries are safe.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type ProcessWebsetResult = {
  job_id: string;
  status: string;
  exa_webset_id?: string;
  dex_run_id?: string;
  item_count?: number;
  error?: string;
  skipped?: boolean;
  reason?: string;
};

export const exaProcessWebsetJob = task({
  id: "exa.process_webset_job",
  // Webset creation + polling can take up to 15 min; 3600 gives retries
  // plus DEX persistence latency.
  maxDuration: 3600,
  run: async ({ job_id }: { job_id: string }, { ctx }) => {
    const result = await callHqx<ProcessWebsetResult>(
      `/internal/exa/websets/${job_id}/process`,
      { trigger_run_id: ctx.run.id },
      { timeoutMs: 900_000 }, // 15 min — matches _POLL_MAX_ATTEMPTS * _POLL_INTERVAL
    );
    logger.info("exa process_webset_job completed", result);
    return result;
  },
});
