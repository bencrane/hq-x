-- =============================================================================
-- Directive 131 (expanded scope): prune partial / redundant FMCSA feed snapshots
--
-- Deletes every feed_date except '2026-04-25' from 13 entities.* FMCSA raw
-- tables. The 2026-04-25 feed is the latest full ~3-year archive snapshot and
-- supersedes every earlier (partial or full) feed for DMaaS targeting purposes.
--
-- STRATEGY: DELETE + VACUUM FULL (the directive's "fallback").
--
-- Why not the directive's recommended rebuild-swap?
--   The first table tried (out_of_service_orders) tripped the issue: PG tracks
--   materialized-view dependencies by table OID, not by name. ALTER TABLE x
--   RENAME TO x_old causes every MV depending on x to follow the OID into
--   x_old. After the second rename (x_new -> x), the MVs are still pointed at
--   x_old, and DROP TABLE x_old fails with "objects depend on it" (and CASCADE
--   would silently drop the MVs themselves -- forbidden by the directive).
--
--   DELETE + VACUUM FULL preserves the table OID, so all MV dependencies stay
--   intact. The trade-off is per-table runtime: VACUUM FULL takes an
--   AccessExclusive lock for the duration and writes a full new heap. For
--   reads of the MVs' stored data this is fine (they don't read the base
--   table); only base-table reads and MV REFRESHes are blocked while VACUUM
--   FULL runs. Sequencing one table at a time, off-hours, is acceptable.
--
-- Per table:
--   1. Inventory SELECT (feed_date, COUNT(*)).
--   2. Sanity gate (raises if any of three conditions fails):
--        a. feed_date '2026-04-25' exists with >0 rows
--        b. feed_date '2026-04-25' row count is within 90% of the max
--           feed_date row count
--        c. no feed_date is later than '2026-04-25'
--   3. BEGIN; DELETE WHERE feed_date <> '2026-04-25'; verify; COMMIT.
--      (Sanity + verify run inside the DELETE transaction; ROLLBACK on any
--      failure leaves the table unchanged.)
--   4. VACUUM FULL entities.<t>; -- reclaims disk; runs outside transaction.
--   5. Report post-prune size.
--
-- Tables are processed smallest -> largest so any procedural mistake costs
-- the cheapest table.
--
-- Pre-flight verified (all 13 tables):
--   * 0 FKs (inbound or outbound).
--   * 1 trigger per table (BEFORE UPDATE update_<t>_updated_at calling
--     update_updated_at_column()) -- DELETE doesn't fire BEFORE UPDATE
--     triggers; trigger preserved through the operation.
--   * relrowsecurity=true on every table -- DELETE/VACUUM preserve.
--   * GRANTs preserved (we're not dropping/recreating the table).
--   * Materialized-view dependencies preserved (same OID throughout).
--
-- Run via:
--   doppler run -- bash -c \
--     'psql "$DEX_DB_URL_DIRECT" -v ON_ERROR_STOP=1 -f scripts/prune_partial_fmcsa_feeds.sql'
--
-- =============================================================================

\set ON_ERROR_STOP on
\timing on
\pset pager off

SET statement_timeout = '180min';
SET work_mem = '1GB';
SET maintenance_work_mem = '2GB';

\echo
\echo '###############################################################################'
\echo '# FMCSA feed prune starting (DELETE + VACUUM FULL strategy)'
\echo '###############################################################################'

SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size_before;

-- =============================================================================
-- Table 1/13: out_of_service_orders (~2.4 GB)
-- =============================================================================
\echo
\echo '============ 1/13 out_of_service_orders ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.out_of_service_orders
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.out_of_service_orders WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.out_of_service_orders;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.out_of_service_orders GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity out_of_service_orders: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity out_of_service_orders: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity out_of_service_orders: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK out_of_service_orders: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.out_of_service_orders WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.out_of_service_orders;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify out_of_service_orders: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK out_of_service_orders: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.out_of_service_orders;
SELECT pg_size_pretty(pg_total_relation_size('entities.out_of_service_orders'::regclass)) AS post_size_out_of_service_orders;

-- =============================================================================
-- Table 2/13: insurance_policy_filings (~7.4 GB)
-- =============================================================================
\echo
\echo '============ 2/13 insurance_policy_filings ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.insurance_policy_filings
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.insurance_policy_filings WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.insurance_policy_filings;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.insurance_policy_filings GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity insurance_policy_filings: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity insurance_policy_filings: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity insurance_policy_filings: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK insurance_policy_filings: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.insurance_policy_filings WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.insurance_policy_filings;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify insurance_policy_filings: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK insurance_policy_filings: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.insurance_policy_filings;
SELECT pg_size_pretty(pg_total_relation_size('entities.insurance_policy_filings'::regclass)) AS post_size_insurance_policy_filings;

-- =============================================================================
-- Table 3/13: operating_authority_revocations (~9.4 GB)
-- =============================================================================
\echo
\echo '============ 3/13 operating_authority_revocations ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.operating_authority_revocations
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.operating_authority_revocations WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.operating_authority_revocations;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.operating_authority_revocations GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity operating_authority_revocations: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity operating_authority_revocations: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity operating_authority_revocations: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK operating_authority_revocations: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.operating_authority_revocations WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.operating_authority_revocations;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify operating_authority_revocations: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK operating_authority_revocations: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.operating_authority_revocations;
SELECT pg_size_pretty(pg_total_relation_size('entities.operating_authority_revocations'::regclass)) AS post_size_operating_authority_revocations;

-- =============================================================================
-- Table 4/13: carrier_registrations (~11 GB)
-- =============================================================================
\echo
\echo '============ 4/13 carrier_registrations ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.carrier_registrations
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.carrier_registrations WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.carrier_registrations;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.carrier_registrations GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity carrier_registrations: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity carrier_registrations: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity carrier_registrations: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK carrier_registrations: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.carrier_registrations WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.carrier_registrations;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify carrier_registrations: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK carrier_registrations: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.carrier_registrations;
SELECT pg_size_pretty(pg_total_relation_size('entities.carrier_registrations'::regclass)) AS post_size_carrier_registrations;

-- =============================================================================
-- Table 5/13: carrier_safety_basic_measures (~14 GB)
-- =============================================================================
\echo
\echo '============ 5/13 carrier_safety_basic_measures ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.carrier_safety_basic_measures
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.carrier_safety_basic_measures WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.carrier_safety_basic_measures;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.carrier_safety_basic_measures GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity carrier_safety_basic_measures: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity carrier_safety_basic_measures: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity carrier_safety_basic_measures: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK carrier_safety_basic_measures: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.carrier_safety_basic_measures WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.carrier_safety_basic_measures;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify carrier_safety_basic_measures: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK carrier_safety_basic_measures: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.carrier_safety_basic_measures;
SELECT pg_size_pretty(pg_total_relation_size('entities.carrier_safety_basic_measures'::regclass)) AS post_size_carrier_safety_basic_measures;

-- =============================================================================
-- Table 6/13: process_agent_filings (~18 GB)
-- =============================================================================
\echo
\echo '============ 6/13 process_agent_filings ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.process_agent_filings
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.process_agent_filings WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.process_agent_filings;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.process_agent_filings GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity process_agent_filings: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity process_agent_filings: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity process_agent_filings: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK process_agent_filings: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.process_agent_filings WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.process_agent_filings;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify process_agent_filings: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK process_agent_filings: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.process_agent_filings;
SELECT pg_size_pretty(pg_total_relation_size('entities.process_agent_filings'::regclass)) AS post_size_process_agent_filings;

-- =============================================================================
-- Table 7/13: commercial_vehicle_crashes (~31 GB)
-- =============================================================================
\echo
\echo '============ 7/13 commercial_vehicle_crashes ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.commercial_vehicle_crashes
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.commercial_vehicle_crashes WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.commercial_vehicle_crashes;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.commercial_vehicle_crashes GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity commercial_vehicle_crashes: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity commercial_vehicle_crashes: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity commercial_vehicle_crashes: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK commercial_vehicle_crashes: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.commercial_vehicle_crashes WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.commercial_vehicle_crashes;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify commercial_vehicle_crashes: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK commercial_vehicle_crashes: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.commercial_vehicle_crashes;
SELECT pg_size_pretty(pg_total_relation_size('entities.commercial_vehicle_crashes'::regclass)) AS post_size_commercial_vehicle_crashes;

-- =============================================================================
-- Table 8/13: insurance_policy_history_events (~33 GB)
-- =============================================================================
\echo
\echo '============ 8/13 insurance_policy_history_events ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.insurance_policy_history_events
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.insurance_policy_history_events WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.insurance_policy_history_events;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.insurance_policy_history_events GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity insurance_policy_history_events: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity insurance_policy_history_events: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity insurance_policy_history_events: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK insurance_policy_history_events: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.insurance_policy_history_events WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.insurance_policy_history_events;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify insurance_policy_history_events: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK insurance_policy_history_events: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.insurance_policy_history_events;
SELECT pg_size_pretty(pg_total_relation_size('entities.insurance_policy_history_events'::regclass)) AS post_size_insurance_policy_history_events;

-- =============================================================================
-- Table 9/13: vehicle_inspection_units (~38 GB)
-- =============================================================================
\echo
\echo '============ 9/13 vehicle_inspection_units ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.vehicle_inspection_units
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.vehicle_inspection_units WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.vehicle_inspection_units;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.vehicle_inspection_units GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity vehicle_inspection_units: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity vehicle_inspection_units: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity vehicle_inspection_units: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK vehicle_inspection_units: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.vehicle_inspection_units WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.vehicle_inspection_units;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify vehicle_inspection_units: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK vehicle_inspection_units: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.vehicle_inspection_units;
SELECT pg_size_pretty(pg_total_relation_size('entities.vehicle_inspection_units'::regclass)) AS post_size_vehicle_inspection_units;

-- =============================================================================
-- Table 10/13: motor_carrier_census_records (~41 GB)
-- =============================================================================
\echo
\echo '============ 10/13 motor_carrier_census_records ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.motor_carrier_census_records
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.motor_carrier_census_records WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.motor_carrier_census_records;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.motor_carrier_census_records GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity motor_carrier_census_records: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity motor_carrier_census_records: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity motor_carrier_census_records: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK motor_carrier_census_records: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.motor_carrier_census_records WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.motor_carrier_census_records;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify motor_carrier_census_records: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK motor_carrier_census_records: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.motor_carrier_census_records;
SELECT pg_size_pretty(pg_total_relation_size('entities.motor_carrier_census_records'::regclass)) AS post_size_motor_carrier_census_records;

-- =============================================================================
-- Table 11/13: carrier_inspection_violations (~56 GB)
-- =============================================================================
\echo
\echo '============ 11/13 carrier_inspection_violations ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.carrier_inspection_violations
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.carrier_inspection_violations WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.carrier_inspection_violations;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.carrier_inspection_violations GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity carrier_inspection_violations: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity carrier_inspection_violations: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity carrier_inspection_violations: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK carrier_inspection_violations: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.carrier_inspection_violations WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.carrier_inspection_violations;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify carrier_inspection_violations: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK carrier_inspection_violations: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.carrier_inspection_violations;
SELECT pg_size_pretty(pg_total_relation_size('entities.carrier_inspection_violations'::regclass)) AS post_size_carrier_inspection_violations;

-- =============================================================================
-- Table 12/13: operating_authority_histories (~56 GB)
-- =============================================================================
\echo
\echo '============ 12/13 operating_authority_histories ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.operating_authority_histories
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.operating_authority_histories WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.operating_authority_histories;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.operating_authority_histories GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity operating_authority_histories: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity operating_authority_histories: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity operating_authority_histories: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK operating_authority_histories: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.operating_authority_histories WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.operating_authority_histories;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify operating_authority_histories: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK operating_authority_histories: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.operating_authority_histories;
SELECT pg_size_pretty(pg_total_relation_size('entities.operating_authority_histories'::regclass)) AS post_size_operating_authority_histories;

-- =============================================================================
-- Table 13/13: carrier_inspections (~66 GB)
-- =============================================================================
\echo
\echo '============ 13/13 carrier_inspections ============'

SELECT feed_date, COUNT(*) AS rows
FROM entities.carrier_inspections
GROUP BY feed_date
ORDER BY feed_date;

DO $$
DECLARE
  v_keep BIGINT;
  v_max  BIGINT;
  v_late DATE;
BEGIN
  SELECT COUNT(*)         INTO v_keep FROM entities.carrier_inspections WHERE feed_date = DATE '2026-04-25';
  SELECT MAX(feed_date)   INTO v_late FROM entities.carrier_inspections;
  SELECT MAX(rc)          INTO v_max
    FROM (SELECT COUNT(*) AS rc FROM entities.carrier_inspections GROUP BY feed_date) s;
  IF v_keep IS NULL OR v_keep = 0 THEN
    RAISE EXCEPTION 'sanity carrier_inspections: 2026-04-25 absent';
  END IF;
  IF v_late > DATE '2026-04-25' THEN
    RAISE EXCEPTION 'sanity carrier_inspections: latest feed_date % > 2026-04-25', v_late;
  END IF;
  IF v_keep::numeric < v_max::numeric * 0.9 THEN
    RAISE EXCEPTION 'sanity carrier_inspections: 04-25 rows % < 90%% of max %', v_keep, v_max;
  END IF;
  RAISE NOTICE 'sanity OK carrier_inspections: keep=% max=% latest=%', v_keep, v_max, v_late;
END $$;

BEGIN;
  DELETE FROM entities.carrier_inspections WHERE feed_date <> DATE '2026-04-25';
  DO $$
  DECLARE v_kept BIGINT;
  BEGIN
    SELECT COUNT(*) INTO v_kept FROM entities.carrier_inspections;
    IF v_kept = 0 THEN
      RAISE EXCEPTION 'verify carrier_inspections: 0 rows after delete';
    END IF;
    RAISE NOTICE 'delete OK carrier_inspections: kept=%', v_kept;
  END $$;
COMMIT;

VACUUM (FULL, VERBOSE) entities.carrier_inspections;
SELECT pg_size_pretty(pg_total_relation_size('entities.carrier_inspections'::regclass)) AS post_size_carrier_inspections;

\echo
\echo '###############################################################################'
\echo '# Prune complete. Final DB size:'
\echo '###############################################################################'

SELECT pg_size_pretty(pg_database_size(current_database())) AS db_size_after;
