// jsearch-scheduled-ingest — declared schedule task for operator-defined
// recurring JSearch ingests. One declaration here; schedule entities are
// registered dynamically by the DEX backend (POST /api/v1/jsearch/schedules)
// via Trigger.dev's Management API using hq-x's project secret key.
// Trigger.dev fires this task on the registered cadence; payload.externalId
// carries the schedule_id (UUID) from ops.jsearch_search_schedules.
//
// On each fire we call hq-x's /internal/jsearch/run-scheduled-ingest, which
// looks up the schedule's params from DEX and POSTs to DEX's /jsearch/ingest.
// Same shape as exa-process-webset-job.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { callHqx } from "./lib/hqx-client";

interface JSearchScheduledIngestResult {
  schedule_id: string;
  status: string;
  rows_seen?: number;
  rows_upserted?: number;
  credits_used?: number;
  run_id?: string | null;
  error?: string | null;
}

export const JSEARCH_SCHEDULED_INGEST_TASK_ID = "jsearch-scheduled-ingest";

export const jsearchScheduledIngest = schedules.task({
  id: JSEARCH_SCHEDULED_INGEST_TASK_ID,
  maxDuration: 600,
  run: async (payload, { ctx }) => {
    const externalId = payload.externalId;
    if (!externalId) {
      throw new Error(
        "jsearch-scheduled-ingest fired without payload.externalId (schedule_id).",
      );
    }
    const result = await callHqx<JSearchScheduledIngestResult>(
      `/internal/jsearch/run-scheduled-ingest`,
      { schedule_id: externalId, trigger_run_id: ctx.run.id },
      { timeoutMs: 5 * 60_000 },
    );
    logger.info("jsearch-scheduled-ingest complete", { ...result });
    return result;
  },
});
