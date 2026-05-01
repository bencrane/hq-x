// gtm.run-initiative-pipeline — sequences the post-payment GTM pipeline
// across five actor/verdict pairs (the last is fanout):
//   1. gtm-sequence-definer            → -verdict
//   2. gtm-channel-step-materializer   → -verdict
//   3. gtm-audience-materializer       → -verdict
//   4. gtm-master-strategist           → -verdict
//   5. gtm-per-recipient-creative      → -verdict   [FANOUT per (recipient × DM step)]
//
// This task DOES NOT call Anthropic. Every Anthropic invocation lives
// inside hq-x's POST /internal/gtm/initiatives/{id}/run-step, which
// opens the MAGS session, blocks for the agent's terminal turn, parses
// the output, persists a gtm_subagent_runs row, and returns a structured
// StepResult. This task is just the loop + the verdict ship-or-redo
// decision + the manual-mode gate.
//
// Failure model (MAX_VERDICT_RETRIES = 1 in v0):
//   * Actor failure  → mark pipeline failed, throw.
//   * Verdict failure → mark pipeline failed, throw.
//   * Verdict ship: false → that's the actor producing output that
//     didn't pass review. Throw with reason `verdict_block`.
//
// Trigger.dev's task durability covers crashes / network blips at the
// hqx-client layer. The TS task itself owns ZERO state — every state
// mutation lands in hq-x's DB before run-step returns.

import { logger, task, wait } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";
import {
  PerRecipientPayload,
  PerRecipientResult,
  runPerRecipientCreative,
} from "./gtm-run-per-recipient-creative";

// Manual-mode gating uses Trigger's wait-token pattern: createToken with a
// stable idempotencyKey (`advance:<initiativeId>:<actor>`), then forToken
// blocks until the operator completes it. The frontend's "Advance" button
// calls into hq-x → /api/v1/admin/initiatives/{id}/advance, which records
// the request in initiative.history. Wiring the actual completeToken call
// from hq-x lands in a follow-up directive once the Trigger management
// API key is provisioned (the wait module's completeToken needs an SDK
// initialized with TRIGGER_API_KEY, which only the trigger runtime has
// today). Until then, manual mode logs the gate but doesn't actually
// pause — the directive's §11 explicitly defers richer manual-mode
// handling to a follow-up directive.
const ENABLE_MANUAL_GATE = false;

