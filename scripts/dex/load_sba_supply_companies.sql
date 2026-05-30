-- SBA supply-side loader (Phase 4B).
--
-- Source: entities.mv_pdl_to_sba_borrowers, filtered to confidence_tier='medium'.
-- The match MV exposes tiers {medium, low, very_low}; 'medium' is the
-- highest tier the MV produces (it represents name+state+locality
-- match at score 0.85). 'low' = name+state only at 0.70. 'very_low' is
-- everything weaker. Per the Phase 4B directive we load only the
-- highest-confidence tier and defer the rest.
--
-- Stable key for source_row_id: sba_loan_id::text. SBA does not assign
-- a stable borrower ID; loans are the atomic record. A single
-- target_company can have many SBA source_refs (one per matched loan),
-- mirroring the FMCSA pattern (one company → many DOTs).
--
-- Per loan:
--   * If the loan matches multiple PDLs, keep the best (score DESC,
--     pdl_id tiebreak).
--   * Group all loans for a single PDL onto one canonical target_company.
--   * Existing-row resolution order:
--       1. source_ref (source='sba', source_row_id=sba_loan_id::text) — handles rerun.
--       2. target_companies.domain match (when PDL has a website).
--       3. target_companies.linkedin_url match.
--   * If found: UPDATE row, COALESCE-preserving existing values.
--     entity_role and source are NEVER overwritten on update.
--   * If not found: INSERT with entity_role='supply', source='sba'.
--
-- Skip rule: rows are kept if PDL match has a domain or linkedin_url
-- (the match MV guarantees at least one — pdl_linkedin_url is
-- non-null for every medium-tier row). No SBA-level skip needed.
--
-- Idempotent: rerun produces the same target_companies + source_refs.

BEGIN;

CREATE TEMP TABLE _summary (step text, count bigint) ON COMMIT DROP;

-- ------------------------------------------------------------------
-- 1. Best PDL match per loan (medium tier only).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _best_match ON COMMIT DROP AS
SELECT DISTINCT ON (sba_loan_id)
  sba_loan_id,
  program,
  pdl_id,
  pdl_name,
  NULLIF(pdl_website, '')        AS pdl_domain,
  NULLIF(pdl_linkedin_url, '')   AS pdl_linkedin_url,
  match_score,
  confidence_tier,
  match_reasons,
  borrower_name,
  borrower_state
FROM entities.mv_pdl_to_sba_borrowers
WHERE confidence_tier = 'medium'
ORDER BY sba_loan_id, match_score DESC, pdl_id;

CREATE INDEX ON _best_match (sba_loan_id);
CREATE INDEX ON _best_match (pdl_id);

INSERT INTO _summary VALUES ('match_rows_medium_tier_total',
  (SELECT COUNT(*) FROM entities.mv_pdl_to_sba_borrowers WHERE confidence_tier = 'medium'));
INSERT INTO _summary VALUES ('distinct_loans_after_best_match',
  (SELECT COUNT(*) FROM _best_match));
INSERT INTO _summary VALUES ('distinct_pdl_ids_in_loader',
  (SELECT COUNT(DISTINCT pdl_id) FROM _best_match));

-- ------------------------------------------------------------------
-- 2. Loan-grain candidates with SBA-side address joined in (most
--    recent loan provides borrower address used by INSERT/UPDATE).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _loans ON COMMIT DROP AS
SELECT
  bm.sba_loan_id,
  bm.program,
  bm.pdl_id,
  bm.pdl_name,
  bm.pdl_domain,
  bm.pdl_linkedin_url,
  bm.match_score,
  bm.confidence_tier,
  bm.match_reasons,
  bm.borrower_name,
  bm.borrower_state                                       AS sba_state,
  COALESCE(s7a.borrstreet, s504.borrstreet)               AS borr_street,
  COALESCE(s7a.borrcity,   s504.borrcity)                 AS borr_city,
  COALESCE(s7a.borrstate,  s504.borrstate)                AS borr_state,
  COALESCE(s7a.borrzip,    s504.borrzip)                  AS borr_zip,
  COALESCE(s7a.approvaldate::date, s504.approvaldate::date) AS approval_date,
  COALESCE(
    s7a.subprogram,
    s504.subprogram
  )                                                        AS subprogram
FROM _best_match bm
LEFT JOIN entities.sba_7a_loans  s7a  ON bm.program = '7a'  AND s7a.id  = bm.sba_loan_id
LEFT JOIN entities.sba_504_loans s504 ON bm.program = '504' AND s504.id = bm.sba_loan_id;

CREATE INDEX ON _loans (sba_loan_id);
CREATE INDEX ON _loans (pdl_id);

