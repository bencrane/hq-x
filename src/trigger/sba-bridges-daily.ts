// sba-bridges-daily — fires the SBA/PDL/SAM/USAspending + UCC/GLEIF identity-spine
// Lance bridge pipeline every day at 09:00 UTC, after the matching-engine-daily
// cron at 08:00 UTC. Bridges must be fresh BEFORE the matching engine reads
// them; this cron runs AFTER — the matching engine picks up the updated
// bridges on its next scheduled run.
//
// Extended by ucc-gleif-identity-spine cycle (2026-05-12): now also covers
// GLEIF Level-2 emit + parent-traversal derive + 6 UCC/SBA/GLEIF bridges.
//
// Extended by scorer-enrichment-borrower-ucc-history cycle (2026-05-13):
// adds emit_borrowers_ucc_profile_lance.py (per-borrower UCC-history derive).
//
// Invokes 16 daily-refresh scripts in order via the canonical
// hq-x → DEX path (per memory: app_responsibilities.md). PDL emit is
// NOT in this cron — PDL is a manual-refresh-only dataset.
//
// Scripts run inside DEX's Doppler env (sub-processes by the DEX
// endpoint) in this order:
//   1.  emit_sba_loans_lance.py               (full SBA corpus refresh)
//   2.  emit_sba_borrowers_lance.py           (derive from loans)
//   3.  emit_sba_lenders_lance.py             (derive from loans)
//   4.  emit_sam_entities_lance.py            (SAM monthly entity snapshot)
//   5.  build_bridge_pdl_sba_borrower.py
//   6.  build_bridge_sam_sba_borrower.py
//   7.  build_bridge_usaspending_sba_borrower.py
//   8.  emit_gleif_relationships_lance.py     (GLEIF Level-2 — must precede s9)
//   9.  emit_gleif_with_parent_lance.py       (parent-traversal derive — depends on 8)
//   10. build_bridge_ucc_gleif_lance.py       (depends on existing gleif_lei_records)
//   11. build_bridge_ucc_pdl_lance.py         (depends on 9 + 10)
//   12. build_bridge_ucc_sba_lender_lance.py
//   13. build_bridge_ucc_sba_borrower_lance.py
//   14. build_bridge_sba_lender_gleif_lance.py
//   15. build_bridge_sba_borrower_gleif_lance.py
//   16. emit_borrowers_ucc_profile_lance.py   (depends on 13 — borrower UCC-history derive)

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
  maxDuration: 10800, // 3 hours — extended for ucc-gleif-identity-spine (15 scripts)
  run: async (_payload, { ctx }) => {
    const result = await callHqx<RunDailyResult>(
      "/internal/sba-bridges/run-daily",
      { trigger_run_id: ctx.run.id },
      { timeoutMs: 165 * 60_000 }, // 165 min — leaves 15 min headroom under maxDuration
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
