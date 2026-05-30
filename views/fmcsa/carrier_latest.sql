-- fmcsa.carrier_latest — latest census snapshot per USDOT, snake_case columns.
--
-- Foundational view for 5 of the 6 FMCSA GTM personas. Conceptually replaces
-- the dropped entities.mv_fmcsa_latest_census matview but lives in DuckDB
-- (registered at typed_audiences process startup) rather than Postgres.
--
-- Source: R2 parquet under fmcsa/Company Census File/snapshot=*/data.parquet.zst
--
-- Why direct read_parquet vs the Iceberg bridge:
--   The FMCSA R2 writer did NOT stamp a synthetic snapshot_date column into
--   the parquet payload. To recover snapshot identity we use DuckDB's
--   read_parquet(..., filename=true) and regex-extract from the path. The
--   Iceberg-via-PyIceberg bridge does not surface filename. Future cleanup:
--   re-register the FMCSA Iceberg tables with snapshot_date as a partition
--   column derived at add_files time; then this view can use the bridge.
--
-- Latest semantics: ROW_NUMBER OVER (PARTITION BY DOT_NUMBER ORDER BY
-- snapshot_date DESC) and keep rn=1. Equivalent to DISTINCT ON in Postgres.
--
-- Column rename: only the high-traffic identity + targeting columns are
-- snake_cased (matches the names the dropped mv_fmcsa_latest_census exposed).
-- The rest stay upstream-verbatim as a SELECT * passthrough — audience specs
-- can quote them if they need exotic columns. This keeps the view ergonomic
-- for common cases without forcing a 148-column manual rename.

CREATE OR REPLACE VIEW fmcsa_carrier_latest AS
WITH ranked AS (
  SELECT
    "DOT_NUMBER"                                         AS dot_number,
    "LEGAL_NAME"                                         AS legal_name,
    "DBA_NAME"                                           AS dba_name,
    "PHY_STREET"                                         AS physical_street,
    "PHY_CITY"                                           AS physical_city,
    "PHY_STATE"                                          AS physical_state,
    "PHY_ZIP"                                            AS physical_zip,
    "CARRIER_MAILING_STREET"                             AS mailing_street,
    "CARRIER_MAILING_CITY"                               AS mailing_city,
    "CARRIER_MAILING_STATE"                              AS mailing_state,
    "CARRIER_MAILING_ZIP"                                AS mailing_zip,
    "PHONE"                                              AS phone,
    "CELL_PHONE"                                         AS cell_phone,
    "EMAIL_ADDRESS"                                      AS email_address,
    TRY_CAST("POWER_UNITS"   AS INTEGER)                 AS power_units,
    TRY_CAST("TOTAL_DRIVERS" AS INTEGER)                 AS driver_total,
    "CARRIER_OPERATION"                                  AS carrier_operation_code,
    "STATUS_CODE"                                        AS status_code,
    "MCS150_DATE"                                        AS mcs150_date,
    "ADD_DATE"                                           AS add_date,
    -- snapshot_date extracted from the R2 file path
    regexp_extract(filename, 'snapshot=(\d{4}-\d{2}-\d{2})/', 1) AS snapshot_date,
    ROW_NUMBER() OVER (
      PARTITION BY "DOT_NUMBER"
      ORDER BY regexp_extract(filename, 'snapshot=(\d{4}-\d{2}-\d{2})/', 1) DESC
    ) AS _rn,
    -- everything else upstream-verbatim
    * EXCLUDE (
      "DOT_NUMBER", "LEGAL_NAME", "DBA_NAME",
      "PHY_STREET", "PHY_CITY", "PHY_STATE", "PHY_ZIP",
      "CARRIER_MAILING_STREET", "CARRIER_MAILING_CITY",
      "CARRIER_MAILING_STATE", "CARRIER_MAILING_ZIP",
      "PHONE", "CELL_PHONE", "EMAIL_ADDRESS",
      "POWER_UNITS", "TOTAL_DRIVERS",
      "CARRIER_OPERATION", "STATUS_CODE",
      "MCS150_DATE", "ADD_DATE"
    )
  FROM read_parquet(
    's3://dex-raw-landing-zone/fmcsa/Company Census File/snapshot=*/data.parquet.zst',
    filename = true
  )
)
SELECT * EXCLUDE (_rn) FROM ranked WHERE _rn = 1;