-- ------------------------------------------------------------------
-- 3. Canonical SBA-side row per pdl_id (most-recent loan picks the
--    name/address/program shown on target_companies).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _canonical ON COMMIT DROP AS
SELECT DISTINCT ON (pdl_id)
  pdl_id,
  pdl_name,
  pdl_domain,
  pdl_linkedin_url,
  borrower_name,
  borr_street,
  borr_city,
  borr_state,
  borr_zip,
  approval_date,
  (borr_street IS NOT NULL AND borr_street <> ''
    AND borr_city  IS NOT NULL AND borr_city  <> ''
    AND borr_state IS NOT NULL AND borr_state <> ''
    AND borr_zip   IS NOT NULL AND borr_zip   <> '') AS has_addr
FROM _loans
ORDER BY pdl_id, approval_date DESC NULLS LAST, sba_loan_id;

CREATE INDEX ON _canonical (pdl_id);
CREATE INDEX ON _canonical (pdl_domain) WHERE pdl_domain IS NOT NULL;
CREATE INDEX ON _canonical (pdl_linkedin_url) WHERE pdl_linkedin_url IS NOT NULL;

-- ------------------------------------------------------------------
-- 4. Resolve canonical pdl_id rows to existing target_company_id.
--    Source-ref hit at the loan level takes precedence; otherwise
--    domain / linkedin_url.
-- ------------------------------------------------------------------
CREATE TEMP TABLE _resolved_pdl ON COMMIT DROP AS
WITH any_existing_ref AS (
  -- Any prior source_ref for any loan owned by this pdl_id ⇒ same target_company.
  SELECT DISTINCT ON (l.pdl_id)
    l.pdl_id,
    r.target_company_id
  FROM _loans l
  JOIN entities.target_company_source_refs r
    ON r.source = 'sba' AND r.source_row_id = l.sba_loan_id::text
  ORDER BY l.pdl_id, r.target_company_id
)
SELECT
  c.*,
  COALESCE(ref.target_company_id, tc_dom.id, tc_link.id) AS existing_id
FROM _canonical c
LEFT JOIN any_existing_ref ref USING (pdl_id)
LEFT JOIN entities.target_companies tc_dom
  ON c.pdl_domain IS NOT NULL AND tc_dom.domain = c.pdl_domain
LEFT JOIN entities.target_companies tc_link
  ON c.pdl_linkedin_url IS NOT NULL AND tc_link.linkedin_url = c.pdl_linkedin_url;

CREATE INDEX ON _resolved_pdl (pdl_id);
CREATE INDEX ON _resolved_pdl (existing_id) WHERE existing_id IS NOT NULL;

ALTER TABLE _resolved_pdl ADD COLUMN new_id uuid;

-- Pre-assign UUIDs for unresolved canonical PDL rows.
UPDATE _resolved_pdl
SET new_id = gen_random_uuid()
WHERE existing_id IS NULL;

INSERT INTO _summary VALUES ('to_insert_distinct_new_companies',
  (SELECT COUNT(*) FROM _resolved_pdl WHERE existing_id IS NULL));
INSERT INTO _summary VALUES ('to_update_distinct_existing_companies',
  (SELECT COUNT(DISTINCT existing_id) FROM _resolved_pdl WHERE existing_id IS NOT NULL));

