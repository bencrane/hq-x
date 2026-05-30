-- fmcsa.authhist_latest — authority history per DOT from latest snapshot.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/authhist_essentials/snapshot=*/data.parquet
--
-- Each snapshot is cumulative history as of that day; we read only the most
-- recent. Many rows per dot_number — one per authority lifecycle event.
-- Callers should ORDER BY original_action_served_date DESC + LIMIT.
--
-- Parquet sibling of the existing fmcsa_authhist_latest_lance view. The Lance
-- variant materializes ~4.4M rows in process memory at boot via the Arrow
-- bridge; this parquet view streams rows on demand with predicate pushdown.

CREATE OR REPLACE VIEW fmcsa_authhist_latest AS
WITH latest_snap AS (
  SELECT MAX(snapshot) AS max_snap
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/authhist_essentials/snapshot=*/data.parquet')
)
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
FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/authhist_essentials/snapshot=*/data.parquet')
WHERE snapshot = (SELECT max_snap FROM latest_snap);
