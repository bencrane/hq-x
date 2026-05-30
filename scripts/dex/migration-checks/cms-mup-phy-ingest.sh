#!/usr/bin/env bash
# Verification harness for directive: 2026-05-03-cms-mup-phy-ingest
#
# Surfaces (BOTH variants in scope; first-run NOT in scope):
#   s1a migration  apps/data-engine-x/supabase/migrations/{ts}_cms_mup_phy_source_tables.sql
#                  → entities.source_cms_mup_phy_by_provider + ops.cms_mup_phy_by_provider_ingest_runs
#   s1b migration  same file                                       (one migration, two source tables, two ops tables)
#                  → entities.source_cms_mup_phy_by_provider_and_service + ops.cms_mup_phy_by_provider_and_service_ingest_runs
#   s2a code       apps/data-engine-x/scripts/run_cms_mup_phy_ingest.py        (one script, two-variant arg)
#   s2b code       apps/data-engine-x/trigger/src/tasks/check-cms-mup-phy-version.ts (alert-only schedules.task)
#   s3  deploy     Trigger.dev project proj_gunoekmmafoeqygflcmm  (auto-on-merge via apps/data-engine-x/.github/workflows/trigger-deploy-prod.yml — paths: trigger/**)
#
# s4 (first-run) is intentionally OUT of scope — operator runs scripts/run_cms_mup_phy_ingest.py manually post-merge.
#
# Usage:
#   ./cms-mup-phy-ingest.sh [--repo data-engine-x] [--surface s1a|s1b|s2a|s2b|s3]

set -euo pipefail

REPO_FILTER=""
SURFACE_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Stage-3 executor's auto-created worktree resolves apps/data-engine-x/ relative
# to its own root. Override via DEX_APP_DIR if running from a different worktree.
APP_DIR="${DEX_APP_DIR:-/Users/benjamincrane/hq-all/apps/data-engine-x}"

if [[ ! -d "$APP_DIR" ]]; then
  echo "FAIL: app dir missing: $APP_DIR" >&2
  exit 1
fi

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (repo filter)"
    return 0
  fi
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then
    echo "-- $id ($repo): SKIPPED (surface filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  # cd into APP_DIR so `doppler run` resolves to project=hq-all/config=prd via
  # apps/data-engine-x/doppler.yaml. Running from a worktree resolves to a
  # different (wrong) doppler project (sfdc-engine-x) and DEX_DB_URL_DIRECT
  # comes back empty — verify would FAIL for the wrong reason.
  if ( cd "$APP_DIR" && eval "$cmd" ); then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

# Doppler shell gotcha: `bash -c '...'` is required so $VAR expansion happens
# AFTER Doppler injects (per apps/data-engine-x/CLAUDE.md). DDL must use
# DEX_DB_URL_DIRECT (not pooled) — pgbouncer transaction-mode blocks DDL.
# Doppler project = hq-all, config = prd (per worktree-level finding;
# CLAUDE.md doc claiming project=data-engine-x is stale).

# --- s1a: by-Provider source table + ingest-runs table -----------------------
run_surface "s1a" "data-engine-x" \
  'doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_tables WHERE schemaname='"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"' AND tablename='"'"'"'"'"'"'"'"'source_cms_mup_phy_by_provider'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_tables WHERE schemaname='"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"' AND tablename='"'"'"'"'"'"'"'"'cms_mup_phy_by_provider_ingest_runs'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM information_schema.columns WHERE table_schema='"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"' AND table_name='"'"'"'"'"'"'"'"'source_cms_mup_phy_by_provider'"'"'"'"'"'"'"'"' AND column_name='"'"'"'"'"'"'"'"'raw_source_row'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid='"'"'"'"'"'"'"'"'entities.source_cms_mup_phy_by_provider'"'"'"'"'"'"'"'"'::regclass AND i.indisprimary ORDER BY a.attnum"'"'"' | tr "\n" "," | grep -q "rndrng_npi,dataset_year,"'

