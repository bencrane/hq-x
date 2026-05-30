#!/usr/bin/env bash
# Rollback harness for /scope cycle `usaspending-poison-file-class-fix-v1`.
#
# Authored 2026-05-13 UTC per directive
# /Users/benjamincrane/Desktop/hq/directives/2026-05-13-usaspending-poison-file-class-fix-v1.md
# Refined 2026-05-13 UTC by Stage 3.A audit subagent.
#
# Rollback strategy is asymmetric per surface (see `## Rollback harness` in
# the directive). This script prints the operator-runnable commands; it does
# NOT execute them unconditionally. The destructive ones (R2 deletes in s8)
# are explicitly irreversible — accepted at directive-signoff time.
#
# Per audit invariant: every code surface's rollback is satisfiable. s8 is
# accepted as irreversible. s9 is idempotent. Status: review-ready, no
# blocked-no-rollback gate triggered.

set -euo pipefail

MERGE_SHA="${MERGE_SHA:-}"

if [[ -z "$MERGE_SHA" ]]; then
  echo "USAGE: MERGE_SHA=<merge-commit-sha> ./usaspending-poison-file-class-fix-v1-rollback.sh"
  echo ""
  echo "Set MERGE_SHA to the merge commit of the cycle PR. The script will"
  echo "emit the per-surface rollback commands for operator execution."
  exit 2
fi

cat <<EOF
==> Rollback plan for usaspending-poison-file-class-fix-v1 (merge SHA: $MERGE_SHA)

Single-PR cycle. Code surfaces s1-s7 + s5/s6 + s10 all revert via
\`git revert\`. Apply ONCE, then redeploy each subapp.

--- 1. Code revert (everything except s8) -----------------------------

  cd ~/hq-all
  git revert --no-edit $MERGE_SHA
  git push origin HEAD:main

--- 2. Modal redeploys (writer fix + verify cron + catalog refresh) ---

  cd ~/hq-all/apps/data-engine-x
  # Enumerate touched usaspending-* apps (writer-fix consumers):
  for APP in \\
      usaspending_api_daily_app \\
      usaspending_daily_app \\
      usaspending_monthly_app \\
      usaspending_contracts_lance_emit_app \\
      usaspending_weekly_coverage_app \\
      usaspending_daily_verify_app \\
      data_source_catalog_refresh_app; do
    doppler run --project hq-all --config prd -- modal deploy "modal/\$APP.py"
  done

  # If this cycle added all_sources_verify_app, stop the new cron in Modal
  # (git revert removes the file from main but Modal keeps the deployed app
  # until explicitly stopped):
  doppler run --project hq-all --config prd -- modal app stop data-engine-x-all-sources-verify || true

--- 3. HQ-command rollback (Railway auto-redeploy) --------------------

  # Railway watches apps/hq-command/** and auto-redeploys on the revert push.
  cd ~/hq-all/apps/hq-command
  railway status --json 2>/dev/null | jq -r '.environments.edges[].node.serviceInstances.edges[].node | {commitHash: .latestDeployment.meta.commitHash, status: .latestDeployment.status}'

  # Expected: commitHash == post-revert SHA, status == SUCCESS within 5-10 min.

--- 4. Supabase migration rollback (synthetic sandbox source) ---------

  # The cycle's migration adds the _sandbox_dashboard_probe row to
  # ops.data_source_catalog. The git revert above removes the migration FILE
  # but does NOT undo the INSERT. Manual undo (optional — the row is benign):
  #
  #   cd ~/hq-all/apps/data-engine-x
  #   doppler run --project hq-all --config prd -- bash -c \\
  #     'psql "\$DEX_DB_URL_DIRECT" -c "DELETE FROM ops.data_source_catalog WHERE source_slug = '\''_sandbox_dashboard_probe'\'';"'

--- 5. s8 (R2 deletions) — IRREVERSIBLE -------------------------------

  Per directive \`## Rollback harness\`, the deleted 0-byte poison files
  cannot be restored. They were intentionally garbage. Audit's R2 forensics
  enumerates every key deleted (see directive \`## Audit plan\` s8); cross-
  reference if needed.

--- 6. s9 (manual cron re-invocation) — NO ROLLBACK NEEDED ------------

  Each re-invocation writes a NEW ops.data_source_ingest_runs row
  (append-only). No state to undo.

--- 7. Verify rollback ------------------------------------------------

  # Confirm Modal apps re-deployed:
  modal app list --json 2>/dev/null | jq -r '.[] | select((.Description // "" | tostring) | startswith("data-engine-x-usaspending")) | {desc: .Description, state: .State}'

  # Confirm hq-command latestDeployment commit matches the revert:
  cd ~/hq-all/apps/hq-command
  railway status --json | jq -r '.environments.edges[].node.serviceInstances.edges[].node.latestDeployment.meta.commitHash'

  # Confirm dashboard no longer returns the new fields (pre-fix shape).
  # Auth: DEX-direct + super-admin API key (the hq-command proxy strips
  # client Authorization headers — see harness s5 comment).
  cd ~/hq-all/apps/data-engine-x
  DEX_URL="\${DEX_API_BASE_URL:-https://api.dataengine.run}"
  doppler run --project hq-all --config prd -- bash -c "
    curl -fsS -H \"Authorization: Bearer \\\$DEX_SERVICE_TOKEN\" \"\$DEX_URL/api/internal/data-source-catalog\"
  " | jq '.data.sources[0] | (has("freshness_state") or has("poison_files"))'
  # Expected: false (fields removed by revert).

EOF
exit 0
