-- FMCSA supply-side loader (Phase 2 rewrite).
--
-- Source: entities.mv_fmcsa_carrier_master (one-row-per-DOT, ~2.58M
-- current carriers). The directive named motor_carrier_census_records,
-- but raw census is multi-row-per-DOT (16.23M feed snapshots, 4.4M
-- distinct DOTs incl. defunct). Using master keeps target_companies
-- aligned with what mv_fmcsa_supply_carriers will surface
-- (per-directive INNER JOIN to master).
--
-- Per DOT:
--   * Find existing target_company via:
--     1. source_ref (source='fmcsa', source_row_id=dot_number) — handles
--        rerun.
--     2. Domain match in target_companies (when PDL provides a domain).
--     3. linkedin_url match in target_companies (when PDL provides one).
--   * If found: UPDATE the row, COALESCE-preserving existing values for
--     company_name/linkedin_url/phone/mailing_address. entity_role and
--     source are NEVER overwritten on update (Clay/'demand' stays
--     Clay/'demand').
--   * If not found: INSERT with entity_role='supply', source='fmcsa'.
--     For new candidates sharing a domain, one DOT becomes canonical
--     (highest match_reason_count, then dot_number tiebreak); other DOTs
--     attach as additional source_refs.
--
-- Skip rule: a carrier with no domain AND no phone AND no usable
-- mailing address (street + city + state + zip all populated) is not
-- loaded. Still counted in the report.
--
-- Idempotent: rerun produces the same target_companies + source_refs.

BEGIN;

CREATE TEMP TABLE _summary (step text, count bigint) ON COMMIT DROP;

-- ------------------------------------------------------------------
-- 1. Best PDL match per DOT (only DOTs with non-empty PDL domain).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _best_pdl ON COMMIT DROP AS
SELECT DISTINCT ON (dot_number)
  dot_number,
  pdl_id,
  pdl_name,
  pdl_domain_normalized,
  pdl_linkedin_url_lower,
  match_reasons,
  match_reason_count,
  confidence_tier
FROM entities.mv_fmcsa_pdl_matches
WHERE pdl_domain_normalized IS NOT NULL
  AND pdl_domain_normalized <> ''
ORDER BY
  dot_number,
  CASE confidence_tier WHEN 'highest' THEN 1 WHEN 'high' THEN 2 ELSE 3 END,
  match_reason_count DESC,
  pdl_id;

CREATE INDEX ON _best_pdl (dot_number);

-- ------------------------------------------------------------------
-- 2. Candidates from carrier_master, with PDL attached, skip-rule applied.
-- ------------------------------------------------------------------
CREATE TEMP TABLE _candidates ON COMMIT DROP AS
SELECT
  cm.dot_number,
  cm.legal_name,
  cm.dba_name,
  NULLIF(cm.telephone, '')         AS phone,
  cm.physical_street,
  cm.physical_city,
  cm.physical_state,
  cm.physical_zip,
  NULLIF(cm.physical_country, '')  AS physical_country,
  cm.power_unit_count,
  pdl.pdl_id,
  pdl.pdl_name,
  pdl.pdl_domain_normalized        AS domain,
  pdl.pdl_linkedin_url_lower       AS linkedin_url,
  pdl.match_reasons,
  pdl.match_reason_count,
  pdl.confidence_tier,
  (cm.physical_street IS NOT NULL AND cm.physical_street <> ''
    AND cm.physical_city  IS NOT NULL AND cm.physical_city  <> ''
    AND cm.physical_state IS NOT NULL AND cm.physical_state <> ''
    AND cm.physical_zip   IS NOT NULL AND cm.physical_zip   <> '') AS has_addr,
  (cm.telephone IS NOT NULL AND cm.telephone <> '') AS has_phone
FROM entities.mv_fmcsa_carrier_master cm
LEFT JOIN _best_pdl pdl USING (dot_number);

INSERT INTO _summary VALUES ('master_carriers_total',
  (SELECT COUNT(*) FROM _candidates));
