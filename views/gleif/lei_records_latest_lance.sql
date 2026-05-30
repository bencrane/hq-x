-- gleif.lei_records_latest_lance — Lance-format mirror of GLEIF LEI records.
--
-- Wave 2 Lance sweep — the universal legal-entity identity spine. ~3.3M
-- LEI-registered entities worldwide; weekly snapshot from
-- gleif/snapshot=YYYY-MM-DD/lei_records.parquet.
--
-- Per-LEI lookups should call ``lance.dataset(uri).scanner(filter='lei = ?')``
-- directly via Lance for sub-100ms latency; the DuckDB view exists for
-- SQL-ergonomic aggregate workloads and as the partner-matching engine's
-- join target when normalizing entity identities across sources.

CREATE OR REPLACE VIEW gleif_lei_records_latest_lance AS
SELECT
  lei,
  legal_name,
  legal_name_normalized,
  entity_status,
  entity_category,
  legal_form_id,
  headquarters_country,
  headquarters_region,
  headquarters_city,
  headquarters_postal_code,
  headquarters_zip5,
  legal_address_country,
  legal_address_region,
  legal_address_city,
  legal_address_postal_code,
  registration_status,
  initial_registration_date,
  last_update_date,
  next_renewal_date,
  managing_lou,
  validation_authority_id,
  validation_authority_entity_id,
  gleif_snapshot_date,
  snapshot
FROM gleif_lei_records_lance_raw;
