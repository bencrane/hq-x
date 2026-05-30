// batch-others-schedules — the non-FMCSA remainder: derived lance emitters,
// Pattern-B entity bridges, the GTM signal-fire cron, and infra crons
// (material-change detection, reap-orphans, polaris health, coverage stats),
// migrated from native modal.Cron to Trigger.dev (2026-05-30). Each task POSTs
// the shared Modal dispatch endpoint, which spawns the target Modal function by
// name. FIRE-AND-FORGET: all compute runs in Modal — never in Trigger.dev.
// Workers self-default their date/window, so no kwargs.
//
// DELIBERATELY NOT migrated (held on Modal as the independent watchdog plane):
// alerter_cron (alert delivery + stale-heartbeat detection) and all_sources_verify
// (per-source freshness + failure alerts) — they must keep running independently
// of the scheduler they watch. FMCSA is also untouched.

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
  if (!data.call_id) throw new Error(`modal-dispatch ${app}::${fn}: no call_id`);
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

// ── Derived lance emitters (Pattern-A) ───────────────────────────────────────
export const cmsOpenPaymentsGeneralEmit = task("cms-open-payments-general-emit.daily", "30 7 * * *", "data-engine-x-cms-open-payments-general-lance-emit", "emit");
export const cmsOpenPaymentsResearchEmit = task("cms-open-payments-research-emit.daily", "45 7 * * *", "data-engine-x-cms-open-payments-research-lance-emit", "emit");
export const gleifLeiRecordsEmit = task("gleif-lei-records-emit.weekly", "0 8 * * 0", "data-engine-x-gleif-lei-records-lance-emit", "emit");

// ── Pattern-B entity bridges ─────────────────────────────────────────────────
export const openfdaDevicePdlBridge = task("openfda-device-pdl-bridge.weekly", "0 16 * * 1", "data-engine-x-openfda-device-pdl-bridge", "weekly_refresh");
export const pppSosCaBridge = task("ppp-sos-ca-bridge.weekly", "0 15 * * 1", "data-engine-x-ppp-sos-ca-bridge", "weekly_refresh");
export const pppSosFlBridge = task("ppp-sos-fl-bridge.weekly", "0 15 * * 2", "data-engine-x-ppp-sos-fl-bridge", "weekly_refresh");
export const pppSosNyBridge = task("ppp-sos-ny-bridge.weekly", "0 15 * * 3", "data-engine-x-ppp-sos-ny-bridge", "weekly_refresh");
export const pppUccCaDebtorBridge = task("ppp-ucc-ca-debtor-bridge.weekly", "0 16 * * 4", "data-engine-x-ppp-ucc-ca-debtor-bridge", "weekly_refresh");
export const sbaSosNyOwnerBridge = task("sba-sos-ny-owner-bridge.weekly", "0 16 * * 3", "data-engine-x-sba-sos-ny-owner-bridge", "weekly_refresh");

// ── epiq bridge legs (intra-app clock offsets preserved) ─────────────────────
export const epiqBridgePppBorrower = task("epiq-bridge-ppp-borrower.daily", "30 3 * * *", "data-engine-x-epiq-ingest", "daily_bridge_ppp_borrower_refresh");
export const epiqBridgeUsptoOwner = task("epiq-bridge-uspto-owner.daily", "0 4 * * *", "data-engine-x-epiq-ingest", "daily_bridge_uspto_owner_refresh");

// ── SEC chained re-parse ─────────────────────────────────────────────────────
export const bdcSoiParseV2 = task("bdc-soi-parse-v2.monthly", "0 14 9 * *", "data-engine-x-bdc-soi-parse-v2", "monthly_refresh");

// ── Infra / cross-source (NOT the alerting watchdog — that stays on Modal) ───
export const materialChangeDetection = task("material-change-detection.6h", "0 */6 * * *", "data-engine-x-material-change-cron", "run_cycle");
export const coverageStatsEmit = task("coverage-stats-emit.daily", "0 8 * * *", "data-engine-x-coverage-stats-emit", "run_emit");
export const polarisHealthCheck = task("polaris-health-check.daily", "0 6 * * *", "data-engine-x-polaris-health-check", "health_check");
export const reapOrphans = task("reap-orphans.15m", "*/15 * * * *", "data-engine-x-reap-orphans", "reap_orphans");
export const gtmUsaspendingSignals = task("gtm-usaspending-signals.daily", "0 9 * * *", "data-engine-x-gtm-usaspending-trigger", "run_signals");