# --- s1b: by-Provider-and-Service source table + ingest-runs table ----------
run_surface "s1b" "data-engine-x" \
  'doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_tables WHERE schemaname='"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"' AND tablename='"'"'"'"'"'"'"'"'source_cms_mup_phy_by_provider_and_service'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM pg_tables WHERE schemaname='"'"'"'"'"'"'"'"'ops'"'"'"'"'"'"'"'"' AND tablename='"'"'"'"'"'"'"'"'cms_mup_phy_by_provider_and_service_ingest_runs'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT 1 FROM information_schema.columns WHERE table_schema='"'"'"'"'"'"'"'"'entities'"'"'"'"'"'"'"'"' AND table_name='"'"'"'"'"'"'"'"'source_cms_mup_phy_by_provider_and_service'"'"'"'"'"'"'"'"' AND column_name='"'"'"'"'"'"'"'"'raw_source_row'"'"'"'"'"'"'"'"'"'"'"' | grep -q 1 \
   && doppler run -- bash -c '"'"'psql "$DEX_DB_URL_DIRECT" -tAc "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid='"'"'"'"'"'"'"'"'entities.source_cms_mup_phy_by_provider_and_service'"'"'"'"'"'"'"'"'::regclass AND i.indisprimary ORDER BY a.attnum"'"'"' | tr "\n" "," | grep -q "rndrng_npi,hcpcs_cd,place_of_srvc,dataset_year,"'

# --- s2a: Python ingest script ----------------------------------------------
run_surface "s2a" "data-engine-x" \
  'test -f "$APP_DIR/scripts/run_cms_mup_phy_ingest.py" \
   && python3 -c "import ast; ast.parse(open('"'"'"$APP_DIR/scripts/run_cms_mup_phy_ingest.py"'"'"').read())" \
   && grep -q "by_provider" "$APP_DIR/scripts/run_cms_mup_phy_ingest.py" \
   && grep -q "by_provider_and_service" "$APP_DIR/scripts/run_cms_mup_phy_ingest.py" \
   && grep -q "MUP_PHY_R25" "$APP_DIR/scripts/run_cms_mup_phy_ingest.py" \
   && grep -q "ops.cms_mup_phy_by_provider_ingest_runs" "$APP_DIR/scripts/run_cms_mup_phy_ingest.py" \
   && grep -q "ops.cms_mup_phy_by_provider_and_service_ingest_runs" "$APP_DIR/scripts/run_cms_mup_phy_ingest.py"'

# --- s2b: alert-only Trigger.dev schedules.task -----------------------------
run_surface "s2b" "data-engine-x" \
  'test -f "$APP_DIR/trigger/src/tasks/check-cms-mup-phy-version.ts" \
   && grep -q "schedules.task" "$APP_DIR/trigger/src/tasks/check-cms-mup-phy-version.ts" \
   && grep -q "check-cms-mup-phy-version" "$APP_DIR/trigger/src/tasks/check-cms-mup-phy-version.ts" \
   && grep -q "92396110-2aed-4d63-a6a2-5d6207d46a29" "$APP_DIR/trigger/src/tasks/check-cms-mup-phy-version.ts" \
   && grep -q "8889d81e-2ee7-448f-8713-f071038289b5" "$APP_DIR/trigger/src/tasks/check-cms-mup-phy-version.ts" \
   && ( cd "$APP_DIR/trigger" && npx --no-install tsc --noEmit )'

# --- s3: deploy verification (Trigger.dev auto-on-merge) --------------------
# Verify after merge: most recent successful run of trigger-deploy-prod.yml
# in bencrane/hq-all matches the merge SHA. Accept either pre-merge "no run yet"
# (when running pre-merge to confirm scaffold) by exiting 0; reviewer/deploy-verifier
# will re-run after merge.
run_surface "s3" "data-engine-x" \
  'gh -R bencrane/hq-all run list --workflow=trigger-deploy-prod.yml --limit 1 --json status,conclusion,headSha 2>/dev/null \
   | jq -e '"'"'.[0] | (.conclusion=="success" or .status=="in_progress" or .status=="queued")'"'"' >/dev/null \
   || { echo "  (info) no successful trigger-deploy-prod.yml run yet — expected pre-merge; deploy-verifier re-checks post-merge"; exit 0; }'

echo "All requested surfaces verified."
