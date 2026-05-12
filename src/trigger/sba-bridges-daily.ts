// sba-bridges-daily — fires the SBA/PDL/SAM/USAspending Lance bridge
// pipeline every day at 09:00 UTC, after the matching-engine-daily cron
// at 08:00 UTC. Bridges must be fresh BEFORE the matching engine reads
// them; this cron runs AFTER — the matching engine picks up the updated
// bridges on its next scheduled run.
//
// Invokes 7 daily-refresh scripts in order via the canonical
// hq-x → DEX path (per memory: app_responsibilities.md). PDL emit is
// NOT in this cron — PDL is a manual-refresh-only dataset.
//
// Scripts run inside DEX's Doppler env (sub-processes by the DEX
// endpoint) in this order:
//   1. emit_sba_loans_lance.py       (full SBA corpus refresh)
//   2. emit_sba_borrowers_lance.py   (derive from loans)
//   3. emit_sba_lenders_lance.py     (derive from loans)
//   4. emit_sam_entities_lance.py    (SAM monthly entity snapshot)
//   5. build_bridge_pdl_sba_borrower.py
//   6. build_bridge_sam_sba_borrower.py
//   7. build_bridge_usaspending_sba_borrower.py

import { logger, schedules } from "@trigger.dev/sdk/v3";

import { callHqx } from "./lib/hqx-client";

const CRON_DAILY_09_UTC = "0 9 * * *";

type RunDailyResult = {
  overall_status: "succeeded" | "failed";
  per_script: Record<string, { exit_code: number; duration_ms: number }>;
  trigger_run_id?: string;
  failing_script?: string;
  stderr_tail?: string;
};

export const sbaBridgesDaily = schedules.task({
  id: "sba-bridges-daily",
  cron: CRON_DAILY_09_UTC,
  maxDuration: 5400, // 90 min — bounded by s10 iteration estimate
  run: async (_payload, { ctx }) => {
    const result = await callHqx<RunDailyResult>(
      "/internal/sba-bridges/run-daily",
      { trigger_run_id: ctx.run.id },
      { timeoutMs: 75 * 60_000 }, // 75 min — leaves 15 min headroom under maxDuration
    );
    logger.info("sba-bridges-daily completed", { ...result, trigger_run_id: ctx.run.id });
    if (result.overall_status !== "succeeded") {
      throw new Error(
        `sba-bridges-daily orchestration failed: ${JSON.stringify(result.per_script)}`,
      );
    }
    return result;
  },
});
