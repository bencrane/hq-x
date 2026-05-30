#!/usr/bin/env bash
# Rollback s4: code-surface rollback is operator-driven git revert.
# Pre-merge: no rollback needed (just don't merge the PR).
# Post-merge: operator runs git revert. Bridge Lance dataset teardown is handled
# by s6 rollback (Polaris DELETE + R2 prefix delete polaris-warehouse/bridges/fl_cilb_sunbiz_lance/).
echo "s4 rollback: run 'git -C /Users/benjamincrane/hq-all revert <merge-SHA>' (post-merge)"
echo "             pre-merge rollback is implicit (close PR without merge)"
echo "             Bridge Lance teardown for polaris-warehouse/bridges/fl_cilb_sunbiz_lance/ is handled by s6 rollback"
exit 0
