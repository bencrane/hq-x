#!/usr/bin/env bash
set -euo pipefail

# Reverse-order rollback for directive 2026-05-04-predecessor-precheck-and-applier-hardening.
# --surface <id> filters to one surface; --repo <vault|hq-all> filters to one repo.
#
# Uses env vars for merge SHAs:
#   S1_MERGE_SHA=<sha>   (vault bencrane/hq commit SHA — vault uses
#                         direct-commit-to-main; the SHA is the s1 commit itself)
#   S3S4_MERGE_SHA=<sha> (hq-all bencrane/hq-all PR merge SHA — same PR carries
#                         both s3 and s4, so the same revert reverts both)
# Uses env var for the SKILL.md backup path:
#   S2_BACKUP=<path>     (e.g. /Users/.../SKILL.md.pre-s2-20260504030521)
#
# Note: rollback for s1 and s3+s4 ECHOES the `git revert` command rather than
# running it directly. `git revert` requires editor interaction by default and
# can fail mid-cycle; the operator runs the command (or the deploy-verifier
# opens a hotfix-revert PR per Stage 3.5). s2 restores from backup directly
# (idempotent, no editor needed).

SURFACE_FILTER=""
REPO_FILTER=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --surface) SURFACE_FILTER="$2"; shift 2 ;;
    --repo)    REPO_FILTER="$2";    shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

echo "==> Rolling back surfaces (surface: ${SURFACE_FILTER:-all}, repo: ${REPO_FILTER:-all})"

rollback_surface() {
  local id="$1" repo="$2" cmd="$3"
  if [[ -n "$SURFACE_FILTER" && "$SURFACE_FILTER" != "$id" ]]; then return 0; fi
  if [[ -n "$REPO_FILTER" && "$REPO_FILTER" != "$repo" ]]; then return 0; fi
  echo "-- rollback $id ($repo): RUNNING"
  if eval "$cmd"; then
    echo "-- rollback $id ($repo): OK"
  else
    echo "-- rollback $id ($repo): FAILED" >&2
    return 1
  fi
}

# REVERSE order: s4 → s3 → s2 → s1
rollback_surface "s4"  "hq-all" 'echo "Run: git -C /Users/benjamincrane/Desktop/hq-all revert ${S3S4_MERGE_SHA:?S3S4_MERGE_SHA env var required} (reverts s3+s4 together)"'
rollback_surface "s3"  "hq-all" 'echo "Run: git -C /Users/benjamincrane/Desktop/hq-all revert ${S3S4_MERGE_SHA:?S3S4_MERGE_SHA env var required} (already reverted with s4 — same PR; this echo is a NO-OP confirmation)"'
rollback_surface "s2"  "vault"  'cp "${S2_BACKUP:?S2_BACKUP env var required (path to SKILL.md.pre-s2-* backup)}" /Users/benjamincrane/.claude/skills/scope/SKILL.md && echo "SKILL.md restored from backup"'
rollback_surface "s1"  "vault"  'echo "Run: git -C /Users/benjamincrane/Desktop/hq revert ${S1_MERGE_SHA:?S1_MERGE_SHA env var required}"'

echo "Rollback complete (commands echoed for operator to run if env vars set; some surfaces require explicit operator action)."
