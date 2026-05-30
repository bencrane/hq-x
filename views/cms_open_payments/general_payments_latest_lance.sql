-- cms_open_payments.general_payments_latest_lance — Lance-format mirror of CMS
-- Open Payments General feed (normalized 15-column schema, 2024 onward).
--
-- Wave 2 Lance sweep — drug/biological/device payments to physicians and
-- teaching hospitals. Years 2018-2023 use a wide-raw 70+ column schema and
-- stay on Parquet+Iceberg until a separate backfill cycle.
--
-- IMPORTANT: ``cms_open_payments_general_lance_raw`` is declared with
-- ``register_at_boot=False`` in ``app/services/lance_views.py``. The
-- underlying Lance dataset has ~15.4M rows; eagerly materializing into
-- DuckDB at FastAPI boot would consume too much memory. Per-record_id
-- random-access lookups should call
-- ``lance.dataset(uri).scanner(filter='record_id = ?')`` directly via
-- Lance, not through this view. This view exists for batch / analytical
-- workloads that explicitly call
-- ``register_lance_view_lazy(con, 'cms_open_payments_general_lance_raw')``
-- to materialize on demand.

CREATE OR REPLACE VIEW cms_open_payments_general_payments_latest_lance AS
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
FROM cms_open_payments_general_lance_raw;
