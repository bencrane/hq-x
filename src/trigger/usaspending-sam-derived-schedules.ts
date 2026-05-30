// usaspending-sam-derived-schedules — the 15 derived/chained/observability
// USAspending + SAM feeds, migrated from native modal.Cron to Trigger.dev
// scheduling (2026-05-29). Each task POSTs the shared Modal dispatch endpoint,
// which spawns the target Modal function by name and returns its call_id.
//
// FIRE-AND-FORGET: all compute runs inside Modal — never in Trigger.dev. The
// fan-out orchestrator's `.map()` also stays in Modal; Trigger only kicks it off.
// Unauthenticated for now (pilot posture); proxy-auth gets added on the shared
// dispatcher before broadening exposure.
//
// Each task is gated by the operator control plane (ops.scheduled_tasks): the
// shared `task()` factory asks hq-x whether the schedule is enabled before
// dispatching to Modal, and skips (fail-open) when disabled. See
// ./lib/scheduled-gate.

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { passesGate, SKIPPED_DISABLED } from "./lib/scheduled-gate";

const DISPATCH_URL =
  "https://bencrane--data-engine-x-trigger-dispatch-dispatch.modal.run";

async function spawnModal(app: string, fn: string, triggerRunId: string) {
  logger.info("modal-dispatch: spawning", { app, fn, trigger_run_id: triggerRunId });
  const resp = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ app, function: fn }),
  });
  if (!resp.ok) {
    throw new Error(`modal-dispatch ${app}::${fn} -> ${resp.status}: ${await resp.text()}`);
  }
  const data = (await resp.json()) as { call_id: string };
  logger.info("modal-dispatch: spawned", { app, fn, call_id: data.call_id, trigger_run_id: triggerRunId });
  return data;
}

const task = (id: string, cron: string, app: string, fn: string) =>
  schedules.task({
    id,
    cron: { pattern: cron, timezone: "UTC" },
    maxDuration: 120,
    run: async (_p, { ctx }) => {
      if (!(await passesGate(id))) return SKIPPED_DISABLED;
      return spawnModal(app, fn, ctx.run.id);
    },
  });

// ── Pattern-A lance emitters ────────────────────────────────────────────────
export const usaspendingContractsLanceEmit = task("usaspending-contracts-lance-emit.monthly", "0 7 16 * *", "data-engine-x-usaspending-contracts-lance-emit", "emit");
export const usaspendingRecipientGrainLanceEmit = task("usaspending-recipient-grain-lance-emit.daily", "0 4 * * *", "data-engine-x-usaspending-recipient-grain-lance-emit", "emit");
export const samOppsActiveLanceEmit = task("sam-opps-active-lance-emit.daily", "30 12 * * *", "data-engine-x-sam-opps-active-lance-emit", "emit");

// ── Pattern-B SOS entity bridges / cohorts ──────────────────────────────────
export const samSosCaEntitiesBridge = task("sam-sos-ca-entities-bridge.weekly", "0 13 * * 1", "data-engine-x-sam-sos-ca-entities-bridge", "weekly_refresh");
export const samSosCaPrincipalsCohort = task("sam-sos-ca-principals-cohort.weekly", "0 14 * * 1", "data-engine-x-sam-sos-ca-principals-cohort", "weekly_refresh");
export const samSosFlEntitiesBridge = task("sam-sos-fl-entities-bridge.weekly", "0 13 * * 2", "data-engine-x-sam-sos-fl-entities-bridge", "weekly_refresh");
export const samSosFlOfficersCohort = task("sam-sos-fl-officers-cohort.weekly", "0 14 * * 3", "data-engine-x-sam-sos-fl-officers-cohort", "weekly_refresh");
export const samSosNyEntitiesBridge = task("sam-sos-ny-entities-bridge.weekly", "0 14 * * 1", "data-engine-x-sam-sos-ny-entities-bridge", "weekly_refresh");
export const usaspendingSosFlOwnerBridge = task("usaspending-sos-fl-owner-bridge.weekly", "0 16 * * 4", "data-engine-x-usaspending-sos-fl-owner-bridge", "weekly_refresh");
export const usaspendingSosNyOwnerBridge = task("usaspending-sos-ny-owner-bridge.weekly", "0 15 * * 1", "data-engine-x-usaspending-sos-ny-owner-bridge", "weekly_refresh");

// ── Fan-out orchestrator (schedule only; the .map() fan-out stays in Modal) ──
export const usaspendingContractsLanceRebuild = task("usaspending-contracts-lance-rebuild.daily", "0 8 * * *", "data-engine-x-usaspending-api-daily-lance-rebuild", "run_contracts_lance_daily");

// ── Derived / observability ─────────────────────────────────────────────────
export const usaspendingDerivedViews = task("usaspending-derived-views.daily", "30 8 * * *", "data-engine-x-usaspending-derived-views-daily", "run_derived_views_daily");
export const usaspendingRecipientFeatures = task("usaspending-recipient-features.monthly", "0 9 16 * *", "data-engine-x-usaspending-recipient-features", "emit");
export const usaspendingDailyVerify = task("usaspending-daily-verify.daily", "0 8 * * *", "data-engine-x-usaspending-daily-verify", "run_daily_verify");
export const usaspendingWeeklyCoverage = task("usaspending-weekly-coverage.weekly", "30 12 * * 1", "data-engine-x-usaspending-weekly-coverage", "run_weekly_coverage");
