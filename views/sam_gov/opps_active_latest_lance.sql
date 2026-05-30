-- sam_gov.opps_active_latest_lance — Lance-format mirror of SAM.gov opps active.
--
-- Wave 1 Lance sweep — daily SAM.gov opportunity feed. Reads from the Lance
-- dataset registered by ``apps/data-engine-x/app/services/lance_views.py``
-- (Arrow-bridge registration named ``sam_gov_opps_active_lance_raw``).
--
-- The "_ingest_*" columns from the SAM.gov ingest pipeline are dropped here
-- (they belong to provenance, not the public surface). Use the raw Lance
-- dataset via ``lance.dataset(uri).scanner(...)`` if you need them.
--
-- Per-notice random-access goes through Lance scanner directly with
-- ``filter='notice_id = ...'``.

CREATE OR REPLACE VIEW sam_gov_opps_active_latest_lance AS
SELECT
  notice_id,
  title,
  sol_num,
  department_agency,
  cgac,
  sub_tier,
  fpds_code,
  office,
  aac_code,
  posted_date,
  notice_type,
  base_type,
  archive_type,
  archive_date,
  set_aside_code,
  set_aside,
  response_deadline,
  naics_code,
  classification_code,
  pop_street_address,
  pop_city,
  pop_state,
  pop_zip,
  pop_country,
  active_flag,
  award_number,
  award_date,
  award_amount,
  awardee,
  primary_contact_title,
  primary_contact_fullname,
  primary_contact_email,
  primary_contact_phone,
  primary_contact_fax,
  secondary_contact_title,
  secondary_contact_fullname,
  secondary_contact_email,
  secondary_contact_phone,
  secondary_contact_fax,
  organization_type,
  org_state,
  org_city,
  org_zip,
  org_country,
  additional_info_link,
  link,
  description,
  snapshot AS snapshot_date
FROM sam_gov_opps_active_lance_raw;
