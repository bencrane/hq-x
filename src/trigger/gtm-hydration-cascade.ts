// gtm_hydration_cascade_test — Phase 1 Bulk Firmographic Hydration test task.
//
// Sequence:
//   A. Cohort fetch — GET hq-x `/api/v1/gtm/companies/hydration-slice` via
//      `callHqxApi`. hq-x proxies to DEX, which reads the physical SAM ↔ PDL
//      ↔ USAspending bridge Lance dataset and returns up to 11 {uei, domain}
//      rows for Construction-NAICS recipients with lifetime obligations
//      > $150K. The Trigger task never talks to Modal, Lance, or any DB
//      directly — read path is Trigger → hq-x → DEX, mirroring every other
//      orchestrator.
//   B. Fan-out — for each {uei, domain}, POST to hq-x's `/internal/tasks/enrich`
//      proxy with provider=modal, action=hydrate_firmo_cascade. The proxy
//      dispatches to the GTM-hydration Modal Web Function for the heavy
//      waterfall and writes the terminal state into ops.task_runs before
//      acking — so the task can rely on `ack.status === "completed"` as the
//      ground truth for per-row success.
//   C. Auth — Step A uses BACKEND_X_SERVICE_TOKEN via `callHqxApi`. Step B
//      uses TRIGGER_SHARED_SECRET via `callHqx`. Both secrets must be present
//      in the Trigger.dev project env.
//
// The task owns ZERO business state. ops.task_runs is the enrich proxy's
// ledger; this task only orchestrates cohort-fetch → fan-out. No Supabase /
// Prisma / Postgres client is imported here — Trigger.dev is orchestration
// only.

import { logger, queue, task } from "@trigger.dev/sdk/v3";
import { callHqx, callHqxApi } from "./lib/hqx-client";

// ── Cohort row shape returned by the hq-x proxy ──────────────────────────

interface HydrationSliceEntity {
  uei: string;
  domain: string;
  // PDL-bridge LinkedIn URL when the upstream entity matched a PDL company.
  // Nullable — when present, the Modal hydrator uses it as the deterministic
  // Blitz key; when null, Modal falls back to the SAM-derived domain.
  linkedin_url: string | null;
}

// ── Payload contract for /internal/tasks/enrich ──────────────────────────

interface EnrichRequest {
  task_run_id: string;
  provider: string;
  action: string;
  entity_data: HydrationSliceEntity;
}

interface EnrichAck {
  acknowledged: boolean;
  endpoint: string;
  // Set by the enrich proxy after the provider call resolves. `acknowledged`
  // only proves the ledger row was inserted — `status` is the only field
  // that reflects whether the upstream provider call actually succeeded.
  status?: "completed" | "failed" | null;
  error?: Record<string, unknown> | null;
  [k: string]: unknown;
}

// ── Task ─────────────────────────────────────────────────────────────────

interface HydrationCascadePayload {
  // Reserved for future use (e.g., cohort selector, override limit). The
  // Phase 1 test exercises a fixed 11-row micro-batch entirely server-side,
  // so payload is intentionally empty today.
}

interface HydrationCascadeResult {
  status: "completed" | "partial" | "failed";
  task_run_id: string;
  total_entities: number;
  enriched_entities: number;
  failed_entities: number;
}

