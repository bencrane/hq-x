// dmaas.reconcile_postgrid_pieces — daily sweep against PostGrid's source
// of truth for active direct_mail steps. Mirrors dmaas-reconcile-lob-pieces.ts
// for the PostGrid provider. Surfaces any piece-count drift (dropped webhooks,
// etc.) as drift events; gap-fill handled inside the projector.
//
// PostGrid IDs use verbose prefixes (letter_*, postcard_*, selfmailer_*) rather
// than Lob's three-letter convention. The reconcile endpoint on hq-x handles
// provider-aware ID matching.

import { logger, schedules } from "@trigger.dev/sdk/v3";
import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_AT_07_UTC = "0 7 * * *";

type Result = {
  enabled: boolean;
  rows_scanned: number;
  rows_touched: number;
  drift_found: number;
};

export const dmaasReconcilePostgridPieces = schedules.task({
  id: "dmaas.reconcile_postgrid_pieces",
  cron: CRON_DAILY_AT_07_UTC,
  maxDuration: 1200,
  run: async (_payload, { ctx }) => {
    const result = await callHqx<Result>(
      "/internal/dmaas/reconcile/postgrid",
      { trigger_run_id: ctx.run.id },
    );
    logger.info("reconcile.postgrid", result);
    return result;
  },
});
