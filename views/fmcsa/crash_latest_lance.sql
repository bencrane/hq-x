-- fmcsa.crash_latest_lance — Lance-format mirror of fmcsa crash_essentials.
--
-- Wave 1 Lance sweep — sibling to carrier_latest_lance. Reads from the Lance
-- dataset registered by ``apps/data-engine-x/app/services/lance_views.py``
-- (Arrow-bridge registration named ``fmcsa_crash_essentials_lance_raw``).
--
-- Schema-shape parity with the FMCSA crash_essentials Parquet upstream:
--   - Multiple rows per dot_number (one per crash event).
--   - snake_case columns matching upstream.
--
-- Per-DOT random-access goes through ``lance.dataset(uri).scanner(filter=...)``
-- directly, not through this view.

CREATE OR REPLACE VIEW fmcsa_crash_latest_lance AS
SELECT
  dot_number,
  crash_id,
  report_date,
  report_state,
  state,
  city,
  location,
  vehicles_in_accident,
  fatalities,
  injuries,
  tow_away,
  federal_recordable,
  state_recordable,
  light_condition_id,
  weather_condition_id,
  road_surface_condition_id,
  vehicle_hazmat_placard,
  hazmat_released,
  add_date,
  snapshot AS snapshot_date
FROM fmcsa_crash_essentials_lance_raw;
