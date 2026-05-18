// txdot-letting-monthly — monthly Socrata → R2 ingest dispatch.
// Cron: 1st of month, 08:00 UTC. POSTs Modal REST API to fire
// data-engine-x-txdot-letting-ingest::ingest_socrata_to_r2.
// Auth: MODAL_TOKEN_ID + MODAL_TOKEN_SECRET (configured in hq-x Trigger.dev project
//       proj_khmvxxrpyloqmnivdetu — NOT the deprecated DEX project).
// Auth via MODAL_TOKEN_ID/SECRET only — NOT the deprecated M2M token boundary.
//
// ── OPERATOR-GATED CAVEAT (Review notes Finding #5 / Audit-resolved clarification #2) ──
//
// The URL shape `POST https://api.modal.com/v1/spawn/{app}/{fn}` is NOT documented in
// Modal's public REST API guide. Modal's documented out-of-process invocation paths are:
//   (a) Modal Python/Node SDK (gRPC under the hood; not pure REST), or
//   (b) @modal.web_endpoint() per-function HTTPS URL.
//
// The first cron fire (2026-06-01 08:00 UTC) may 404 if this URL shape is wrong.
// Failure mode is non-destructive — Trigger.dev logs the throw; no data corruption.
//
// Operator MUST verify the URL pre-merge by running:
//   curl -X POST https://api.modal.com/v1/spawn/data-engine-x-txdot-letting-ingest/ingest_socrata_to_r2 \
//     -H "Modal-Token-Id: <MODAL_TOKEN_ID>" \
//     -H "Modal-Token-Secret: <MODAL_TOKEN_SECRET>" \
//     -H "Content-Type: application/json" \
//     -d '{"args":["2026-05-18"],"kwargs":{}}'
//
// If URL 404s, fall back to adding @modal.web_endpoint(method="POST") to
// ingest_socrata_to_r2 in apps/data-engine-x/modal/txdot_letting_ingest_app.py
// (additional surface; would require directive amendment) and rewire this task to
// fetch the Modal-issued HTTPS URL instead.
//
// Operator must also confirm MODAL_TOKEN_ID + MODAL_TOKEN_SECRET are configured
// in the Trigger.dev dashboard for proj_khmvxxrpyloqmnivdetu before merge.

import { logger, schedules } from "@trigger.dev/sdk/v3";

const MODAL_APP_NAME = "data-engine-x-txdot-letting-ingest";
const MODAL_FUNCTION_NAME = "ingest_socrata_to_r2";
const MODAL_API_BASE = "https://api.modal.com";

const requireEnv = (name: string): string => {
  const v = process.env[name];
  if (!v) throw new Error(`${name} must be set in the Trigger.dev dashboard.`);
  return v;
};

export const txdotLettingMonthly = schedules.task({
  id: "txdot-letting.monthly",
  cron: "0 8 1 * *",  // 1st of month, 08:00 UTC
  maxDuration: 600,
  run: async (_payload, { ctx }) => {
    const tokenId = requireEnv("MODAL_TOKEN_ID");
    const tokenSecret = requireEnv("MODAL_TOKEN_SECRET");
    const snapshotDate = new Date().toISOString().slice(0, 10);  // YYYY-MM-DD UTC

    // See caveat block above re: URL shape uncertainty.
    const url = `${MODAL_API_BASE}/v1/spawn/${MODAL_APP_NAME}/${MODAL_FUNCTION_NAME}`;

    logger.info("txdot-letting.monthly: spawning Modal ingest", {
      url,
      snapshot_date: snapshotDate,
      trigger_run_id: ctx.run.id,
    });

    const resp = await fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Modal-Token-Id": tokenId,
        "Modal-Token-Secret": tokenSecret,
      },
      body: JSON.stringify({ args: [snapshotDate], kwargs: {} }),
    });

    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(
        `Modal spawn failed ${resp.status}: ${text} — check URL shape caveat in file header`
      );
    }

    const data = (await resp.json()) as { call_id: string };
    logger.info("txdot-letting.monthly: Modal call spawned", {
      call_id: data.call_id,
      snapshot_date: snapshotDate,
      trigger_run_id: ctx.run.id,
    });
    return { call_id: data.call_id, snapshot_date: snapshotDate };
  },
});
