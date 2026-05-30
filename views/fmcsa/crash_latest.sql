-- fmcsa.crash_latest — crash history per DOT from latest snapshot.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/crash_essentials/snapshot=*/data.parquet
--
-- Each snapshot is cumulative history as of that day; we read only the most
-- recent. Many rows per dot_number — one per crash event. Callers should
-- ORDER BY report_date DESC + LIMIT.
--
-- Parquet sibling of the existing fmcsa_crash_latest_lance view (which uses
-- the Arrow-bridge boot-time materialization pattern).

CREATE OR REPLACE VIEW fmcsa_crash_latest AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/crash_essentials/snapshot=*/data.parquet')
)
SELECT
  dot_number,
  crash_id,
  report_date,
  report_state,
  state,
  city,
  location,
  TRY_CAST(vehicles_in_accident AS BIGINT) AS vehicles_in_accident,
  TRY_CAST(fatalities AS BIGINT)           AS fatalities,
  TRY_CAST(injuries AS BIGINT)             AS injuries,
  TRY_CAST(tow_away AS BIGINT)             AS tow_away,
  federal_recordable,
  state_recordable,
  light_condition_id,
  weather_condition_id,
  road_surface_condition_id,
  vehicle_hazmat_placard,
  hazmat_released,
  add_date,
  snapshot AS snapshot_date
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/crash_essentials/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
