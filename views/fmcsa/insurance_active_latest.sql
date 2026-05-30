-- fmcsa.insurance_active_latest — currently-active insurance filings per docket.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/actpendinsur_essentials/snapshot=*/data.parquet
--
-- Each daily FMCSA snapshot replaces the prior active-insurance set; we take
-- the most recent snapshot as the source of truth. Multiple rows per
-- dot_number are expected (one per insurance type / policy).
--
-- Columns aliased to the shape the LTH dashboard expects:
--   - bipd_maximum_limit is in thousands of dollars (FMCSA convention).

CREATE OR REPLACE VIEW fmcsa_insurance_active_latest AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/actpendinsur_essentials/snapshot=*/data.parquet')
)
SELECT
  dot_number,
  docket_number,
  form_code,
  insurance_type_description,
  insurance_company_name,
  policy_number,
  posted_date,
  TRY_CAST(bipd_underlying_limit AS BIGINT) AS bipd_underlying_limit,
  TRY_CAST(bipd_maximum_limit AS BIGINT)    AS bipd_maximum_limit,
  effective_date,
  cancel_effective_date,
  snapshot AS snapshot_date
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/actpendinsur_essentials/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
