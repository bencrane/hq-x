// batch-a-schedules — SEC EDGAR/DERA (9) + epiq ingest legs (5) + gov singletons (16),
// migrated from native modal.Cron to Trigger.dev scheduling (2026-05-30, Batch A).
// Each task POSTs the shared Modal dispatch endpoint, which spawns the target
// Modal function by name and returns its call_id. FIRE-AND-FORGET: all compute
// runs in Modal — never in Trigger.dev. Workers self-default their date/window,
// so no kwargs are passed. Crons mirror the original modal.Cron values exactly
// (UTC). epiq's 5 legs preserve their intra-app clock stagger; the 2 epiq bridges
// + bdc_soi_parse_v2 are intentionally NOT migrated (chained — stay on modal.Cron).

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
  if (!data.call_id) throw new Error(`modal-dispatch ${app}::${fn}: no call_id in response`);
  logger.info("modal-dispatch: spawned", { app, fn, call_id: data.call_id, trigger_run_id: triggerRunId });
  return data;
}

const task = (id: string, cron: string, app: string, fn: string) =>
  schedules.task({
    id,
    cron: { pattern: cron, timezone: "UTC" },
    maxDuration: 120,
    run: async (_p, { ctx }) => spawnModal(app, fn, ctx.run.id),
  });

// ── SEC EDGAR / DERA (9) ─────────────────────────────────────────────────────
export const secEdgarForm8k = task("sec-edgar-form-8k.scan", "30 3,9,15,21 * * *", "data-engine-x-sec-edgar-form-8k", "run_form_8k_backfill");
export const secEdgarForm10k = task("sec-edgar-form-10k.quarterly", "30 3 1 */3 *", "data-engine-x-sec-edgar-form-10k", "run_form_10k_backfill");
export const secEdgarForm13f = task("sec-edgar-form-13f.scan", "0 2,8,14,20 * * *", "data-engine-x-sec-edgar-form-13f", "run_form_13f_backfill");
export const secEdgarFormAbs15g = task("sec-edgar-form-abs-15g.scan", "0 3,9,15,21 * * *", "data-engine-x-sec-edgar-form-abs-15g", "run_form_abs_15g_backfill");
export const secEdgarSchedule13d13g = task("sec-edgar-schedule-13d-13g.scan", "0 1,7,13,19 * * *", "data-engine-x-sec-edgar-schedule-13d-13g", "run_schedule_13d_13g_backfill");
export const secEdgarDef14a = task("sec-edgar-def-14a.scan", "0 */6 * * *", "data-engine-x-sec-edgar-def-14a", "run_def_14a_backfill");
export const secBdcSoi = task("sec-bdc-soi.monthly", "0 14 8 * *", "data-engine-x-sec-bdc-soi", "monthly_refresh");
export const secDeraFormD = task("sec-dera-form-d.daily", "0 4 * * *", "data-engine-x-sec-dera-form-d", "daily_incremental");
export const secDeraFsds = task("sec-dera-fsds.daily", "0 4 * * *", "data-engine-x-sec-dera-fsds", "daily_incremental");

// ── epiq — 5 ingest legs (intra-app clock stagger preserved) ────────────────
export const epiqCases = task("epiq-cases.daily", "0 1 * * *", "data-engine-x-epiq-ingest", "daily_cases_refresh");
export const epiqClaims = task("epiq-claims.daily", "0 2 * * *", "data-engine-x-epiq-ingest", "daily_claims_refresh");
export const epiqDockets = task("epiq-dockets.daily", "30 2 * * *", "data-engine-x-epiq-ingest", "daily_dockets_refresh");
export const epiqClaimsResolved = task("epiq-claims-resolved.daily", "0 3 * * *", "data-engine-x-epiq-ingest", "daily_claims_resolved_refresh");
export const epiqCreditors = task("epiq-creditors.daily", "15 3 * * *", "data-engine-x-epiq-ingest", "daily_creditors_refresh");

// ── Gov singletons (16) ──────────────────────────────────────────────────────
export const azAppRfpPublic = task("az-app-rfp-public.daily", "0 17 * * *", "data-engine-x-az-app-rfp-public", "daily_az_app_rfp_refresh");
export const btsT100Segment = task("bts-t100-segment.monthly", "0 3 5 * *", "data-engine-x-bts-t100-segment-ingest", "run_ingest");
export const caltransCcop = task("caltrans-ccop.daily", "0 14 * * *", "data-engine-x-caltrans-ccop", "daily_ccop_refresh");
export const clinicaltrialsDeviceStudies = task("clinicaltrials-device-studies.weekly", "0 14 * * 1", "data-engine-x-clinicaltrials-device-studies", "weekly_refresh");
export const faaAircraftRegistry = task("faa-aircraft-registry.weekly", "0 6 * * 4", "data-engine-x-faa-aircraft-registry-ingest", "run_ingest");
export const faaAirmen = task("faa-airmen.monthly", "0 0 5 * *", "data-engine-x-faa-airmen-ingest", "run_ingest");
export const fdicCallReport = task("fdic-call-report.quarterly", "0 6 15 2,5,8,11 *", "data-engine-x-fdic-call-report-ingest", "run_quarterly_ingest");
export const flCilbDaily = task("fl-cilb.daily", "0 11 * * *", "data-engine-x-fl-cilb-daily", "run_daily");
export const grantsGovDaily = task("grants-gov.daily", "0 8 * * *", "data-engine-x-grants-gov-daily", "run_grants_gov_daily");
export const nyDataConstruction = task("ny-data-construction.weekly", "0 12 * * 1", "data-engine-x-ny-data-construction-ingest", "run_backfill");
export const nyNycLocalAwards = task("ny-nyc-local-awards.weekly", "0 13 * * 1", "data-engine-x-ny-nyc-local-awards-ingest", "run_backfill");
export const openfdaDevice = task("openfda-device.weekly", "0 14 * * 1", "data-engine-x-openfda-device", "weekly_ingest_and_emit");
export const opscSchoolFacilityFunding = task("opsc-school-facility-funding.weekly", "0 16 * * 1", "data-engine-x-opsc-school-facility-funding", "weekly_opsc_refresh");
export const overturePlaces = task("overture-places.monthly", "0 0 5 * *", "data-engine-x-overture-places-ingest", "run_ingest");
export const sbirAwards = task("sbir-awards.monthly", "0 6 5 * *", "data-engine-x-sbir-awards-monthly-ingest", "run_sbir_awards_monthly_ingest");
export const usptoPatents = task("uspto-patents.monthly", "0 8 1 * *", "data-engine-x-uspto-patents", "ingest");