export const gtmHydrationCascadeTest = task({
  id: "gtm_hydration_cascade_test",
  maxDuration: 600,
  run: async (
    _payload: HydrationCascadePayload,
    { ctx },
  ): Promise<HydrationCascadeResult> => {
    const taskRunId = ctx.run.id;

    // ── Step 1: cohort fetch via hq-x → DEX proxy ───────────────────────
    const cohort = await callHqxApi<HydrationSliceEntity[]>(
      "/api/v1/gtm/companies/hydration-slice",
    );
    logger.info("gtm_hydration_cascade_test — cohort fetched", {
      taskRunId,
      cohortSize: cohort.length,
    });

    if (cohort.length === 0) {
      logger.warn("gtm_hydration_cascade_test — proxy returned empty cohort", {
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

    // ── Step 2: fan-out — one POST per {uei, domain} ────────────────────
    let enriched = 0;
    let failed = 0;

    for (const entity of cohort) {
      const body: EnrichRequest = {
        task_run_id: taskRunId,
        provider: "modal",
        action: "hydrate_firmo_cascade",
        entity_data: {
          uei: entity.uei,
          domain: entity.domain,
          linkedin_url: entity.linkedin_url,
        },
      };

      try {
        // 90s client-side budget. The proxy's Modal POST is bounded at 80s
        // (see gtm_pipeline.py:_MODAL_HYDRATION_URL dispatch), so the proxy
        // always returns a structured failure ack before this fetch aborts.
        const ack = await callHqx<EnrichAck>(
          "/internal/tasks/enrich",
          body,
          { timeoutMs: 90_000 },
        );
        // Require BOTH: the proxy acknowledged the ledger insert AND the
        // upstream provider call (Modal) resolved successfully. `acknowledged`
        // alone is true even when Modal returns 5xx or raises — that path
        // would silently inflate enriched counts.
        if (ack.acknowledged && ack.status === "completed") {
          enriched += 1;
        } else {
          failed += 1;
          logger.warn(
            "gtm_hydration_cascade_test — proxy not completed",
            {
              taskRunId,
              uei: entity.uei,
              ack,
            },
          );
        }
      } catch (err) {
        failed += 1;
        logger.error("gtm_hydration_cascade_test — proxy call failed", {
          taskRunId,
          uei: entity.uei,
          error: err instanceof Error ? err.message : String(err),
        });
      }
    }

    const status: HydrationCascadeResult["status"] =
      failed === 0
        ? "completed"
        : enriched === 0
        ? "failed"
        : "partial";

    logger.info("gtm_hydration_cascade_test — finished", {
      taskRunId,
      status,
      totalEntities: cohort.length,
      enrichedEntities: enriched,
      failedEntities: failed,
    });

    return {
      status,
      task_run_id: taskRunId,
      total_entities: cohort.length,
      enriched_entities: enriched,
      failed_entities: failed,
    };
  },
});

// ── Phase 2: 90-day active-primes hydration ─────────────────────────────
//
// Production fan-out over the pre-materialized split cohorts:
//   cohorts/primes_90d_fast (pdl_linkedin_url IS NOT NULL — proxy skips
//     the domain-resolution step natively)
//   cohorts/primes_90d_slow (pdl_linkedin_url IS NULL — proxy walks the
//     2-call fallback path)
//
// Cohort gate, anti-join against ops.task_runs, and split are all applied
// at emit time in apps/data-engine-x/scripts/build_cohort_primes_90d_lance.py.
// hq-x serves them via GET /api/v1/gtm/cohorts/primes-90d/{lane}.
//
// Asymmetric concurrency: 25 for fast (cheap single-call path) vs strictly
// 5 for slow (2-call fallback — throttled to keep the Supabase pool under
// the connection-extraction threshold). Each lane gets its own named
// queue + dedicated child task so the limits don't interfere.

const HYDRATION_90D_FAST_QUEUE = queue({
  name: "gtm-hydration-90d-fast",
  concurrencyLimit: 25,
});

const HYDRATION_90D_SLOW_QUEUE = queue({
  name: "gtm-hydration-90d-slow",
  concurrencyLimit: 5,
});

const BATCH_CHUNK_SIZE = 250;
// Per-row enrich timeout: matches the cascade-test budget. The proxy's
// Modal POST is bounded at 80s; this gives ~10s headroom.
const ENRICH_TIMEOUT_MS = 90_000;

interface EnrichEntityPayload {
  parent_run_id: string;
  entity: HydrationSliceEntity;
}

interface EnrichEntityResult {
  uei: string;
  enriched: boolean;
}

interface Hydration90dParentResult {
  status: "completed" | "partial" | "failed";
  task_run_id: string;
  lane: "fast" | "slow";
  total_entities: number;
  enriched_entities: number;
  failed_entities: number;
  chunks_dispatched: number;
}

function chunkArray<T>(items: T[], size: number): T[][] {
  if (size <= 0) {
    throw new Error("chunk size must be positive");
  }
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}

async function dispatchEnrich(
  parentRunId: string,
  entity: HydrationSliceEntity,
): Promise<EnrichEntityResult> {
  const body: EnrichRequest = {
    task_run_id: parentRunId,
    provider: "modal",
    action: "hydrate_firmo_cascade",
    entity_data: entity,
  };
  try {
    const ack = await callHqx<EnrichAck>(
      "/internal/tasks/enrich",
      body,
      { timeoutMs: ENRICH_TIMEOUT_MS },
    );
    const enriched = ack.acknowledged && ack.status === "completed";
    if (!enriched) {
      logger.warn("enrich proxy not completed", {
        parentRunId,
        uei: entity.uei,
        ack,
      });
    }
    return { uei: entity.uei, enriched };
  } catch (err) {
    logger.error("enrich proxy call failed", {
      parentRunId,
      uei: entity.uei,
      error: err instanceof Error ? err.message : String(err),
    });
    return { uei: entity.uei, enriched: false };
  }
}

export const gtmEnrichOne90dFast = task({
  id: "gtm_enrich_one_90d_fast",
  queue: HYDRATION_90D_FAST_QUEUE,
  maxDuration: 120,
  run: async (payload: EnrichEntityPayload): Promise<EnrichEntityResult> => {
    return dispatchEnrich(payload.parent_run_id, payload.entity);
  },
});

export const gtmEnrichOne90dSlow = task({
  id: "gtm_enrich_one_90d_slow",
  queue: HYDRATION_90D_SLOW_QUEUE,
  maxDuration: 120,
  run: async (payload: EnrichEntityPayload): Promise<EnrichEntityResult> => {
    return dispatchEnrich(payload.parent_run_id, payload.entity);
  },
});

interface Hydration90dParentPayload {
  // Reserved for future use (e.g., cap, override cohort slug).
}

async function runHydration90dParent(
  parentRunId: string,
  lane: "fast" | "slow",
  child: typeof gtmEnrichOne90dFast | typeof gtmEnrichOne90dSlow,
): Promise<Hydration90dParentResult> {
  const cohort = await callHqxApi<HydrationSliceEntity[]>(
    `/api/v1/gtm/cohorts/primes-90d/${lane}`,
  );
  logger.info("gtm_hydration_90d parent — cohort fetched", {
    parentRunId,
    lane,
    cohortSize: cohort.length,
  });

  if (cohort.length === 0) {
    return {
      status: "completed",
      task_run_id: parentRunId,
      lane,
      total_entities: 0,
      enriched_entities: 0,
      failed_entities: 0,
      chunks_dispatched: 0,
    };
  }

  const chunks = chunkArray(cohort, BATCH_CHUNK_SIZE);
  let enriched = 0;
  let failed = 0;

  for (let i = 0; i < chunks.length; i += 1) {
    const ch = chunks[i];
    logger.info("gtm_hydration_90d parent — dispatching chunk", {
      parentRunId,
      lane,
      chunkIndex: i,
      chunkSize: ch.length,
      totalChunks: chunks.length,
    });
    const batchResult = await child.batchTriggerAndWait(
      ch.map((entity) => ({
        payload: { parent_run_id: parentRunId, entity } satisfies EnrichEntityPayload,
      })),
    );
    for (const r of batchResult.runs) {
      if (!r.ok) {
        failed += 1;
        continue;
      }
      const out = r.output as EnrichEntityResult | undefined;
      if (out && out.enriched) {
        enriched += 1;
      } else {
        failed += 1;
      }
    }
  }

  const status: Hydration90dParentResult["status"] =
    failed === 0
      ? "completed"
      : enriched === 0
      ? "failed"
      : "partial";

  logger.info("gtm_hydration_90d parent — finished", {
    parentRunId,
    lane,
    status,
    totalEntities: cohort.length,
    enrichedEntities: enriched,
    failedEntities: failed,
    chunksDispatched: chunks.length,
  });

  return {
    status,
    task_run_id: parentRunId,
    lane,
    total_entities: cohort.length,
    enriched_entities: enriched,
    failed_entities: failed,
    chunks_dispatched: chunks.length,
  };
}

export const gtmHydration90dFast = task({
  id: "gtm_hydration_90d_fast",
  // Headroom: fast lane ≈ 3.8K rows / 25 concurrency × avg per-call seconds.
  // 4h ceiling accommodates the worst-case where most rows hit the 80s
  // Modal bound. Trigger will surface a maxDuration error long before the
  // run is "stuck"; the orchestrator is restart-safe via the proxy ledger.
  maxDuration: 14400,
  run: async (
    _payload: Hydration90dParentPayload,
    { ctx },
  ): Promise<Hydration90dParentResult> => {
    return runHydration90dParent(ctx.run.id, "fast", gtmEnrichOne90dFast);
  },
});

export const gtmHydration90dSlow = task({
  id: "gtm_hydration_90d_slow",
  // Slow lane ≈ 1.4K rows / 5 concurrency × per-call seconds — the
  // tightest concurrency lane needs the largest headroom. 6h ceiling.
  maxDuration: 21600,
  run: async (
    _payload: Hydration90dParentPayload,
    { ctx },
  ): Promise<Hydration90dParentResult> => {
    return runHydration90dParent(ctx.run.id, "slow", gtmEnrichOne90dSlow);
  },
});