-- ------------------------------------------------------------------
-- 5. INSERT new target_companies. Domain collision guard: if
--    pdl_domain already exists on another target_company, NULL it
--    (we'd otherwise hit the partial unique index on domain).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _existing_domains ON COMMIT DROP AS
SELECT domain FROM entities.target_companies WHERE domain IS NOT NULL;
CREATE INDEX ON _existing_domains (domain);

CREATE TEMP TABLE _existing_linkedin ON COMMIT DROP AS
SELECT linkedin_url FROM entities.target_companies WHERE linkedin_url IS NOT NULL;
CREATE INDEX ON _existing_linkedin (linkedin_url);

INSERT INTO entities.target_companies (
  id, company_name, domain, linkedin_url, source, entity_role,
  phone, mailing_address
)
SELECT
  rp.new_id,
  COALESCE(NULLIF(rp.pdl_name, ''), NULLIF(rp.borrower_name, '')),
  CASE
    WHEN rp.pdl_domain IS NULL THEN NULL
    WHEN EXISTS (SELECT 1 FROM _existing_domains ed WHERE ed.domain = rp.pdl_domain) THEN NULL
    ELSE rp.pdl_domain
  END,
  CASE
    WHEN rp.pdl_linkedin_url IS NULL THEN NULL
    WHEN EXISTS (SELECT 1 FROM _existing_linkedin el WHERE el.linkedin_url = rp.pdl_linkedin_url) THEN NULL
    ELSE rp.pdl_linkedin_url
  END,
  'sba',
  'supply',
  NULL,
  CASE WHEN rp.has_addr THEN
    jsonb_build_object(
      'street',  rp.borr_street,
      'city',    rp.borr_city,
      'state',   rp.borr_state,
      'zip',     rp.borr_zip,
      'country', 'US'
    )
  ELSE NULL END
FROM _resolved_pdl rp
WHERE rp.existing_id IS NULL;

-- ------------------------------------------------------------------
-- 6. UPDATE existing target_companies (refresh contact info,
--    preserve role/source, COALESCE all other fields).
-- ------------------------------------------------------------------
WITH src AS (
  SELECT
    existing_id,
    COALESCE(NULLIF(pdl_name, ''), NULLIF(borrower_name, '')) AS company_name,
    pdl_domain,
    pdl_linkedin_url,
    has_addr,
    borr_street, borr_city, borr_state, borr_zip
  FROM _resolved_pdl
  WHERE existing_id IS NOT NULL
)
UPDATE entities.target_companies tc
SET
  company_name    = COALESCE(tc.company_name, src.company_name),
  domain          = COALESCE(
                      tc.domain,
                      CASE
                        WHEN src.pdl_domain IS NULL THEN NULL
                        WHEN EXISTS (
                          SELECT 1 FROM entities.target_companies tc2
                          WHERE tc2.domain = src.pdl_domain AND tc2.id <> tc.id
                        ) THEN NULL
                        ELSE src.pdl_domain
                      END
                    ),
  linkedin_url    = COALESCE(
                      tc.linkedin_url,
                      CASE
                        WHEN src.pdl_linkedin_url IS NULL THEN NULL
                        WHEN EXISTS (
                          SELECT 1 FROM entities.target_companies tc2
                          WHERE tc2.linkedin_url = src.pdl_linkedin_url AND tc2.id <> tc.id
                        ) THEN NULL
                        ELSE src.pdl_linkedin_url
                      END
                    ),
  mailing_address = COALESCE(
                      tc.mailing_address,
                      CASE WHEN src.has_addr THEN
                        jsonb_build_object(
                          'street',  src.borr_street,
                          'city',    src.borr_city,
                          'state',   src.borr_state,
                          'zip',     src.borr_zip,
                          'country', 'US'
                        )
                      ELSE NULL END
                    ),
  updated_at      = now()
FROM src
WHERE tc.id = src.existing_id;

-- ------------------------------------------------------------------
-- 7. Map every loan back to its canonical target_company_id and
--    insert one source_ref per loan (idempotent on the unique triple).
-- ------------------------------------------------------------------
CREATE TEMP TABLE _loan_targets ON COMMIT DROP AS
SELECT
  l.sba_loan_id,
  l.program,
  l.pdl_id,
  l.match_score,
  l.confidence_tier,
  l.match_reasons,
  l.borrower_name,
  l.sba_state,
  l.subprogram,
  COALESCE(rp.existing_id, rp.new_id) AS target_company_id
FROM _loans l
JOIN _resolved_pdl rp USING (pdl_id);

CREATE INDEX ON _loan_targets (target_company_id);

WITH ref_ins AS (
  INSERT INTO entities.target_company_source_refs (
    target_company_id, source, source_row_id, match_metadata
  )
  SELECT
    target_company_id,
    'sba',
    sba_loan_id::text,
    jsonb_build_object(
      'pdl_id',             pdl_id,
      'match_confidence',   confidence_tier,
      'match_score',        match_score,
      'match_reasons',      to_jsonb(match_reasons),
      'sba_borrower_name',  borrower_name,
      'sba_state',          sba_state,
      'loan_program',       program,
      'loan_subprogram',    subprogram
    )
  FROM _loan_targets
  ON CONFLICT (target_company_id, source, source_row_id) DO NOTHING
  RETURNING 1
)
INSERT INTO _summary
SELECT 'source_refs_newly_inserted', COUNT(*) FROM ref_ins;

INSERT INTO _summary VALUES ('source_refs_total_after_load',
  (SELECT COUNT(*) FROM entities.target_company_source_refs WHERE source = 'sba'));

INSERT INTO _summary VALUES ('target_companies_supply_total_after_load',
  (SELECT COUNT(*) FROM entities.target_companies WHERE entity_role = 'supply'));

INSERT INTO _summary VALUES ('target_companies_sba_sourced',
  (SELECT COUNT(*) FROM entities.target_companies WHERE source = 'sba'));

SELECT step, count FROM _summary ORDER BY step;

COMMIT;
