-- fmcsa.authhist_latest_lance — Lance-format mirror of fmcsa authhist_essentials.
--
-- Wave 1 Lance sweep — authority-history cohort. Reads from the Lance
-- dataset registered by ``apps/data-engine-x/app/services/lance_views.py``
-- (Arrow-bridge registration named ``fmcsa_authhist_essentials_lance_raw``).
--
-- Schema-shape parity with the FMCSA authhist_essentials Parquet upstream:
--   - Multiple rows per dot_number (one per authority lifecycle event).
--   - snake_case columns matching upstream.

CREATE OR REPLACE VIEW fmcsa_authhist_latest_lance AS
SELECT
  dot_number,
  docket_number,
  sub_number,
  authority_type,
  original_action,
  original_action_served_date,
  final_action,
  final_decision_date,
  final_served_date,
  snapshot AS snapshot_date
FROM fmcsa_authhist_essentials_lance_raw;
