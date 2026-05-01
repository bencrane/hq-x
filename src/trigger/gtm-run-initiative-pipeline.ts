// gtm.run-initiative-pipeline — sequences the post-payment GTM pipeline
// across three actor/verdict pairs:
//   1. gtm-sequence-definer       → gtm-sequence-definer-verdict
//   2. gtm-master-strategist      → gtm-master-strategist-verdict
//   3. gtm-per-recipient-creative → gtm-per-recipient-creative-verdict
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
}

const STEPS: PipelineStep[] = [
  { actor: "gtm-sequence-definer", verdict: "gtm-sequence-definer-verdict" },
  { actor: "gtm-master-strategist", verdict: "gtm-master-strategist-verdict" },
  {
    actor: "gtm-per-recipient-creative",
    verdict: "gtm-per-recipient-creative-verdict",
  },
];

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

    for (let i = startIdx; i < STEPS.length; i++) {
      const { actor, verdict } = STEPS[i];

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

      let actorOutput: unknown = null;
      let lastVerdict: VerdictOutput | null = null;
      const upstream: Record<string, unknown> = {};
      // Carry forward outputs from prior steps so verdicts + later
      // actors can read them.
      for (let j = 0; j < i; j++) {
        // Note: prior steps' outputs are captured in the DB. We
        // don't re-load them here — when MAX_VERDICT_RETRIES > 1
        // and we want to feed the prior actor's output to the
        // next actor, we'll either GET them via /runs or pass them
        // through the in-process upstream map. For v0 (single-pass),
        // each step's actor only needs its own most-recent output
        // for the paired verdict.
      }

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