type StepResult = {
  run_id: string;
  run_index: number;
  status: "succeeded" | "failed" | "running" | "superseded" | "queued";
  output_blob: {
    shape: string;
    raw_chars?: number;
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

interface PipelineStep {
  actor: string;
  verdict: string;
  isFanout: boolean;
  fanoutKind: "per_recipient_per_dm_step" | null;
}

const STEPS: PipelineStep[] = [
  {
    actor: "gtm-sequence-definer",
    verdict: "gtm-sequence-definer-verdict",
    isFanout: false,
    fanoutKind: null,
  },
  {
    actor: "gtm-channel-step-materializer",
    verdict: "gtm-channel-step-materializer-verdict",
    isFanout: false,
    fanoutKind: null,
  },
  {
    actor: "gtm-audience-materializer",
    verdict: "gtm-audience-materializer-verdict",
    isFanout: false,
    fanoutKind: null,
  },
  {
    actor: "gtm-master-strategist",
    verdict: "gtm-master-strategist-verdict",
    isFanout: false,
    fanoutKind: null,
  },
  {
    actor: "gtm-per-recipient-creative",
    verdict: "gtm-per-recipient-creative-verdict",
    isFanout: true,
    fanoutKind: "per_recipient_per_dm_step",
  },
];

// Per-recipient fanout failure threshold. If more than this fraction of
// (recipient × DM step) child runs end up in any non-`shipped` state,
// the parent pipeline is marked failed so the operator can iterate the
// per-recipient prompt before another full audience run.
const FANOUT_FAILURE_THRESHOLD = 0.5;

interface FanoutTarget {
  recipient_id: string;
  channel_campaign_step_id: string;
}

interface FanoutTargetsResponse {
  items: FanoutTarget[];
  expected_count: number;
}

// V0: a verdict failure means the pipeline fails at that step. The retry
// loop is structured so flipping this to >1 in a follow-up directive
// enables retry-with-hint without restructuring.
const MAX_VERDICT_RETRIES = 1;

// hq-x blocks for the full Anthropic round trip in /run-step. Master
// strategist runs can take 60-120s on Opus. Allow plenty of headroom.
const RUN_STEP_TIMEOUT_MS = 600_000;

async function callRunStep(
  initiativeId: string,
  agentSlug: string,
  body: { hint?: string | null; upstream_outputs?: Record<string, unknown> },
): Promise<StepResult> {
  return await callHqx<StepResult>(
    `/internal/gtm/initiatives/${initiativeId}/run-step`,
    { agent_slug: agentSlug, ...body },
    { timeoutMs: RUN_STEP_TIMEOUT_MS },
  );
}

async function reportPipelineFailed(
  initiativeId: string,
  triggerRunId: string,
  failedAtSlug: string,
  reason: string,
): Promise<void> {
  await callHqx(
    `/internal/gtm/initiatives/${initiativeId}/pipeline-failed`,
    {
      trigger_run_id: triggerRunId,
      failed_at_slug: failedAtSlug,
      reason,
    },
  );
}

async function reportPipelineCompleted(
  initiativeId: string,
  triggerRunId: string,
): Promise<void> {
  await callHqx(
    `/internal/gtm/initiatives/${initiativeId}/pipeline-completed`,
    { trigger_run_id: triggerRunId },
  );
}

interface PipelinePayload {
  initiativeId: string;
  gatingMode?: "auto" | "manual";
  startFrom?: string;
}

export const gtmRunInitiativePipeline = task({
  id: "gtm.run-initiative-pipeline",
  // Three steps × actor + verdict × ~120s headroom + retry overhead.
  // Trigger.dev caps maxDuration at the project level, so this is a
  // soft target; the hard budget is RUN_STEP_TIMEOUT_MS per call.
  maxDuration: 3600,
  run: async (
    payload: PipelinePayload,
    { ctx },
  ): Promise<{ status: "completed" | "failed"; failedAtSlug?: string }> => {
    const { initiativeId, gatingMode = "auto", startFrom } = payload;
    const triggerRunId = ctx.run.id;

    const startIdx = startFrom
      ? STEPS.findIndex((s) => s.actor === startFrom)
      : 0;
    if (startIdx < 0) {
      const reason = `unknown_start_from:${startFrom ?? ""}`;
      await reportPipelineFailed(initiativeId, triggerRunId, "startFrom", reason);
      throw new Error(reason);
    }

    logger.info("gtm pipeline kicked off", {
      initiativeId,
      gatingMode,
      startFrom,
      startIdx,
    });

    // Upstream outputs accumulate across pipeline steps so each actor
    // sees the prior actors' outputs (channel-step-materializer reads
    // sequence-definer; audience-materializer reads channel-step-materializer's
    // executed; master-strategist reads sequence-definer; per-recipient
    // creative reads master-strategist).
    const stepUpstream: Record<string, unknown> = {};

    for (let i = startIdx; i < STEPS.length; i++) {
      const stepCfg = STEPS[i];
      const { actor, verdict } = stepCfg;

      // Manual-mode gate between steps (not before the very first).
      if (gatingMode === "manual" && i > startIdx) {
        if (ENABLE_MANUAL_GATE) {
          logger.info("manual-mode gate — waiting for advance token", {
            initiativeId,
            before: actor,
          });
          const token = await wait.createToken({
            idempotencyKey: `advance:${initiativeId}:${actor}`,
            timeout: "24h",
            tags: [`gtm:${initiativeId}`, `step:${actor}`],
          });
          await wait.forToken(token);
        } else {
          logger.warn(
            "manual-mode requested but ENABLE_MANUAL_GATE=false — proceeding without pause",
            { initiativeId, before: actor },
          );
        }
      }

      // Fanout step: dispatch the per-recipient child task once per
      // (recipient × DM step) tuple. Child tasks run the actor →
      // verdict loop scoped to a single (recipient, step) pair.
      // Pipeline-level success is tolerant of per-recipient failures
      // up to FANOUT_FAILURE_THRESHOLD; above that, the parent
      // pipeline is failed so the operator can iterate the prompt.
      if (stepCfg.isFanout) {
        let targetsResp: FanoutTargetsResponse;
        try {
          targetsResp = await callHqx<FanoutTargetsResponse>(
            `/internal/gtm/initiatives/${initiativeId}/fanout-targets`,
            { agent_slug: actor },
          );
        } catch (err) {
          await reportPipelineFailed(
            initiativeId,
            triggerRunId,
            actor,
            `fanout_targets_unreachable:${String(err).slice(0, 100)}`,
          );
          return { status: "failed", failedAtSlug: actor };
        }
        if (!targetsResp.items.length) {
          await reportPipelineFailed(
            initiativeId,
            triggerRunId,
            actor,
            "fanout_targets_empty",
          );
          return { status: "failed", failedAtSlug: actor };
        }

        logger.info("dispatching per-recipient fanout", {
          initiativeId,
          step: actor,
          targetCount: targetsResp.items.length,
        });

        const batchResult = await runPerRecipientCreative.batchTriggerAndWait(
          targetsResp.items.map((t) => ({
            payload: {
              initiativeId,
              recipientId: t.recipient_id,
              channelCampaignStepId: t.channel_campaign_step_id,
              upstream: stepUpstream,
            } satisfies PerRecipientPayload,
          })),
        );

        // batchTriggerAndWait returns {runs: BatchTriggerAndWaitItem[]}, each
        // with {ok: bool, output: T | undefined, error?}. We collapse to a
        // PerRecipientResult; non-ok items count as failures.
        const totalRuns = targetsResp.items.length;
        let failedRuns = 0;
        for (const r of batchResult.runs) {
          if (!r.ok) {
            failedRuns += 1;
            continue;
          }
          const out = r.output as PerRecipientResult | undefined;
          if (!out || out.status !== "shipped") {
            failedRuns += 1;
          }
        }
        const failureRate = failedRuns / totalRuns;
        logger.info("per-recipient fanout finished", {
          initiativeId,
          step: actor,
          totalRuns,
          failedRuns,
          failureRate,
        });
        if (failureRate > FANOUT_FAILURE_THRESHOLD) {
          await reportPipelineFailed(
            initiativeId,
            triggerRunId,
            actor,
            `fanout_high_failure_rate:${(failureRate * 100).toFixed(0)}%`,
          );
          return { status: "failed", failedAtSlug: actor };
        }
        continue;
      }

      let actorOutput: unknown = null;
      let lastVerdict: VerdictOutput | null = null;
      const upstream: Record<string, unknown> = stepUpstream;

      let attempt = 0;
      while (attempt <= MAX_VERDICT_RETRIES) {
        // 1. Run actor (with hint from prior verdict if retrying).
        const actorRun = await callRunStep(initiativeId, actor, {
          hint: attempt > 0 ? lastVerdict?.redo_with ?? null : null,
          upstream_outputs: upstream,
        });
        if (actorRun.status !== "succeeded") {
          await reportPipelineFailed(
            initiativeId,
            triggerRunId,
            actor,
            `actor_run_failed:${actorRun.status}`,
          );
          throw new Error(`actor ${actor} failed: ${actorRun.status}`);
        }
        actorOutput = actorRun.output_blob?.value ?? null;
        upstream[actor] = actorOutput;

        // 2. Run verdict against the actor's most recent output.
        const verdictRun = await callRunStep(initiativeId, verdict, {
          upstream_outputs: upstream,
        });
        if (verdictRun.status !== "succeeded") {
          await reportPipelineFailed(
            initiativeId,
            triggerRunId,
            verdict,
            `verdict_run_failed:${verdictRun.status}`,
          );
          throw new Error(`verdict ${verdict} failed: ${verdictRun.status}`);
        }
        lastVerdict = (verdictRun.output_blob?.value as VerdictOutput) ?? {
          ship: false,
          issues: [],
          redo_with: null,
        };
        upstream[verdict] = lastVerdict;

        if (lastVerdict.ship) {
          logger.info("verdict shipped", {
            actor,
            verdict,
            attempts: attempt + 1,
          });
          break;
        }
        logger.warn("verdict blocked actor draft", {
          actor,
          verdict,
          attempt,
          issues: lastVerdict.issues,
        });
        attempt++;
      }

      if (!lastVerdict?.ship) {
        await reportPipelineFailed(
          initiativeId,
          triggerRunId,
          actor,
          "verdict_block_after_retries",
        );
        return { status: "failed", failedAtSlug: actor };
      }
    }

    await reportPipelineCompleted(initiativeId, triggerRunId);
    logger.info("gtm pipeline completed", { initiativeId });
    return { status: "completed" };
  },
});
