// matching-engine.daily — fires the Phase 5 matching engine every day at
// 08:00 UTC, 15 min after the Phase 4 embedding-emit cron (07:45 UTC) so
// embeddings are fresh when the engine reads them.
//
// Calls hq-x's POST /api/v1/internal/matching-engine/evaluate-all, which
// iterates every enabled `business.matching_relationships` row and
// persists ranked matches + per-channel surfacings.
//
// Scaffold: the seeded `demand_side_fulfillment_paid_spec_v1` relationship
// is the only one active. Operator tunes scoring + adds more relationships
// without a code change — only a row insert.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { callHqx } from "./lib/hqx-client";
import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

// Daily at 08:00 UTC. Aligns with the Phase 4 embedding-emit cron at 07:45
// UTC: by the time matching runs, the new embeddings are ready.
const CRON_DAILY_08_UTC = "0 8 * * *";

type EvaluateAllResult = {
  relationships_evaluated: number;
  total_matches: number;
  per_relationship: Record<string, number>;
};

export const matchingEngineDaily = schedules.task({
  id: "matching-engine-daily",
  cron: CRON_DAILY_08_UTC,
  maxDuration: 1800, // 30 min — bounded by the engine's per-relationship loop.
  run: async (_payload, { ctx }) => {
    if (!(await passesGate("matching-engine-daily"))) return SKIPPED_DISABLED;
    const result = await callHqx<EvaluateAllResult>(
      "/api/v1/internal/matching-engine/evaluate-all",
      { trigger_run_id: ctx.run.id },
      { timeoutMs: 600_000 },
    );
    logger.info("matching-engine.daily completed", {
      ...result,
      trigger_run_id: ctx.run.id,
    });
    return result;
  },
});
