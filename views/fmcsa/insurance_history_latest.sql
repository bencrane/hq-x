-- fmcsa.insurance_history_latest — cumulative insurance policy history per DOT.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/inshist_essentials/snapshot=*/data.parquet
--
-- Each daily snapshot is the cumulative history as of that day; we read the
-- most recent snapshot. Many rows per dot_number — one per historical policy.
-- Callers should ORDER BY effective_date DESC and LIMIT to surface the
-- most-recent N policies.

CREATE OR REPLACE VIEW fmcsa_insurance_history_latest AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/inshist_essentials/snapshot=*/data.parquet')
)
SELECT
  dot_number,
  docket_number,
  form_code,
  insurance_type_indicator,
  insurance_type_description,
  policy_number,
  insurance_company_name,
  insurance_company_branch,
  effective_date,
  cancel_effective_date,
  cancellation_method,
  specific_cancellation_method,
  TRY_CAST(bipd_underlying_limit_amount AS BIGINT) AS bipd_underlying_limit,
  TRY_CAST(bipd_max_coverage_amount AS BIGINT)     AS bipd_maximum_limit,
  snapshot AS snapshot_date
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/inshist_essentials/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
