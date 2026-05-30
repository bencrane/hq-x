// Fail-open operator gate for declarative scheduled tasks.
//
// Trigger.dev's management API cannot deactivate DECLARATIVE schedules
// (cron-in-code) — only IMPERATIVE schedules created via schedules.create() can
// be toggled. So the operator's enable/disable switch lives in hq-x
// (ops.scheduled_tasks.is_enabled) and every scheduled task asks hq-x "am I
// enabled?" before doing its work, via POST /internal/scheduled-tasks/gate.
//
// FAIL-OPEN: on ANY error (hq-x down, timeout, secret misconfig) passesGate
// returns true so the task RUNS. An hq-x blip must never be the silent reason an
// SLA-critical ingest stops firing. The cost of failing open is a run that
// should have been skipped; the cost of failing closed is a missed ingest that
// breaks a downstream direct-mail SLA — the asymmetry is the whole design.

import { logger } from "@trigger.dev/sdk/v3";

import { callHqx } from "./hqx-client";

export interface GateDecision {
  run: boolean;
  enabled: boolean;
  reason?: string | null;
}

// Return value a gated task yields when the operator has disabled it. Surfaces
// an explicit "skipped (disabled)" in the Trigger.dev run output rather than a
// silent no-op, so the run history reads truthfully.
export const SKIPPED_DISABLED = {
  skipped: true as const,
  reason: "disabled_by_operator" as const,
};

export async function passesGate(taskId: string): Promise<boolean> {
  try {
    const decision = await callHqx<GateDecision>(
      "/internal/scheduled-tasks/gate",
      { task_id: taskId },
      { timeoutMs: 10_000 },
    );
    if (!decision.run) {
      logger.warn("scheduled-gate: disabled by operator — skipping", {
        task_id: taskId,
        reason: decision.reason,
      });
    }
    return decision.run;
  } catch (err) {
    logger.warn("scheduled-gate: check failed, failing OPEN (running)", {
      task_id: taskId,
      error: String(err),
    });
    return true;
  }
}
