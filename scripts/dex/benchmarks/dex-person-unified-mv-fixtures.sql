-- dex-person-unified-mv-fixtures.sql
--
-- Self-checking normalization fixture suite for the entities.person_unified
-- MV (directive: 2026-05-02-dex-person-unified-mv.md).
--
-- Contract:
--   The MV migration MUST define two IMMUTABLE SQL functions, both LANGUAGE
--   sql, both deterministic on the input string:
--
--     entities.person_unified_normalize_name(input text)    RETURNS text
--     entities.person_unified_normalize_address(input text) RETURNS text
--
--   Rules (pinned by validator):
--
--   NAME normalization:
--     1. Lowercase all letters.
--     2. Strip leading/trailing whitespace.
--     3. Collapse internal whitespace to a single ASCII space.
--     4. Strip every character that is NOT [a-z0-9 ] (after lowercasing).
--        i.e. punctuation including commas/periods/hyphens/apostrophes are
--        removed. Internal spaces are preserved by step 3.
--     5. NULL or empty input → NULL output (callers are expected to filter
--        NULLs out of the MV; the dedup key is enforced NOT NULL by the MV
--        WHERE clause, see C11).
--
--   ADDRESS normalization:
--     1. Lowercase all letters.
--     2. Strip leading/trailing whitespace.
--     3. Collapse internal whitespace to a single ASCII space.
--     4. Strip every character that is NOT [a-z0-9 ,] (commas preserved as
--        soft delimiters; periods, hyphens, slashes, # all removed).
--     5. Apply USPS-ish abbreviation collapse using whole-word substitution
--        (only the canonical set below; do NOT extend silently — the test
--        suite freezes the rule set):
--          street    → st
--          avenue    → ave
--          boulevard → blvd
--          road      → rd
--          drive     → dr
--          lane      → ln
--          court     → ct
--          place     → pl
--          terrace   → ter
--          square    → sq
--          highway   → hwy
--          parkway   → pkwy
--          north     → n
--          south     → s
--          east      → e
--          west      → w
--          apartment → apt
--          suite     → ste
--          floor     → fl
--          room      → rm
--          building  → bldg
--          number    → num
--     6. After abbreviation pass, collapse whitespace once more.
--     7. NULL or empty → NULL.
--
-- Test mechanism:
--   This script SELECTs each (input, expected, actual) tuple from a VALUES
--   table and emits one row PER MISMATCH. The benchmark harness counts rows
--   in the output: 0 rows = PASS, >0 rows = FAIL.
--
-- The script must run cleanly without -X if the migration is applied. If the
-- functions don't exist yet, psql errors out (which the harness treats as a
-- C12 failure).
--
-- Invoke with `psql -At -v ON_ERROR_STOP=1 -f <this-file>` so output is one
-- mismatch per line and zero non-mismatch chrome lands on stdout.

WITH name_fixtures(input, expected) AS (
  VALUES
    ('John Smith',                       'john smith'),
    ('  John   Smith  ',                 'john smith'),
    ('JOHN A. SMITH',                    'john a smith'),
    ('O''Brien, Patrick',                'obrien patrick'),
    ('Smith-Jones, Mary',                'smithjones mary'),
    ('María García Ñoño',                 'mara garca oo'),     -- ASCII-only stripping, accented letters fall outside [a-z0-9 ]
    ('Dr. Steven L. Lain Jr.',           'dr steven l lain jr'),
    ('   ',                               NULL),                 -- empty after trim → NULL
    (NULL,                                NULL),
    ('Jean-Luc  Picard',                 'jeanluc picard'),
    ('McDonald''s',                      'mcdonalds'),
    ('Robert "Bobby" Tables',            'robert bobby tables'),
    ('a',                                'a'),                   -- single character pass-through
    ('1234',                             '1234')                 -- digits preserved
),
name_check AS (
  SELECT input, expected,
         entities.person_unified_normalize_name(input) AS actual
  FROM name_fixtures
),
addr_fixtures(input, expected) AS (
  VALUES
    ('123 Main Street',                              '123 main st'),
    ('456 Park Avenue, Apt 7B',                      '456 park ave, apt 7b'),
    ('  789 South 5th Boulevard  ',                  '789 s 5th blvd'),
    ('1010 N. Highway 99',                           '1010 n hwy 99'),
    ('Suite 200, 555 East Drive',                    'ste 200, 555 e dr'),
    ('PO Box 47',                                    'po box 47'),
    ('West 34th Street, Floor 12',                   'w 34th st, fl 12'),
    ('Building A, Apartment 9C',                     'bldg a, apt 9c'),
    ('123-A Oak Lane #5',                            '123a oak ln 5'),
    ('99 Place de la République',                    '99 pl de la rpublique'), -- accents stripped, place→pl
    (NULL,                                            NULL),
    ('   ',                                           NULL),
    ('500 Court Road / Box 1',                       '500 ct rd box 1'),
    ('1 Square Park Terrace',                        '1 sq park ter'),
    ('77 Parkway, Number 4',                         '77 pkwy, num 4')
),
addr_check AS (
  SELECT input, expected,
         entities.person_unified_normalize_address(input) AS actual
  FROM addr_fixtures
)

SELECT 'name' AS kind, input, expected, actual
FROM name_check
WHERE expected IS DISTINCT FROM actual
UNION ALL
SELECT 'addr', input, expected, actual
FROM addr_check
WHERE expected IS DISTINCT FROM actual
ORDER BY 1, 2;
