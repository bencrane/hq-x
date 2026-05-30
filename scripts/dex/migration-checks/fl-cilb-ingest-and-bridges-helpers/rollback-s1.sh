#!/usr/bin/env bash
# Rollback s1: code-surface rollback is operator-driven git revert.
# Pre-merge: no rollback needed (just don't merge the PR).
# Post-merge: operator runs git revert. If the daily Modal app was deployed,
# also run `modal app stop data-engine-x-fl-cilb-daily` (handled in s6 rollback).
echo "s1 rollback: run 'git -C /Users/benjamincrane/hq-all revert <merge-SHA>' (post-merge)"
echo "             pre-merge rollback is implicit (close PR without merge)"
echo "             if Modal app deployed: 'modal app stop data-engine-x-fl-cilb-daily' (also handled by s6 rollback)"
exit 0
