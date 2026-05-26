// gtm_slice_enrichment — Phase 2 + 5 of the slice-to-campaign GTM pipeline.
//
// Sequence:
//   A. Extraction — GET hq-x `/api/v1/gtm/people?limit=5` via
//      `callHqxApi`. hq-x proxies to DEX `/api/internal/gtm/leads`,
//      which reads gtm.people (DEX is the system of record). Phase 5
//      replaces the Phase 2 mock array with this live proxy call. The
//      Trigger task never talks to Modal or Lance directly — the read
//      path is Trigger → hq-x → DEX, mirroring the write path the
//      enrich proxy already uses.
//   B. Fan-out  — for each entity, POST to hq-x's /internal/tasks/enrich
//      proxy (Phase 1).
//   C. Payload  — { task_run_id, provider, action, entity_data } per the
//      Phase 2 directive. `task_run_id` = ctx.run.id, `provider` =
//      "blitz" (hardcoded), `action` = "find_work_email" (hardcoded).
//   D. Auth     — Step A uses BACKEND_X_SERVICE_TOKEN (the same surface
//      hq-zone platform-api hits). Step B uses TRIGGER_SHARED_SECRET
//      via `callHqx`, unchanged. Both secrets must be present in the
//      Trigger.dev project env.
//
// The task owns ZERO business state. ops.task_runs is the enrich proxy's
// ledger; this task only orchestrates extract → fan-out.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx, callHqxApi } from "./lib/hqx-client";

// ── Entity shape passed downstream to /internal/tasks/enrich ─────────────

interface SliceEntity {
  id: string;
  domain: string | null;
  linkedin_url: string | null;
}

// ── Shape of GET /api/v1/gtm/people (see apps/hq-x/app/routers/gtm_people.py)

interface GtmPersonRow {
  id: string | null;
  full_name: string | null;
  title: string | null;
  source: string | null;
  company_id: string | null;
  company_name: string | null;
  company_domain: string | null;
  company_linkedin_url: string | null;
}

interface GtmPeoplePage {
  data: GtmPersonRow[];
  total: number;
  limit: number;
  offset: number;
}

// Extract a slice via the hq-x → DEX read proxy. Drops rows that lack an
// id, or that lack both anchors (domain and linkedin_url) — those rows
// can't be enriched downstream regardless of provider.
async function extractSliceViaHqx(limit: number): Promise<SliceEntity[]> {
  const page = await callHqxApi<GtmPeoplePage>(
    `/api/v1/gtm/people?limit=${limit}`,
  );
  const out: SliceEntity[] = [];
  for (const row of page.data ?? []) {
    if (!row.id) continue;
    if (!row.company_domain && !row.company_linkedin_url) continue;
    out.push({
      id: row.id,
      domain: row.company_domain,
      linkedin_url: row.company_linkedin_url,
    });
  }
  return out;
}

// ── Payload contract for /internal/tasks/enrich ──────────────────────────

interface EnrichRequest {
  task_run_id: string;
  provider: string;
  action: string;
  entity_data: SliceEntity;
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

    // ── Step A: extraction — live read via hq-x → DEX proxy ─────────────
    const entities = await extractSliceViaHqx(5);
    logger.info("gtm_slice_enrichment — extracted slice", {
      taskRunId,
      entityCount: entities.length,
    });

    if (entities.length === 0) {
      logger.warn("gtm_slice_enrichment — proxy returned no enrichable rows", {
        taskRunId,
      });
      return {
        status: "completed",
        task_run_id: taskRunId,
        total_entities: 0,
        enriched_entities: 0,
        failed_entities: 0,
      };
    }

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
