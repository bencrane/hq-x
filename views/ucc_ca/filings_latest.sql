-- ucc_ca_filings_latest — latest filing per (debtor identity key) across CA UCC streams.
--
-- Foundational view for the USAspending awardees ↔ equipment finance lenders
-- GTM motion. A debtor with multiple historical filings (UCC1 → UCC3 → UCC5
-- amendment chain) collapses to one row carrying the most recent filing's
-- snapshot of who lent to whom.
--
-- Source: R2 parquet under
--   s3://dex-raw-landing-zone/ucc/state=CA/stream=*/snapshot=*/data.parquet.zst
--
-- Pattern parallel: views/fmcsa/carrier_latest.sql. Same DISTINCT-via-
-- ROW_NUMBER pattern, same `read_parquet(filename=true)` snapshot extraction,
-- same SELECT * passthrough for non-identity columns so audience specs can
-- still filter on raw upstream columns when needed.
--
-- "Latest" semantics:
--   PARTITION BY debtor_name_normalized
--   ORDER BY filing_date DESC NULLS LAST, ucc_snapshot_date DESC
--   keep _rn = 1
--
-- Why partition by debtor_name_normalized (not file_number):
--   The matching engine joins debtors across SOURCES (UCC ↔ HMDA borrower ↔
--   USAspending awardee). The natural join key is the normalized identity
--   spine, not the filing's intra-source ID. file_number is preserved as a
--   passthrough column so callers who care about the specific filing can
--   pivot on it.
--
-- snapshot_date extraction:
--   The ingest script (run_ucc_ca_ingest.py) stamps a synthetic
--   `ucc_snapshot_date DATE` column directly into the parquet payload, so
--   we don't need the regex-from-filename trick the FMCSA view uses.
--   filename=true is enabled anyway to expose `_filename` for debugging /
--   downstream lineage tracking.
--
-- Column rename: the high-traffic identity + targeting columns stay
-- snake-cased per the ingest script's naming. Everything else passes through
-- via SELECT *.

CREATE OR REPLACE VIEW ucc_ca_filings_latest AS
WITH ranked AS (
  SELECT
    -- Identity-spine columns (snake-case, normalized).
    debtor_name_normalized,
    debtor_zip5,
    debtor_state_normalized,
    secured_party_name_normalized,
    secured_party_zip5,
    secured_party_state_normalized,
    -- Filing identity + date.
    file_number,
    filing_date,
    -- Partition metadata stamped by the ingest script.
    ucc_state,
    ucc_stream,
    ucc_snapshot_date,
    source_run_id,
    source_provider,
    -- ROW_NUMBER over the canonical identity key.
    ROW_NUMBER() OVER (
      PARTITION BY debtor_name_normalized
      ORDER BY
        filing_date DESC NULLS LAST,
        ucc_snapshot_date DESC
    ) AS _rn,
    -- Path-level provenance for debugging.
    filename AS _filename,
    -- Everything else upstream-verbatim.
    * EXCLUDE (
      debtor_name_normalized,
      debtor_zip5,
      debtor_state_normalized,
      secured_party_name_normalized,
      secured_party_zip5,
      secured_party_state_normalized,
      file_number,
      filing_date,
      ucc_state,
      ucc_stream,
      ucc_snapshot_date,
      source_run_id,
      source_provider
    )
  FROM read_parquet(
    's3://dex-raw-landing-zone/ucc/state=CA/stream=*/snapshot=*/data.parquet.zst',
    filename = true,
    union_by_name = true
  )
  WHERE debtor_name_normalized IS NOT NULL
)
SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1;