INSERT INTO _summary VALUES ('skipped_no_channel',
  (SELECT COUNT(*) FROM _candidates
   WHERE domain IS NULL AND NOT has_phone AND NOT has_addr));

DELETE FROM _candidates
WHERE domain IS NULL AND NOT has_phone AND NOT has_addr;

INSERT INTO _summary VALUES ('candidates_after_skip',
  (SELECT COUNT(*) FROM _candidates));

CREATE INDEX ON _candidates (dot_number);
CREATE INDEX ON _candidates (domain) WHERE domain IS NOT NULL;
CREATE INDEX ON _candidates (linkedin_url) WHERE linkedin_url IS NOT NULL;

-- ------------------------------------------------------------------
-- 3. Resolve to existing target_company_id (source_ref / domain / linkedin).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _resolved ON COMMIT DROP AS
SELECT
  c.*,
  COALESCE(r.target_company_id, tc_dom.id, tc_link.id) AS existing_id
FROM _candidates c
LEFT JOIN entities.target_company_source_refs r
  ON r.source = 'fmcsa' AND r.source_row_id = c.dot_number
LEFT JOIN entities.target_companies tc_dom
  ON c.domain IS NOT NULL AND tc_dom.domain = c.domain
LEFT JOIN entities.target_companies tc_link
  ON c.linkedin_url IS NOT NULL AND tc_link.linkedin_url = c.linkedin_url;

CREATE INDEX ON _resolved (dot_number);
CREATE INDEX ON _resolved (existing_id) WHERE existing_id IS NOT NULL;
CREATE INDEX ON _resolved (domain) WHERE domain IS NOT NULL AND existing_id IS NULL;

-- ------------------------------------------------------------------
-- 4. Pre-assign UUIDs for unresolved candidates.
--    Group by domain when domain is present so multiple DOTs sharing
--    a brand-new domain land on a single canonical row.
-- ------------------------------------------------------------------
ALTER TABLE _resolved ADD COLUMN new_id uuid;

WITH canon AS (
  SELECT DISTINCT ON (domain)
    domain,
    gen_random_uuid() AS new_id
  FROM _resolved
  WHERE existing_id IS NULL AND domain IS NOT NULL
  ORDER BY domain, match_reason_count DESC NULLS LAST, dot_number
)
UPDATE _resolved r
SET new_id = canon.new_id
FROM canon
WHERE r.existing_id IS NULL
  AND r.domain IS NOT NULL
  AND r.domain = canon.domain;

UPDATE _resolved
SET new_id = gen_random_uuid()
WHERE existing_id IS NULL
  AND new_id IS NULL;  -- catches NULL-domain unresolved

INSERT INTO _summary VALUES ('to_insert_distinct_new_companies',
  (SELECT COUNT(DISTINCT new_id) FROM _resolved WHERE existing_id IS NULL));
INSERT INTO _summary VALUES ('to_update_distinct_existing_companies',
  (SELECT COUNT(DISTINCT existing_id) FROM _resolved WHERE existing_id IS NOT NULL));

-- ------------------------------------------------------------------
-- 5. INSERT one row per new_id into target_companies.
--    LinkedIn collision guard: if linkedin_url already exists on a
--    different target_companies row, NULL it (we'd otherwise hit the
--    partial unique index on linkedin_url).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _existing_linkedin ON COMMIT DROP AS
SELECT linkedin_url FROM entities.target_companies
WHERE linkedin_url IS NOT NULL;
CREATE INDEX ON _existing_linkedin (linkedin_url);

