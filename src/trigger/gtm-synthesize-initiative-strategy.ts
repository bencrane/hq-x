// gtm.synthesize_initiative_strategy — durable executor for an async
// gtm-initiative strategy synthesis run. The hq-x route
// POST /api/v1/initiatives/{id}/synthesize-strategy enqueues this task
// with {initiative_id}. We call back into hq-x's
// /internal/initiatives/{initiative_id}/process-synthesis, which loads
// the six inputs, calls Anthropic once, validates the YAML
// front-matter, writes data/initiatives/<id>/campaign_strategy.md, and
// transitions the initiative to strategy_ready.
//
// Trigger.dev's task-level retry policy (max 3 attempts) handles
// transient failures (Anthropic rate limit blips, network errors).
// Deterministic synthesizer failures (twice-bad YAML) are persisted as
// initiative.status=failed without re-raising so they don't burn
// retries.

import { logger, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type ProcessSynthesisResult = {
  path?: string;
  model?: string;
  tokens_used?: number;
  cache_read_input_tokens?: number;
  cache_creation_input_tokens?: number;
  status?: string;
  error?: string;
};

export const gtmSynthesizeInitiativeStrategy = task({
  id: "gtm.synthesize_initiative_strategy",
  // Anthropic single-shot synthesis is fast (<2 min in the typical
  // case); 600s gives headroom for retries plus DB / DEX latency.
  maxDuration: 600,
  run: async ({ initiative_id }: { initiative_id: string }, { ctx }) => {
    const result = await callHqx<ProcessSynthesisResult>(
      `/internal/initiatives/${initiative_id}/process-synthesis`,
      { trigger_run_id: ctx.run.id },
    );
    logger.info("gtm synthesize_initiative_strategy completed", result);
    return result;
  },
});
