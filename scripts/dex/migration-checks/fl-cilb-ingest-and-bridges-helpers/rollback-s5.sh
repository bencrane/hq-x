#!/usr/bin/env bash
# Rollback s5: migration-surface rollback is forward-only via git revert.
# Per apps/data-engine-x/supabase/migrations/README.md §"Policy" — no _down.sql.
# The INSERT...ON CONFLICT (display_name) DO UPDATE + ON CONFLICT (bridge_name)
# DO UPDATE + CREATE TABLE IF NOT EXISTS shapes make re-application idempotent
# post-revert (git revert restores the file; apply_pending_migrations.sh re-applies
# the same SQL; no harm done).
#
# Emergency in-place rollback (use ONLY when git revert isn't viable —
# e.g. someone needs to clear data-source rows for an unrelated cycle):
#   doppler run --project hq-all --config prd -- bash -c '
#     psql "$DEX_DB_URL_DIRECT" -c "
#       DELETE FROM ops.data_sources WHERE display_name IN (
#         '\''licensure.fl_cilb_lance'\'',
#         '\''bridges.sba_fl_cilb_lance'\'',
#         '\''bridges.fl_cilb_sunbiz_lance'\''
#       );
#       DELETE FROM ops.bridges WHERE bridge_name IN ('\''sba_fl_cilb'\'', '\''fl_cilb_sunbiz'\'');
#       DROP TABLE IF EXISTS ops.fl_cilb_r2_ingest_runs;
#     "
#   '
# Then ALSO remove the row from ops.dex_applied_migrations so the next
# apply_pending_migrations.sh re-applies cleanly (matches migration filename).
echo "s5 rollback (canonical): run 'git -C /Users/benjamincrane/hq-all revert <merge-SHA>' (post-merge)"
echo "                         then 'apps/data-engine-x/scripts/apply_pending_migrations.sh' re-applies idempotently if needed"
echo "                         (forward-only per apps/data-engine-x/supabase/migrations/README.md §Policy)"
echo ""
echo "s5 rollback (emergency in-place): see embedded SQL DELETE+DROP block above (operator-fired only when git revert isn't viable)"
exit 0
