#!/usr/bin/env bash
set -euo pipefail

# Verifies all surfaces of directive 2026-05-04-predecessor-precheck-and-applier-hardening.
# --repo <vault|hq-all> filters to that repo's surfaces.
#
# Surfaces:
#   s1  vault   — scripts/scope-precheck-predecessor.sh exists + exits 0 on the
#                 self-fixture (this directive itself, whose predecessor's Status=complete).
#   s2  vault   — ~/.claude/skills/scope/SKILL.md mentions the precheck script in
#                 BOTH Stage 2 prompt blocks (default validator + Migration validator).
#   s3a hq-all  — apply_pending_migrations.sh parses cleanly (bash -n).
#   s3b hq-all  — apply_pending_migrations.sh contains the *_down.sql filter.
#   s3c hq-all  — apply_pending_migrations.sh contains the WARN ORPHAN log line.
#   s4  hq-all  — entities.source_cms_open_payments_general has all 9 canonical
#                 provenance columns (dex_provenance_check exit 0).

REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO_FILTER="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Verifying surfaces (filter: ${REPO_FILTER:-all})"

run_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then
    echo "-- $id ($repo): SKIPPED (filter)"
    return 0
  fi
  echo "-- $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- $id ($repo): PASS"
  else
    echo "-- $id ($repo): FAIL" >&2
    return 1
  fi
}

# Vault surfaces
run_surface "s1" "vault"  'bash /Users/benjamincrane/Desktop/hq-all/scripts/scope-precheck-predecessor.sh /Users/benjamincrane/Desktop/hq/directives/2026-05-04-predecessor-precheck-and-applier-hardening.md'
run_surface "s2" "vault"  '[[ $(grep -c "scope-precheck-predecessor.sh" /Users/benjamincrane/.claude/skills/scope/SKILL.md) -ge 2 ]]'

# hq-all surfaces
run_surface "s3a" "hq-all" 'bash -n /Users/benjamincrane/Desktop/hq-all/apps/data-engine-x/scripts/apply_pending_migrations.sh'
run_surface "s3b" "hq-all" 'grep -qE "_down\.sql" /Users/benjamincrane/Desktop/hq-all/apps/data-engine-x/scripts/apply_pending_migrations.sh'
run_surface "s3c" "hq-all" 'grep -qE "WARN ORPHAN" /Users/benjamincrane/Desktop/hq-all/apps/data-engine-x/scripts/apply_pending_migrations.sh'

# s4 — wait for post-merge applier to record the migration in ops.dex_applied_migrations
# BEFORE running dex_provenance_check. The applier runs on the operator's local clone
# via the vault post-merge hook (NOT on Railway), so Railway "SUCCESS" does not imply
# the migration has applied. Poll up to 30 min; surface `blocked-migration-not-applied`
# if the row never appears (per directive `## Deploy targets §"migration-apply path"`).
S4_MIGRATION_FILENAME="20260504030521_backfill_cms_open_payments_general_provenance.sql"
run_surface "s4"  "hq-all" 'cd /Users/benjamincrane/Desktop/hq-all/apps/data-engine-x && doppler run -- bash -c "source ./scripts/_lib/dex.sh && \
  for i in \$(seq 1 60); do \
    found=\$(dex_psql_query \"SELECT 1 FROM ops.dex_applied_migrations WHERE filename = '"'"'${S4_MIGRATION_FILENAME}'"'"' LIMIT 1\"); \
    if [[ -n \"\$found\" ]]; then echo \"applier confirmed at attempt \$i\"; break; fi; \
    if (( i == 60 )); then echo \"FAIL: migration row '"'"'${S4_MIGRATION_FILENAME}'"'"' not in ops.dex_applied_migrations after 30 min — operator has not pulled; surface blocked-migration-not-applied\" >&2; exit 1; fi; \
    sleep 30; \
  done && \
  dex_provenance_check entities.source_cms_open_payments_general"'

echo "All requested surfaces verified."
