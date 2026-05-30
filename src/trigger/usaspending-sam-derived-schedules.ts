// usaspending-sam-derived-schedules — the 15 derived/chained/observability
// USAspending + SAM feeds, migrated from native modal.Cron to Trigger.dev
// scheduling (2026-05-29). Each task POSTs the shared Modal dispatch endpoint,
// which spawns the target Modal function by name and returns its call_id.
//
// FIRE-AND-FORGET: all compute runs inside Modal — never in Trigger.dev. The
// fan-out orchestrator's `.map()` also stays in Modal; Trigger only kicks it off.
// Unauthenticated for now (pilot posture); proxy-auth gets added on the shared
// dispatcher before broadening exposure.

import { logger, schedules } from "@trigger.dev/sdk/v3";

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

// ── Pattern-A lance emitters ────────────────────────────────────────────────
export const usaspendingContractsLanceEmit = schedules.task({
  id: "usaspending-contracts-lance-emit.monthly",
  cron: { pattern: "0 7 16 * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-contracts-lance-emit", "emit", ctx.run.id),
});
export const usaspendingRecipientGrainLanceEmit = schedules.task({
  id: "usaspending-recipient-grain-lance-emit.daily",
  cron: { pattern: "0 4 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-recipient-grain-lance-emit", "emit", ctx.run.id),
});
export const samOppsActiveLanceEmit = schedules.task({
  id: "sam-opps-active-lance-emit.daily",
  cron: { pattern: "30 12 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-opps-active-lance-emit", "emit", ctx.run.id),
});

// ── Pattern-B SOS entity bridges / cohorts ──────────────────────────────────
export const samSosCaEntitiesBridge = schedules.task({
  id: "sam-sos-ca-entities-bridge.weekly",
  cron: { pattern: "0 13 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-sos-ca-entities-bridge", "weekly_refresh", ctx.run.id),
});
export const samSosCaPrincipalsCohort = schedules.task({
  id: "sam-sos-ca-principals-cohort.weekly",
  cron: { pattern: "0 14 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-sos-ca-principals-cohort", "weekly_refresh", ctx.run.id),
});
export const samSosFlEntitiesBridge = schedules.task({
  id: "sam-sos-fl-entities-bridge.weekly",
  cron: { pattern: "0 13 * * 2", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-sos-fl-entities-bridge", "weekly_refresh", ctx.run.id),
});
export const samSosFlOfficersCohort = schedules.task({
  id: "sam-sos-fl-officers-cohort.weekly",
  cron: { pattern: "0 14 * * 3", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-sos-fl-officers-cohort", "weekly_refresh", ctx.run.id),
});
export const samSosNyEntitiesBridge = schedules.task({
  id: "sam-sos-ny-entities-bridge.weekly",
  cron: { pattern: "0 14 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-sam-sos-ny-entities-bridge", "weekly_refresh", ctx.run.id),
});
export const usaspendingSosFlOwnerBridge = schedules.task({
  id: "usaspending-sos-fl-owner-bridge.weekly",
  cron: { pattern: "0 16 * * 4", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-sos-fl-owner-bridge", "weekly_refresh", ctx.run.id),
});
export const usaspendingSosNyOwnerBridge = schedules.task({
  id: "usaspending-sos-ny-owner-bridge.weekly",
  cron: { pattern: "0 15 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-sos-ny-owner-bridge", "weekly_refresh", ctx.run.id),
});

// ── Fan-out orchestrator (schedule only; the .map() fan-out stays in Modal) ──
export const usaspendingContractsLanceRebuild = schedules.task({
  id: "usaspending-contracts-lance-rebuild.daily",
  cron: { pattern: "0 8 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-api-daily-lance-rebuild", "run_contracts_lance_daily", ctx.run.id),
});

// ── Derived / observability ─────────────────────────────────────────────────
export const usaspendingDerivedViews = schedules.task({
  id: "usaspending-derived-views.daily",
  cron: { pattern: "30 8 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-derived-views-daily", "run_derived_views_daily", ctx.run.id),
});
export const usaspendingRecipientFeatures = schedules.task({
  id: "usaspending-recipient-features.monthly",
  cron: { pattern: "0 9 16 * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-recipient-features", "emit", ctx.run.id),
});
export const usaspendingDailyVerify = schedules.task({
  id: "usaspending-daily-verify.daily",
  cron: { pattern: "0 8 * * *", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-daily-verify", "run_daily_verify", ctx.run.id),
});
export const usaspendingWeeklyCoverage = schedules.task({
  id: "usaspending-weekly-coverage.weekly",
  cron: { pattern: "30 12 * * 1", timezone: "UTC" },
  maxDuration: 120,
  run: async (_p, { ctx }) => spawnModal("data-engine-x-usaspending-weekly-coverage", "run_weekly_coverage", ctx.run.id),
});
