-- fmcsa.carrier_latest_lance — Lance-format mirror of fmcsa_carrier_latest.
--
-- Wave 3 Lance canary view. Reads from the Lance dataset registered by
-- ``apps/data-engine-x/app/services/lance_views.py`` (which materializes the
-- Lance dataset as a DuckDB-registered view named ``fmcsa_carrier_latest_lance``
-- via the Arrow bridge — see that module for the registration semantics).
--
-- Schema-shape parity with the existing fmcsa_carrier_latest:
--   - One row per USDOT (the daily Lance emit already deduplicates by writing
--     the latest snapshot only — see
--     scripts/run_fmcsa_carrier_essentials_lance_emit.py)
--   - snake_case columns
--   - Includes the original 26 essentials columns + derived columns
--     (email_domain_normalized, fleet_bucket, etc.)
--
-- Why this is a thin SELECT and not the ROW_NUMBER pattern from
-- carrier_latest.sql: the Lance dataset is overwritten daily with the latest
-- snapshot, so it ALREADY has at-most-one row per DOT. The Parquet+Iceberg
-- path retains multiple historical snapshots and needs the dedup at query
-- time; Lance handles dedup at write time.
--
-- Per-DOT random-access (the headline Lance benefit) goes through
-- ``lance.dataset(uri).scanner(filter='dot_number = ...')`` directly, NOT
-- through this view. The view is for SQL-ergonomic aggregate workloads.
--
-- Once lance-duckdb (community extension) publishes for our DuckDB/platform,
-- this view can be rewritten as a direct ``read_lance(...)`` SQL call and
-- ``app/services/lance_views.py`` becomes unnecessary.

CREATE OR REPLACE VIEW fmcsa_carrier_latest_lance AS
SELECT
  dot_number,
  legal_name,
  dba_name,
  phy_street      AS physical_street,
  phy_city        AS physical_city,
  phy_state       AS physical_state,
  phy_zip         AS physical_zip,
  carrier_mailing_street    AS mailing_street,
  carrier_mailing_city      AS mailing_city,
  carrier_mailing_state     AS mailing_state,
  carrier_mailing_zip       AS mailing_zip,
  phone,
  cell_phone,
  email_address,
  power_units_int           AS power_units,
  total_drivers_int         AS driver_total,
  carrier_operation         AS carrier_operation_code,
  status_code,
  mcs150_date_parsed        AS mcs150_date,
  add_date_parsed           AS add_date,
  snapshot                  AS snapshot_date,
  -- everything else upstream-verbatim
  * EXCLUDE (
    dot_number, legal_name, dba_name,
    phy_street, phy_city, phy_state, phy_zip,
    carrier_mailing_street, carrier_mailing_city,
    carrier_mailing_state, carrier_mailing_zip,
    phone, cell_phone, email_address,
    power_units_int, total_drivers_int,
    carrier_operation, status_code,
    mcs150_date_parsed, add_date_parsed, snapshot
  )
FROM fmcsa_carrier_essentials_lance_raw;
