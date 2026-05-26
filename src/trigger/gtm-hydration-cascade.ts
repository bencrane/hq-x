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

import { logger, task } from "@trigger.dev/sdk/v3";
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
