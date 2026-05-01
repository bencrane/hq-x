// gtm.run-per-recipient-creative — child task fired once per
// (recipient × DM step). The parent gtm.run-initiative-pipeline task
// dispatches this via batchTrigger after the channel-step + audience
// materializers complete and the master strategist ships.
//
// Same actor → verdict loop as the parent task, scoped to one
// (recipient, step) pair. Per-recipient failures DO NOT abort the
// pipeline — the parent task aggregates failure rates and fails the
// pipeline only if a configurable threshold (default 50%) is crossed.
//
// Concurrency limit on this task's queue (configured at deploy time,
// default 50) caps simultaneous Anthropic round-trips to keep
// rate-limits and wall-clock predictable. With 5,000 recipients × 3
// DM steps = 15,000 fanouts × ~10s at concurrency 50 ≈ 1 hour wall.

import { logger, queue, task } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

type StepResult = {
  run_id: string;
  run_index: number;
  status: "succeeded" | "failed" | "running" | "superseded" | "queued";
  output_blob: {
    shape: string;
    value: unknown;
    raw_excerpt?: string;
    parse_error?: string;
    terminal_status?: string;
    stop_reason?: string | null;
  } | null;
  output_artifact_path: string | null;
  prompt_version_id: string | null;
  anthropic_session_id: string | null;
  anthropic_request_ids: string[] | null;
  cost_cents: number | null;
};

type VerdictOutput = {
  ship: boolean;
  issues?: { severity: "block" | "warn"; area: string; detail: string }[];
  redo_with?: string | null;
};

const RUN_STEP_TIMEOUT_MS = 600_000;

async function callRunStep(
  initiativeId: string,
  agentSlug: string,
  body: {
    hint?: string | null;
    upstream_outputs?: Record<string, unknown>;
    recipient_id?: string;
    channel_campaign_step_id?: string;
  },
): Promise<StepResult> {
  return await callHqx<StepResult>(
    `/internal/gtm/initiatives/${initiativeId}/run-step`,
    { agent_slug: agentSlug, ...body },
    { timeoutMs: RUN_STEP_TIMEOUT_MS },
  );
}

export interface PerRecipientPayload {
  initiativeId: string;
  recipientId: string;
  channelCampaignStepId: string;
  upstream?: Record<string, unknown>;
}

export interface PerRecipientResult {
  recipient_id: string;
  channel_campaign_step_id: string;
  status: "shipped" | "verdict_blocked" | "actor_failed" | "verdict_failed";
  reason?: string;
}

// Concurrency cap for the child queue. Bump in the Trigger dashboard
// as Anthropic rate-limits permit.
const PER_RECIPIENT_QUEUE = queue({
  name: "gtm-per-recipient-creative",
  concurrencyLimit: 50,
});

export const runPerRecipientCreative = task({
  id: "gtm.run-per-recipient-creative",
  queue: PER_RECIPIENT_QUEUE,
  maxDuration: 1800, // 30m headroom; an actor + verdict typically completes in ~30s
  run: async (payload: PerRecipientPayload): Promise<PerRecipientResult> => {
    const { initiativeId, recipientId, channelCampaignStepId, upstream = {} } =
      payload;

    const actorSlug = "gtm-per-recipient-creative";
    const verdictSlug = "gtm-per-recipient-creative-verdict";

    let actorRun: StepResult;
    try {
      actorRun = await callRunStep(initiativeId, actorSlug, {
        upstream_outputs: upstream,
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
      });
    } catch (err) {
      logger.error("per-recipient actor call failed", {
        initiativeId,
        recipientId,
        channelCampaignStepId,
        err: String(err),
      });
      return {
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
        status: "actor_failed",
        reason: String(err).slice(0, 200),
      };
    }
    if (actorRun.status !== "succeeded") {
      return {
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
        status: "actor_failed",
        reason: `actor_run_${actorRun.status}`,
      };
    }

    let verdictRun: StepResult;
    try {
      verdictRun = await callRunStep(initiativeId, verdictSlug, {
        upstream_outputs: {
          ...upstream,
          [actorSlug]: actorRun.output_blob?.value ?? null,
        },
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
      });
    } catch (err) {
      logger.error("per-recipient verdict call failed", {
        initiativeId,
        recipientId,
        channelCampaignStepId,
        err: String(err),
      });
      return {
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
        status: "verdict_failed",
        reason: String(err).slice(0, 200),
      };
    }
    if (verdictRun.status !== "succeeded") {
      return {
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
        status: "verdict_failed",
        reason: `verdict_run_${verdictRun.status}`,
      };
    }

    const verdictValue =
      (verdictRun.output_blob?.value as VerdictOutput) ?? null;
    if (verdictValue?.ship) {
      return {
        recipient_id: recipientId,
        channel_campaign_step_id: channelCampaignStepId,
        status: "shipped",
      };
    }
    return {
      recipient_id: recipientId,
      channel_campaign_step_id: channelCampaignStepId,
      status: "verdict_blocked",
      reason: verdictValue?.redo_with ?? "verdict_block_no_redo_hint",
    };
  },
});
