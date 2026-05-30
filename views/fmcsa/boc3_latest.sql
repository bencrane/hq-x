-- fmcsa_boc3_latest — latest BOC-3 process agent per docket_number.
--
-- Source: R2 parquet under s3://dex-raw-landing-zone/fmcsa-derived/boc3_awh/snapshot=*/data.parquet
--
-- Upstream schema (10 columns, space-separated names):
--   ["Docket Number", "USDOT Number", "Company Name", "Attention to or Title",
--    "Street or PO Box", "City", "State", "Country", "Zip Code", "snapshot"]
--
-- Latest semantics: ROW_NUMBER OVER (PARTITION BY "Docket Number" ORDER BY snapshot DESC)
-- keep rn=1. Equivalent to DISTINCT ON in Postgres.
--
-- D1 closure: upstream column names are quoted verbatim; output aliases are snake_case.
-- D2 closure: process_agent columns only — no date-of-filing field exists in substrate.
--
-- Pattern parity: views/fmcsa/insurance_active_latest.sql (parquet-direct, snapshot-latest).

CREATE OR REPLACE VIEW fmcsa_boc3_latest AS
WITH ranked AS (
  SELECT
    "Docket Number"            AS docket_number,
    "USDOT Number"             AS usdot_number,
    "Company Name"             AS process_agent_company_name,
    "Attention to or Title"    AS process_agent_name,
    "Street or PO Box"         AS process_agent_street,
    "City"                     AS process_agent_city,
    "State"                    AS process_agent_state,
    "Zip Code"                 AS process_agent_zip,
    "Country"                  AS process_agent_country,
    snapshot                   AS snapshot_date,
    ROW_NUMBER() OVER (
      PARTITION BY "Docket Number"
      ORDER BY snapshot DESC
    ) AS _rn
  FROM read_parquet('s3://dex-raw-landing-zone/fmcsa-derived/boc3_awh/snapshot=*/data.parquet')
)
SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1;
