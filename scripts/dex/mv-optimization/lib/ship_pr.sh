#!/usr/bin/env bash
# ship_pr.sh — branch, commit, push, open auto-merge PR.
#
# Assumes:
#   - cwd is the project repo root (worktree-aware: branch is created from current HEAD)
#   - migration file exists at the given path, ready to commit
#   - gh authenticated, repo has auto-merge enabled
#
# Usage:
#   ship_pr.sh \
#     --branch autoresearch/optimize-mv_x-2026-05-02 \
#     --migration supabase/migrations/20260502115500_optimize_mv_x_index.sql \
#     --title "perf(mv_x): add idx on col (94% faster on canonical)" \
#     --body "<full body>"
#
# Output (stdout): the PR URL on success.
# Exit codes: 0 = ok, non-zero = git/gh error.

set -euo pipefail

BRANCH=""
MIGRATION=""
TITLE=""
BODY=""

while [ $# -gt 0 ]; do
  case "$1" in
    --branch)    BRANCH="$2"; shift 2 ;;
    --migration) MIGRATION="$2"; shift 2 ;;
    --title)     TITLE="$2"; shift 2 ;;
    --body)      BODY="$2"; shift 2 ;;
    *) echo "ship_pr.sh: unknown arg: $1" >&2; exit 2 ;;
  esac
done

for required in BRANCH MIGRATION TITLE BODY; do
  if [ -z "${!required}" ]; then
    echo "ship_pr.sh: --${required,,} required" >&2
    exit 2
  fi
done

if [ ! -f "$MIGRATION" ]; then
  echo "ship_pr.sh: migration file not found: $MIGRATION" >&2
  exit 2
fi

# Sanity: gh auth
if ! gh auth status >/dev/null 2>&1; then
  echo "ship_pr.sh: gh not authenticated" >&2
  exit 2
fi

# Sanity: clean working tree (only the migration should be the diff)
DIRTY=$(git status --porcelain)
if [ -n "$DIRTY" ]; then
  EXPECTED=$(echo "$DIRTY" | awk '{print $2}' | sort -u)
  if [ "$EXPECTED" != "$MIGRATION" ]; then
    echo "ship_pr.sh: working tree has unexpected changes:" >&2
    echo "$DIRTY" >&2
    echo "ship_pr.sh: only $MIGRATION should be modified" >&2
    exit 2
  fi
fi

# Branch + commit + push
git checkout -b "$BRANCH" 2>&1 | grep -v "^Switched" >&2 || true
git add "$MIGRATION"
git commit -m "$TITLE

$BODY

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>" >&2
git push -u origin "$BRANCH" >&2

# PR with auto-merge
PR_URL=$(gh pr create \
  --title "$TITLE" \
  --body "$BODY" \
  --base main 2>&1 | grep -oE 'https://github.com[^ ]+')

if [ -z "$PR_URL" ]; then
  echo "ship_pr.sh: gh pr create did not return a URL" >&2
  exit 2
fi

# Enable auto-merge
gh pr merge "$PR_URL" --merge --auto --delete-branch >&2

echo "$PR_URL"
