-- DEPRECATED 2026-05-13: zero readers across apps/data-engine-x/{app/services,app/routers,risingwave,trigger}/.
-- Superseded by Lance dataset s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/safety_basics_lance/
-- which expanded coverage from ~9K (sms_ab_pass) to ~706K (sms_ab_pass_property LEFT JOIN sms_ab_pass).
-- See directive ~/Desktop/hq/directives/2026-05-13-fmcsa-safety-basics-property-expansion.md.
-- Retain file for git history only; do NOT apply this view to RisingWave going forward.
-- Per CLAUDE.md §"USAspending pipeline" "RW retired (2026-05-13)" — RisingWave is being retired
-- platform-wide; deprecate-in-place is the consistent pattern.
--
-- (Original view body preserved below for reference; do not run.)
--
-- =====================================================================
-- fmcsa.safety_basics_latest — CSA Safety Measurement System percentiles per DOT.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/sms_ab_pass/snapshot=*/data.parquet
--
-- FMCSA publishes SMS data MONTHLY (not daily); expect ~1 snapshot per
-- calendar month. We take the most recent snapshot. One row per DOT.
--
-- Column names: the FMCSA SMS feed is uppercase. This view renames to
-- snake_case + the canonical column names the LTH dashboard consumes
-- (`<basic>_percentile` + `<basic>_alert`).

CREATE OR REPLACE VIEW fmcsa_safety_basics_latest AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/sms_ab_pass/snapshot=*/data.parquet')
)
SELECT
  "DOT_NUMBER"                                       AS dot_number,
  TRY_CAST("INSP_TOTAL"             AS BIGINT)       AS inspection_total,
  TRY_CAST("DRIVER_INSP_TOTAL"      AS BIGINT)       AS driver_inspection_total,
  TRY_CAST("DRIVER_OOS_INSP_TOTAL"  AS BIGINT)       AS driver_oos_inspection_total,
  TRY_CAST("VEHICLE_INSP_TOTAL"     AS BIGINT)       AS vehicle_inspection_total,
  TRY_CAST("VEHICLE_OOS_INSP_TOTAL" AS BIGINT)       AS vehicle_oos_inspection_total,
  TRY_CAST("UNSAFE_DRIV_PCT"        AS DOUBLE)       AS unsafe_driving_percentile,
  TRY_CAST("HOS_DRIV_PCT"           AS DOUBLE)       AS hos_percentile,
  TRY_CAST("DRIV_FIT_PCT"           AS DOUBLE)       AS driver_fitness_percentile,
  TRY_CAST("CONTR_SUBST_PCT"        AS DOUBLE)       AS controlled_substances_percentile,
  TRY_CAST("VEH_MAINT_PCT"          AS DOUBLE)       AS vehicle_maintenance_percentile,
  "UNSAFE_DRIV_BASIC_ALERT"                          AS unsafe_driving_alert,
  "HOS_DRIV_BASIC_ALERT"                             AS hos_alert,
  "DRIV_FIT_BASIC_ALERT"                             AS driver_fitness_alert,
  "CONTR_SUBST_BASIC_ALERT"                          AS controlled_substances_alert,
  "VEH_MAINT_BASIC_ALERT"                            AS vehicle_maintenance_alert,
  snapshot                                           AS snapshot_date
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/sms_ab_pass/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
