-- fmcsa.inspections_recent — per-inspection records from the latest snapshot.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/vehicle_inspection_essentials/snapshot=*/data.parquet
--
-- The vehicle_inspection_essentials feed is cumulative — each daily snapshot
-- contains the rolling FMCSA inspection set. We read only the most recent
-- snapshot. Many rows per dot_number (one per inspection event). Callers
-- should ORDER BY insp_date DESC and LIMIT N for "recent N inspections".

CREATE OR REPLACE VIEW fmcsa_inspections_recent AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/vehicle_inspection_essentials/snapshot=*/data.parquet')
)
SELECT
  dot_number,
  inspection_id,
  insp_date,
  report_state,
  insp_level_id                                        AS inspection_level,
  location,
  location_desc,
  TRY_CAST(viol_total          AS BIGINT)              AS violation_total,
  TRY_CAST(oos_total           AS BIGINT)              AS oos_total,
  TRY_CAST(driver_viol_total   AS BIGINT)              AS driver_violation_total,
  TRY_CAST(driver_oos_total    AS BIGINT)              AS driver_oos_total,
  TRY_CAST(vehicle_viol_total  AS BIGINT)              AS vehicle_violation_total,
  TRY_CAST(vehicle_oos_total   AS BIGINT)              AS vehicle_oos_total,
  TRY_CAST(hazmat_viol_total   AS BIGINT)              AS hazmat_violation_total,
  TRY_CAST(hazmat_oos_total    AS BIGINT)              AS hazmat_oos_total,
  hazmat_placard_req,
  post_acc_ind,
  mcmis_add_date,
  snapshot                                             AS snapshot_date
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/vehicle_inspection_essentials/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
