-- usaspending.contracts_latest_lance — Lance-format mirror of USAspending contracts.
--
-- Wave 1 Lance sweep — federal contract awards (fiscal years 2024-2026 in this
-- pass; historical years 2008-2014 are stale and out of scope per directive).
--
-- IMPORTANT: ``usaspending_contracts_lance_raw`` is declared with
-- ``register_at_boot=False`` in ``app/services/lance_views.py``. The
-- underlying Lance dataset has ~30M rows × ~298 columns; eagerly
-- materializing into DuckDB at FastAPI boot would consume too much memory.
-- Per-UEI random-access lookups (the dominant pattern) should call
-- ``lance.dataset(uri).scanner(filter='recipient_uei = ?')`` directly via
-- Lance, not through this view. This view exists for batch / analytical
-- workloads that explicitly call
-- ``register_lance_view_lazy(con, 'usaspending_contracts_lance_raw')`` to
-- materialize on demand.
--
-- Schema-shape parity with the USAspending contracts Parquet upstream
-- (298 columns). This view exposes a representative subset surfaced for
-- partner-matching engines; the full schema remains accessible through
-- ``SELECT * FROM usaspending_contracts_lance_raw``.

CREATE OR REPLACE VIEW usaspending_contracts_latest_lance AS
SELECT
  contract_transaction_unique_key,
  contract_award_unique_key,
  award_id_piid,
  modification_number,
  recipient_uei,
  recipient_duns,
  recipient_name,
  recipient_doing_business_as_name,
  recipient_parent_uei,
  recipient_parent_name,
  recipient_state_code,
  recipient_country_code,
  recipient_city_name,
  cage_code,
  action_date,
  action_date_fiscal_year,
  period_of_performance_start_date,
  period_of_performance_current_end_date,
  awarding_agency_name,
  awarding_sub_agency_name,
  awarding_office_name,
  funding_agency_name,
  funding_sub_agency_name,
  federal_action_obligation,
  total_dollars_obligated,
  current_total_value_of_award,
  potential_total_value_of_award,
  primary_place_of_performance_state_code,
  primary_place_of_performance_country_code,
  primary_place_of_performance_city_name
FROM usaspending_contracts_lance_raw;