WITH new_canonical AS (
  SELECT DISTINCT ON (new_id)
    new_id,
    domain,
    linkedin_url,
    COALESCE(NULLIF(pdl_name, ''), NULLIF(legal_name, ''), NULLIF(dba_name, '')) AS company_name,
    phone,
    has_addr,
    physical_street, physical_city, physical_state, physical_zip, physical_country
  FROM _resolved
  WHERE existing_id IS NULL
  ORDER BY new_id, match_reason_count DESC NULLS LAST, dot_number
)
INSERT INTO entities.target_companies (
  id, company_name, domain, linkedin_url, source, entity_role,
  phone, mailing_address
)
SELECT
  nc.new_id,
  nc.company_name,
  nc.domain,
  CASE
    WHEN nc.linkedin_url IS NULL THEN NULL
    WHEN EXISTS (SELECT 1 FROM _existing_linkedin el WHERE el.linkedin_url = nc.linkedin_url) THEN NULL
    ELSE nc.linkedin_url
  END,
  'fmcsa',
  'supply',
  nc.phone,
  CASE WHEN nc.has_addr THEN
    jsonb_build_object(
      'street',  nc.physical_street,
      'city',    nc.physical_city,
      'state',   nc.physical_state,
      'zip',     nc.physical_zip,
      'country', nc.physical_country
    )
  ELSE NULL END
FROM new_canonical nc;

-- ------------------------------------------------------------------
-- 6. UPDATE existing target_companies (refresh contact info,
--    preserve role/source, COALESCE all other fields).
-- ------------------------------------------------------------------
WITH src AS (
  SELECT DISTINCT ON (existing_id)
    existing_id,
    COALESCE(NULLIF(pdl_name, ''), NULLIF(legal_name, ''), NULLIF(dba_name, '')) AS company_name,
    linkedin_url,
    phone,
    has_addr,
    physical_street, physical_city, physical_state, physical_zip, physical_country
  FROM _resolved
  WHERE existing_id IS NOT NULL
  ORDER BY existing_id, match_reason_count DESC NULLS LAST, dot_number
)
UPDATE entities.target_companies tc
SET
  company_name    = COALESCE(tc.company_name, src.company_name),
  linkedin_url    = COALESCE(
                      tc.linkedin_url,
                      CASE
                        WHEN src.linkedin_url IS NULL THEN NULL
                        WHEN EXISTS (
                          SELECT 1 FROM entities.target_companies tc2
                          WHERE tc2.linkedin_url = src.linkedin_url
                            AND tc2.id <> tc.id
                        ) THEN NULL
                        ELSE src.linkedin_url
                      END
                    ),
  phone           = COALESCE(tc.phone, src.phone),
  mailing_address = COALESCE(
                      tc.mailing_address,
                      CASE WHEN src.has_addr THEN
                        jsonb_build_object(
                          'street',  src.physical_street,
                          'city',    src.physical_city,
                          'state',   src.physical_state,
                          'zip',     src.physical_zip,
                          'country', src.physical_country
                        )
                      ELSE NULL END
                    ),
  updated_at      = now()
FROM src
WHERE tc.id = src.existing_id;

-- ------------------------------------------------------------------
-- 7. INSERT source_refs (one per DOT, idempotent on the unique triple).
-- ------------------------------------------------------------------
WITH ref_ins AS (
  INSERT INTO entities.target_company_source_refs (
    target_company_id, source, source_row_id, match_metadata
  )
  SELECT
    COALESCE(existing_id, new_id),
    'fmcsa',
    dot_number,
    CASE WHEN pdl_id IS NOT NULL THEN
      jsonb_build_object(
        'pdl_id',           pdl_id,
        'match_reasons',    to_jsonb(match_reasons),
        'match_confidence', confidence_tier,
        'fmcsa_legal_name', legal_name,
        'fmcsa_dba',        dba_name,
        'physical_state',   physical_state,
        'power_unit_count', power_unit_count
      )
    ELSE
      jsonb_build_object(
        'pdl_match',        false,
        'fmcsa_legal_name', legal_name,
        'fmcsa_dba',        dba_name,
        'physical_state',   physical_state,
        'power_unit_count', power_unit_count
      )
    END
  FROM _resolved
  ON CONFLICT (target_company_id, source, source_row_id) DO NOTHING
  RETURNING 1
)
INSERT INTO _summary
SELECT 'source_refs_inserted_or_skipped_existing', COUNT(*) FROM ref_ins;

SELECT step, count FROM _summary ORDER BY step;

COMMIT;
