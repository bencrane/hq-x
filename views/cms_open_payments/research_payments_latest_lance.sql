-- cms_open_payments.research_payments_latest_lance — Lance-format mirror of
-- CMS Open Payments Research feed (normalized 15-column schema, 2024 onward).
--
-- Wave 2 Lance sweep — industry payments tied to clinical research.
-- Same schema shape as the General feed; far smaller (~756K rows).
--
-- This view IS registered at boot (the dataset is small enough). Per-
-- record_id random-access lookups should still call
-- ``lance.dataset(uri).scanner(filter='record_id = ?')`` directly via
-- Lance for sub-100ms latency; the DuckDB view exists for SQL-ergonomic
-- aggregate workloads.

CREATE OR REPLACE VIEW cms_open_payments_research_payments_latest_lance AS
SELECT
  record_id,
  program_year,
  total_amount_of_payment_usdollars,
  date_of_payment,
  applicable_manufacturer_or_applicable_gpo_making_payment_name,
  applicable_manufacturer_or_applicable_gpo_making_payment_id,
  name_of_drug_or_biological_or_device_or_medical_supply_1,
  covered_recipient_npi,
  covered_recipient_type,
  nature_of_payment_or_transfer_of_value,
  dispute_status_for_publication,
  manufacturer_name_normalized,
  feed,
  year
FROM cms_open_payments_research_lance_raw;
