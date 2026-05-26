// gtm_slice_enrichment — Phase 2 of the slice-to-campaign GTM pipeline.
//
// Sequence per Phase 2 directive:
//   A. Extraction — placeholder for the Modal app fetching a slice from
//      Lance. Returns a fixed array of 5 mock entity objects so the
//      fan-out path is exercised end-to-end without external IO.
//   B. Fan-out  — for each entity, POST to hq-x's /internal/tasks/enrich
//      proxy (Phase 1).
//   C. Payload  — { task_run_id, provider, action, entity_data } per the
//      Phase 2 directive. `task_run_id` = ctx.run.id, `provider` =
//      "blitz" (hardcoded), `action` = "find_work_email" (hardcoded).
//   D. Auth     — Authorization: Bearer ${TRIGGER_SHARED_SECRET},
//      threaded through `callHqx` (apps/hq-x/src/trigger/lib/hqx-client.ts),
//      which is the same auth shim every other /internal/* task uses.
//
// The task owns ZERO business state. The ledger row in ops.task_runs
// is the proxy endpoint's job to write in a follow-up phase; Phase 2's
// scope is just the orchestration loop + payload contract.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

// ── Mock entity shape returned by the placeholder extraction step ─────────

interface MockEntity {
  id: string;
  domain: string;
  linkedin_url: string;
}

// Placeholder for the Modal app's `/fetch-slice` endpoint. Returns a
// deterministic array of 5 mock entities so the fan-out path is
// exercised without real network IO. Wrapped in an async function and
// `await`ed inside the task so the call site already matches the shape
// the real Modal HTTP call will take.
async function mockExtractSliceFromLance(): Promise<MockEntity[]> {
  return [
    {
      id: "ent_0001",
      domain: "acme-trucking.com",
      linkedin_url: "https://www.linkedin.com/company/acme-trucking",
    },
    {
      id: "ent_0002",
      domain: "bayside-logistics.com",
      linkedin_url: "https://www.linkedin.com/company/bayside-logistics",
    },
    {
      id: "ent_0003",
      domain: "cornerstone-freight.io",
      linkedin_url: "https://www.linkedin.com/company/cornerstone-freight",
    },
    {
      id: "ent_0004",
      domain: "deltarun-transit.com",
      linkedin_url: "https://www.linkedin.com/company/deltarun-transit",
    },
    {
      id: "ent_0005",
      domain: "eastpoint-haulage.net",
      linkedin_url: "https://www.linkedin.com/company/eastpoint-haulage",
    },
  ];
}

// ── Payload contract for /internal/tasks/enrich ──────────────────────────

interface EnrichRequest {
  task_run_id: string;
  provider: string;
  action: string;
  entity_data: MockEntity;
}

interface EnrichAck {
  acknowledged: boolean;
  endpoint: string;
  // Additional fields the proxy may echo back are ignored by the task.
  [k: string]: unknown;
}

// ── Task ─────────────────────────────────────────────────────────────────

interface SliceEnrichmentPayload {
  // Reserved for future use (e.g., audience_spec_id, slice partition
  // pointer). Phase 2 only exercises the orchestration loop, so payload
  // is intentionally empty today.
}

interface SliceEnrichmentResult {
  status: "completed" | "partial" | "failed";
  task_run_id: string;
  total_entities: number;
  enriched_entities: number;
  failed_entities: number;
}

export const gtmSliceEnrichment = task({
  id: "gtm_slice_enrichment",
  maxDuration: 600,
  run: async (
    _payload: SliceEnrichmentPayload,
    { ctx },
  ): Promise<SliceEnrichmentResult> => {
    const taskRunId = ctx.run.id;

    // ── Step A: extraction (placeholder Modal call) ─────────────────────
    const entities = await mockExtractSliceFromLance();
    logger.info("gtm_slice_enrichment — extracted slice", {
      taskRunId,
      entityCount: entities.length,
    });

    // ── Step B: fan-out — one POST per entity ───────────────────────────
    let enriched = 0;
    let failed = 0;

    for (const entity of entities) {
      // Step C: payload exactly matches the Phase 2 contract.
      const body: EnrichRequest = {
        task_run_id: taskRunId,
        provider: "blitz",
        action: "find_work_email",
        entity_data: entity,
      };

      try {
        // Step D: callHqx() threads `Authorization: Bearer
        // ${TRIGGER_SHARED_SECRET}` automatically — same auth shim every
        // other /internal/* task uses.
        const ack = await callHqx<EnrichAck>("/internal/tasks/enrich", body);
        if (ack.acknowledged) {
          enriched += 1;
        } else {
          failed += 1;
          logger.warn("gtm_slice_enrichment — proxy ack=false", {
            taskRunId,
            entityId: entity.id,
            ack,
          });
        }
      } catch (err) {
        failed += 1;
        logger.error("gtm_slice_enrichment — proxy call failed", {
          taskRunId,
          entityId: entity.id,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    const status: SliceEnrichmentResult["status"] =
      failed === 0
        ? "completed"
        : enriched === 0
        ? "failed"
        : "partial";

    logger.info("gtm_slice_enrichment — finished", {
      taskRunId,
      status,
      totalEntities: entities.length,
      enrichedEntities: enriched,
      failedEntities: failed,
    });

    return {
      status,
      task_run_id: taskRunId,
      total_entities: entities.length,
      enriched_entities: enriched,
      failed_entities: failed,
    };
  },
});
