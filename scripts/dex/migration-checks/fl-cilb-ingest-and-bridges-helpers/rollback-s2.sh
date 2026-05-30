#!/usr/bin/env bash
# Rollback s2: code-surface rollback is operator-driven git revert.
# Pre-merge: no rollback needed (just don't merge the PR).
# Post-merge: operator runs git revert. R2 Lance dataset teardown is handled
# by s6 rollback (Polaris DELETE + R2 prefix delete polaris-warehouse/licensure/fl_cilb_lance/).
echo "s2 rollback: run 'git -C /Users/benjamincrane/hq-all revert <merge-SHA>' (post-merge)"
echo "             pre-merge rollback is implicit (close PR without merge)"
echo "             Lance dataset teardown for polaris-warehouse/licensure/fl_cilb_lance/ is handled by s6 rollback"
exit 0
