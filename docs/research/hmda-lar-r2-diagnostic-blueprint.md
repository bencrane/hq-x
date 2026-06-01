# HMDA LAR — Legacy R2 Diagnostic & Schema Blueprint

> **Status: DIAGNOSTIC ONLY — hold for formal review. No ingestion worker code written.**
> Probe date: 2026-05-31. All numbers below are measured directly against R2 via
> DuckDB (httpfs + parquet footer/column reads), **not** the Polaris catalog.
> Reproduction commands in the Appendix.

---

## 0. TL;DR — three premise corrections that reshape the ingestion plan

The directive assumed raw, legacy, delimited/fixed-width text staged for first-pass
profiling. The bytes on R2 say otherwise. Three structural realities dominate the design:

1. **It is already Parquet, already DuckDB-normalized.** Every object under `hmda/lar/`
   was written by DuckDB (Parquet v1, ZSTD). There is **no delimiter / fixed-width /
   encoding question** — the data has already been through one DuckDB projection pass and
   landed as columnar, self-describing, typed Parquet. This is effectively the
   architecture's "transport Parquet," sitting one hop short of a Lance emit.
2. **Two HMDA schema generations, already unioned into one wide schema.** 2007–2017
   (legacy LAR) and 2018–2024 (Dynamic LAR) have been harmonized into a single
   **101-column** shared schema; legacy-era files carry **6 extra `legacy_*` columns**
   (107 total) for old-format fields with no modern equivalent. Modern-only columns exist
   in legacy partitions but are **100% NULL** there (verified).
3. **There is no ULI. Anywhere.** The directive's ULI profiling target does not exist as a
   column in this dataset (the public HMDA file strips ULI for privacy). The load-bearing
   institution key is **`lei`** (2018+) and **`legacy_respondent_id`** (2007–2017). Both
   **must be VARCHAR** — proven below by leading zeros and embedded non-numerics.

Net: this is a clean, lossless, query-ready corpus of **~316M LAR rows / 8.7 GiB across 18
yearly partitions**, plus one **2023 reporter-panel** file. The work is a typed Lance emit
with the right index topology — not a text-rescue.

---

## 1. Legacy R2 topology

**Account buckets (R2, S3-compatible endpoint):**

| Bucket | Role |
| --- | --- |
| `dex-raw-landing-zone` | Canonical raw/landing store. **HMDA lives here.** |
| `data-sink` | Separate sink bucket. No HMDA content. |

**HMDA keys under `s3://dex-raw-landing-zone/hmda/`** — 19 objects, 8.7 GiB, all `.parquet`:

| Key | Rows | Size | Writer |
| --- | ---: | ---: | --- |
| `lar/year=2007/lar_2007.parquet` | 26,605,695 | 662.5 MiB | DuckDB |
| `lar/year=2008/lar_2008.parquet` | 17,391,570 | 420.7 MiB | DuckDB |
| `lar/year=2009/lar_2009.parquet` | 19,493,491 | 478.6 MiB | DuckDB |
| `lar/year=2010/lar_2010.parquet` | 16,348,557 | 397.7 MiB | DuckDB |
| `lar/year=2011/lar_2011.parquet` | 14,873,415 | 361.1 MiB | DuckDB |
| `lar/year=2012/lar_2012.parquet` | 18,691,551 | 466.0 MiB | DuckDB |
| `lar/year=2013/lar_2013.parquet` | 17,016,159 | 424.1 MiB | DuckDB |
| `lar/year=2014/lar_2014.parquet` | 12,049,341 | 293.9 MiB | DuckDB |
| `lar/year=2015/lar_2015.parquet` | 14,374,184 | 378.8 MiB | DuckDB |
| `lar/year=2016/lar_2016.parquet` | 16,332,987 | 400.6 MiB | DuckDB |
| `lar/year=2017/lar_2017.parquet` | 14,285,496 | 290.1 MiB | DuckDB |
| `lar/year=2018/lar_2018.parquet` | 15,119,651 | 521.8 MiB | DuckDB |
| `lar/year=2019/lar_2019.parquet` | 17,545,457 | 615.3 MiB | DuckDB |
| `lar/year=2020/lar_2020.parquet` | 25,551,868 | 896.6 MiB | DuckDB |
| `lar/year=2021/lar_2021.parquet` | 26,124,552 | 928.9 MiB | DuckDB |
| `lar/year=2022/lar_2022.parquet` | 16,080,210 | 560.2 MiB | DuckDB |
| `lar/year=2023/lar_2023.parquet` | 11,483,889 | 408.8 MiB | DuckDB |
| `lar/year=2024/lar_2024.parquet` | 12,229,298 | 424.0 MiB | DuckDB |
| `panel/year=2023/panel_2023.parquet` | 5,113 | 278.6 KiB | DuckDB v1.5.2 |
| **TOTAL LAR** | **~316.0M** | **~8.7 GiB** | — |

- **Partitioning:** Hive-style `year=YYYY/`. One object per partition (no multi-part split).
- **Extensions:** uniformly `.parquet`. No `.csv/.txt/.gz/.zip` present.
- **Out of scope (sibling prefixes, not HMDA LAR):** `hud-multifamily/` is HUD multifamily
  data, unrelated to the LAR. `iceberg-test/`, `iceberg-warehouse/`, `polaris-warehouse/`
  are legacy catalog experiments — the directive's "do not rely on Polaris" matches current
  architecture (Gen-3 writes Lance to R2 directly, no Iceberg/Polaris).

---

## 2. Byte layout & encoding (directive: delimited vs fixed-width vs compressed stream)

Resolved at the footer/column-metadata level — no guesswork:

| Attribute | Finding |
| --- | --- |
| Container | Apache Parquet, **format version 1**, single object per partition |
| Compression | **ZSTD** on 100% of column chunks (12,200/12,200 in 2024) |
| Physical types | `BYTE_ARRAY` (UTF-8 string) ×9,272 · `DOUBLE` ×2,806 · `INT32` ×122 |
| Encodings | `PLAIN_DICTIONARY` for low-cardinality (`lei`, `action_taken`, `loan_amount`); `PLAIN` for high-cardinality (`census_tract`) |
| Text encoding | Parquet string columns are **UTF-8 by spec** (BYTE_ARRAY/UTF8). The ASCII/CP1252 ambiguity does not apply to a columnar binary container. |
| Row groups | ~10 row groups per ~1.6M rows (e.g., 2024 = 122 groups / 12.2M rows) |

**Conclusion:** not delimited, not fixed-width, not a compressed multi-part text stream.
It is dictionary-encoded, ZSTD-compressed, UTF-8 columnar Parquet. A lossless text
projection is automatic — string columns are already UTF-8 and typed.

---

## 3. Schema generations — the union-schema reality

| Era | Years | Files | Columns | Shape |
| --- | --- | --- | ---: | --- |
| **Legacy LAR** | 2007–2017 | 11 | **107** | 101 shared + 6 `legacy_*` |
| **Dynamic LAR** | 2018–2024 | 7 | **101** | 101 shared |

- The two eras differ by exactly the **6 legacy-only columns**:
  `legacy_respondent_id`, `legacy_agency_code`, `legacy_property_type`,
  `legacy_sequence_number`, `legacy_edit_status`, `legacy_application_date_indicator`
  (all VARCHAR). The 101 shared columns are **identically named** across all 18 years.
- **Union-fill behavior (verified on 2016):** modern-only columns are physically present in
  legacy partitions but unpopulated —
  `interest_rate` = 0 non-null, `property_value` = 0 non-null,
  `debt_to_income_ratio` = 0 non-null, `lei` = 0 non-null;
  while `loan_amount` = 100%, `legacy_property_type` = 100%, and
  `rate_spread` = 454,671 (~2.8%, late-legacy HOEPA higher-priced-loan reporting).
- **Implication for ingest:** a single emit can span all 18 years **iff** the reader uses
  `union_by_name = true` (legacy 107-wide aligns to modern 101-wide; missing → NULL). This
  is the single most important projection flag.

---

## 4. Identifier reality (the directive's core question)

### 4.1 `lei` — 2018+ institution key
Measured on `lar_2024` (12,229,298 rows):

| Check | Result |
| --- | --- |
| Non-null | 12,229,298 (100%) |
| Length | **20 chars, uniformly** (ISO 17442) |
| Distinct (exact) | **4,908** institutions |
| Starts with `0` | **70,242 rows** → leading zeros real |
| Non-`[A-Z0-9]` | 0 |
| All-digit | 0 (always contains letters) |
| Samples | `5493006G148FKG4GC506`, `254900XN7UWEWK13RO81`, `549300JQEF6V2RNWCC75` |

→ **VARCHAR mandatory.** Leading zeros + embedded letters make any numeric cast lossy.
Source already stores it as VARCHAR — preserve verbatim.

### 4.2 `legacy_respondent_id` — 2007–2017 institution key
Measured on `lar_2016` (16,332,987 rows):

| Check | Result |
| --- | --- |
| Non-null | 16,332,987 (100%) |
| Length | 10 chars, uniformly |
| Distinct | 6,644 respondents |
| Starts with `0` | **7,933,596 rows (≈49%)** → heavy zero-padding |
| Samples | `0000031286`, `0000675332`, **`26-4599244`** (tax-ID form with a dash) |

→ **VARCHAR mandatory.** Both zero-padded numerics and dash-bearing tax-ID strings appear
in the same column. `legacy_agency_code` is a clean single digit ∈ {1,2,3,5,7,9}
(OCC/FRB/FDIC/NCUA/HUD/CFPB).

### 4.3 `uli` — absent
No `uli` column exists in any LAR partition. The directive's ULI leading-zero / non-numeric
analysis has **no target in this corpus**. If ULI is required downstream, it is **not
recoverable from this staged data** and must be sourced from the non-public HMDA snapshot.

### 4.4 Cross-era bridge — the `panel` file
`panel/year=2023` (5,113 rows) maps **`lei` → institution identity**: one row per LEI
(5,113 distinct LEIs = primary key), carrying `respondent_name`, `tax_id`, `agency_code`,
`respondent_rssd` (4,751 distinct), `id_2017` (4,364 distinct), parent/topholder RSSD+name,
and `assets` (VARCHAR holding integer strings; `-1` = missing sentinel).

- Of 4,908 distinct LEIs in **2024** LAR, **4,777** resolve in the **2023** panel (≈131
  2024 new-entrants absent, as expected for a prior-year panel).
- **Gap:** only the **2023** panel is staged. There is **no panel coverage for 2018–2022 or
  2024**, and no `respondent_id ↔ lei` crosswalk for the legacy era. Cross-era entity
  resolution (legacy `legacy_respondent_id` → modern `lei`) is therefore **not closeable**
  with what is on R2 today.

---

## 5. Cardinality & data-typing analysis

### 5.1 Cardinality matrix (`lar_2024`, approx_count_distinct)

| Field | Distinct | Index class |
| --- | ---: | --- |
| `census_tract` | ~116,641 | BTREE |
| `lei` | 4,908 (exact) | BTREE |
| `county_code` | ~2,816 | BTREE |
| `derived_msa_md` | ~435 | BTREE |
| `state_code` | 55 | BITMAP |
| `purchaser_type` | 10 | BITMAP |
| `derived_race` | 10 | BITMAP |
| `action_taken` | 8 | BITMAP |
| `loan_purpose` | 6 | BITMAP |
| `loan_type` | 4 | BITMAP |
| `derived_sex` | 4 | BITMAP |
| `conforming_loan_limit` | 4 | BITMAP |
| `occupancy_type` | 3 | BITMAP |
| `hoepa_status` | 3 | BITMAP |
| `lien_status` | 2 | BITMAP |

### 5.2 Code-field cleanliness (clean enumerations, no nulls/garbage)

| Field | Domain observed | Note |
| --- | --- | --- |
| `action_taken` | 1–8 | identical domain 2016 & 2024 |
| `loan_type` | 1,2,3,4 | identical domain both eras |
| `loan_purpose` | 1,2,31,32,4,5 | Dynamic LAR splits refi → 31/32 |
| `purchaser_type` | 0,1,2,3,4,5,6,71,72,8,9 | two-digit GSE codes 71/72 |
| `occupancy_type` | 1,2,3 | clean |
| `lien_status` | 1,2 | clean |
| `derived_dwelling_category` | 4 string categories | e.g. `Single Family (1-4 Units):Site-Built` |
| `derived_loan_product_type` | 8 `Type:Lien` strings | e.g. `Conventional:First Lien` |
| `conforming_loan_limit` | C / NC / NA / U | clean |

### 5.3 Geography — structural cleanliness + sentinel discipline (`lar_2024`)

| Field | Shape | Missing handling |
| --- | --- | --- |
| `state_code` | 2-char USPS (`AK`,`AL`,…), 55 distinct | 0 NULL; **215,802 = literal `'NA'`** |
| `county_code` | 5-char state+county FIPS; **2,079,755 leading-zero** | 0 NULL; **298,185 = `'NA'`** |
| `census_tract` | **11-char GEOID** (`08035014410`); **2,073,526 leading-zero** | 0 NULL; **350,626 = `'NA'`** |

→ Geo fields are VARCHAR-mandatory on two counts: leading-zero GEOIDs **and** the literal
`'NA'` missing-sentinel (a numeric cast would both drop leading zeros and choke on `'NA'`).

### 5.4 Numeric fields (`lar_2024`)

| Field | min | max | NULLs | Note |
| --- | ---: | ---: | ---: | --- |
| `loan_amount` | 5,000 | 2,962,005,000 | 0 | 0 negatives; integral (midpoint-rounded) → prefer **BIGINT** over DOUBLE |
| `property_value` | — | — | 2,739,983 (~22%) | DOUBLE |
| `income` | — | — | 1,776,284 (~14.5%) | DOUBLE (thousands) |
| `interest_rate` | 0.0 | 60.0 | 4,512,952 (~37%) | NULL for non-originated; `max 60.0` worth a downstream sanity bound |

### 5.5 The VARCHAR-vs-numeric trap field: `debt_to_income_ratio`
Heterogeneous by design — **must stay VARCHAR**. Observed values mix:
binned ranges (`20%-<30%`, `30%-<36%`, `50%-60%`, `>60%`, `<20%`), bare integers as text
(`41`,`42`,`43`,`44`,`49`), the `'Exempt'` reporting state (284,875), and `'NA'` (4,056,284).
This is the canonical example of why HMDA "ratio/amount" fields cannot be blanket-cast:
DuckDB's prior pass correctly kept the numeric ratios (`combined_loan_to_value_ratio`) as
DOUBLE while leaving DTI as VARCHAR.

---

## 6. DuckDB extraction projection (the precise query)

**Design stance (lossless, per directive):** the Lance system-of-record table preserves all
identifiers/codes as **VARCHAR verbatim** — including `'NA'`/`'Exempt'` sentinels and leading
zeros — and casts only the genuinely numeric measures. Sentinel→NULL normalization belongs in
downstream read-views, not at the lossless emit. `union_by_name` spans both schema eras.

```sql
-- 1) Source span: all 18 LAR partitions, both schema generations aligned by name.
CREATE OR REPLACE VIEW hmda_lar_src AS
SELECT *
FROM read_parquet(
        's3://dex-raw-landing-zone/hmda/lar/*/*.parquet',
        hive_partitioning = true,
        union_by_name     = true     -- legacy(107) ⋃ modern(101); absent cols -> NULL
     );

-- 2) Canonical typed projection -> streamed to Lance (system of record).
CREATE OR REPLACE VIEW hmda_lar_canonical AS
SELECT
  -- ── temporal / partition ───────────────────────────────────────────────
  CAST("year" AS INTEGER)                       AS activity_year,   -- from hive partition; canonical int
  -- ── resolution keys (VARCHAR verbatim, leading zeros preserved) ─────────
  lei,                                                              -- 20-char ISO-17442; NULL 2007-2017
  legacy_respondent_id,                                            -- 10-char zero-padded / tax-id; NULL 2018+
  legacy_agency_code,
  -- ── geography (VARCHAR GEOIDs; 'NA' sentinel kept verbatim) ─────────────
  state_code, county_code, census_tract, derived_msa_md,
  -- ── categorical codes (VARCHAR verbatim) ───────────────────────────────
  action_taken, preapproval, loan_type, loan_purpose, lien_status,
  reverse_mortgage, open_end_line_of_credit, business_or_commercial_purpose,
  occupancy_type, construction_method, purchaser_type, hoepa_status,
  conforming_loan_limit, derived_loan_product_type, derived_dwelling_category,
  derived_ethnicity, derived_race, derived_sex,
  manufactured_home_secured_property_type, manufactured_home_land_property_interest,
  negative_amortization, interest_only_payment, balloon_payment, other_nonamortizing_features,
  submission_of_application, initially_payable_to_institution,
  -- ── applicant / co-applicant demographic arrays (VARCHAR verbatim) ──────
  applicant_ethnicity_1, applicant_ethnicity_2, applicant_ethnicity_3, applicant_ethnicity_4, applicant_ethnicity_5,
  co_applicant_ethnicity_1, co_applicant_ethnicity_2, co_applicant_ethnicity_3, co_applicant_ethnicity_4, co_applicant_ethnicity_5,
  applicant_ethnicity_observed, co_applicant_ethnicity_observed,
  applicant_race_1, applicant_race_2, applicant_race_3, applicant_race_4, applicant_race_5,
  co_applicant_race_1, co_applicant_race_2, co_applicant_race_3, co_applicant_race_4, co_applicant_race_5,
  applicant_race_observed, co_applicant_race_observed,
  applicant_sex, co_applicant_sex, applicant_sex_observed, co_applicant_sex_observed,
  applicant_age, co_applicant_age, applicant_age_above_62, co_applicant_age_above_62,
  applicant_credit_score_type, co_applicant_credit_score_type,
  aus_1, aus_2, aus_3, aus_4, aus_5,
  denial_reason_1, denial_reason_2, denial_reason_3, denial_reason_4,
  -- ── mixed-domain VARCHAR (ranges + ints + 'Exempt'/'NA') -> keep VARCHAR ─
  debt_to_income_ratio,
  -- ── measures (numeric) ─────────────────────────────────────────────────
  CAST(loan_amount AS BIGINT)                   AS loan_amount,     -- integral; avoid float semantics
  interest_rate, rate_spread, combined_loan_to_value_ratio,
  total_loan_costs, total_points_and_fees, origination_charges, discount_points, lender_credits,
  loan_term, prepayment_penalty_term, intro_rate_period,
  property_value, income, total_units, multifamily_affordable_units,
  -- ── tract context measures (numeric) ───────────────────────────────────
  tract_population, tract_minority_population_percent, ffiec_msa_md_median_family_income,
  tract_to_msa_income_percentage, tract_owner_occupied_units, tract_one_to_four_family_homes,
  tract_median_age_of_housing_units,
  -- ── legacy-only passthrough (VARCHAR; NULL for 2018+) ───────────────────
  legacy_property_type, legacy_sequence_number, legacy_edit_status, legacy_application_date_indicator
FROM hmda_lar_src;
```

Notes:
- Drop the redundant year columns: source carries `activity_year` (VARCHAR), `dataset_year`
  (SMALLINT), and the hive `year` (BIGINT). Standardize on one int `activity_year`.
- The emit follows the house pattern — DuckDB projection → `lance.write_dataset(s3://…/hmda_lar_lance, data_storage_version="2.0")` → R2; Lance is the system of record (no catalog round-trip). The panel emits as a small companion dataset `hmda_panel_lance`.

---

## 7. Indexing topology (Lance scalar indexes)

| Index | Columns | Justification |
| --- | --- | --- |
| **BTREE** | `lei`, `legacy_respondent_id`, `census_tract`, `county_code`, `derived_msa_md`, `activity_year` | High-cardinality resolution / join / range keys. `lei` and `census_tract` are the primary lookup spines (institution ↔ geography). |
| **BITMAP** | `action_taken`, `loan_type`, `loan_purpose`, `preapproval`, `lien_status`, `occupancy_type`, `purchaser_type`, `hoepa_status`, `conforming_loan_limit`, `derived_loan_product_type`, `derived_dwelling_category`, `derived_race`, `derived_sex`, `derived_ethnicity`, `state_code`, `construction_method`, `reverse_mortgage`, `open_end_line_of_credit`, `business_or_commercial_purpose` | Low-cardinality (≤~60) categoricals; bitmap excels at the equality/`IN` filters these drive (e.g. "originated FHA purchase loans in MSA X"). |
| **none** (measures) | `loan_amount`, `interest_rate`, fee/ratio/property/income/tract_* DOUBLEs | Range-scan/aggregation columns; rely on Parquet/Lance zonemaps. Add BTREE on `loan_amount` only if range predicates become hot. |

`state_code` is borderline (55 distinct) — bitmap is the right call given it is almost always
a filter, not a join key. `applicant_*`/`co_applicant_*`/`aus_*`/`denial_reason_*` arrays are
optional bitmap targets — index only the ones that become live filters (likely
`applicant_race_1`, `applicant_sex`, `denial_reason_1`).

---

## 8. Field-by-field schema layout

Legend — **Era**: B = both eras populated · M = modern-only (NULL 2007–2017) · L = legacy-only (NULL 2018+) · P = partial.
**Idx**: 🔑 BTREE · ▦ BITMAP · · measure/none.

### 8.1 LAR — shared 101-column schema

| # | Column | Type | Era | Domain / sample | Idx |
| ---: | --- | --- | :-: | --- | :-: |
| 1 | `activity_year` | VARCHAR | B | `2007`…`2024` (redundant w/ partition) | 🔑 |
| 2 | `lei` | VARCHAR | M | 20-char ISO-17442; leading zeros | 🔑 |
| 3 | `derived_msa_md` | VARCHAR | B | ~435 distinct MSA/MD codes | 🔑 |
| 4 | `state_code` | VARCHAR | B | 2-char USPS; `'NA'` sentinel | ▦ |
| 5 | `county_code` | VARCHAR | B | 5-char FIPS; leading zeros; `'NA'` | 🔑 |
| 6 | `census_tract` | VARCHAR | B | 11-char GEOID; leading zeros; `'NA'` | 🔑 |
| 7 | `conforming_loan_limit` | VARCHAR | M | C / NC / NA / U | ▦ |
| 8 | `derived_loan_product_type` | VARCHAR | M | 8 `Type:Lien` strings | ▦ |
| 9 | `derived_dwelling_category` | VARCHAR | M | 4 categories | ▦ |
| 10 | `derived_ethnicity` | VARCHAR | B | derived rollup | ▦ |
| 11 | `derived_race` | VARCHAR | B | ~10 | ▦ |
| 12 | `derived_sex` | VARCHAR | B | 4 | ▦ |
| 13 | `action_taken` | VARCHAR | B | 1–8 | ▦ |
| 14 | `purchaser_type` | VARCHAR | B | 0,1–6,71,72,8,9 | ▦ |
| 15 | `preapproval` | VARCHAR | B | 1,2 | ▦ |
| 16 | `loan_type` | VARCHAR | B | 1–4 | ▦ |
| 17 | `loan_purpose` | VARCHAR | B | 1,2,31,32,4,5 | ▦ |
| 18 | `lien_status` | VARCHAR | B | 1,2 | ▦ |
| 19 | `reverse_mortgage` | VARCHAR | M | 1,2,1111 | ▦ |
| 20 | `open_end_line_of_credit` | VARCHAR | M | 1,2,1111 | ▦ |
| 21 | `business_or_commercial_purpose` | VARCHAR | M | 1,2,1111 | ▦ |
| 22 | `loan_amount` | DOUBLE→BIGINT | B | 5,000 … 2.96B; integral | · |
| 23 | `combined_loan_to_value_ratio` | DOUBLE | M | ratio | · |
| 24 | `interest_rate` | DOUBLE | M | 0–60; 37% NULL | · |
| 25 | `rate_spread` | DOUBLE | P | partial in legacy (HOEPA) | · |
| 26 | `hoepa_status` | VARCHAR | B | 1,2,3 | ▦ |
| 27 | `total_loan_costs` | DOUBLE | M | fees | · |
| 28 | `total_points_and_fees` | DOUBLE | M | fees | · |
| 29 | `origination_charges` | DOUBLE | M | fees | · |
| 30 | `discount_points` | DOUBLE | M | fees | · |
| 31 | `lender_credits` | DOUBLE | M | fees | · |
| 32 | `loan_term` | DOUBLE | M | months | · |
| 33 | `prepayment_penalty_term` | DOUBLE | M | months | · |
| 34 | `intro_rate_period` | DOUBLE | M | months | · |
| 35 | `negative_amortization` | VARCHAR | M | 1,2,1111 | ▦ |
| 36 | `interest_only_payment` | VARCHAR | M | 1,2,1111 | ▦ |
| 37 | `balloon_payment` | VARCHAR | M | 1,2,1111 | ▦ |
| 38 | `other_nonamortizing_features` | VARCHAR | M | 1,2,1111 | ▦ |
| 39 | `property_value` | DOUBLE | M | ~22% NULL | · |
| 40 | `construction_method` | VARCHAR | M | 1,2 | ▦ |
| 41 | `occupancy_type` | VARCHAR | B | 1,2,3 | ▦ |
| 42 | `manufactured_home_secured_property_type` | VARCHAR | M | 1,2,3,1111 | ▦ |
| 43 | `manufactured_home_land_property_interest` | VARCHAR | M | 1–5,1111 | ▦ |
| 44 | `total_units` | DOUBLE | B | 1,2,3,4,5+ | · |
| 45 | `multifamily_affordable_units` | DOUBLE | M | count/NA | · |
| 46 | `income` | DOUBLE | B | thousands; ~14.5% NULL | · |
| 47 | `debt_to_income_ratio` | VARCHAR | M | ranges + ints + Exempt/NA | · |
| 48 | `applicant_credit_score_type` | VARCHAR | M | code | ▦ |
| 49 | `co_applicant_credit_score_type` | VARCHAR | M | code | ▦ |
| 50–54 | `applicant_ethnicity_1..5` | VARCHAR | B | code arrays | ▦¹ |
| 55–59 | `co_applicant_ethnicity_1..5` | VARCHAR | B | code arrays | · |
| 60 | `applicant_ethnicity_observed` | VARCHAR | B | code | · |
| 61 | `co_applicant_ethnicity_observed` | VARCHAR | B | code | · |
| 62–66 | `applicant_race_1..5` | VARCHAR | B | code arrays | ▦¹ |
| 67–71 | `co_applicant_race_1..5` | VARCHAR | B | code arrays | · |
| 72 | `applicant_race_observed` | VARCHAR | B | code | · |
| 73 | `co_applicant_race_observed` | VARCHAR | B | code | · |
| 74 | `applicant_sex` | VARCHAR | B | code | ▦¹ |
| 75 | `co_applicant_sex` | VARCHAR | B | code | · |
| 76 | `applicant_sex_observed` | VARCHAR | B | code | · |
| 77 | `co_applicant_sex_observed` | VARCHAR | B | code | · |
| 78 | `applicant_age` | VARCHAR | M | age band | ▦ |
| 79 | `co_applicant_age` | VARCHAR | M | age band | · |
| 80 | `applicant_age_above_62` | VARCHAR | M | Yes/No/NA | ▦ |
| 81 | `co_applicant_age_above_62` | VARCHAR | M | Yes/No/NA | · |
| 82 | `submission_of_application` | VARCHAR | M | code | ▦ |
| 83 | `initially_payable_to_institution` | VARCHAR | M | code | ▦ |
| 84–88 | `aus_1..5` | VARCHAR | M | AUS code arrays | ▦¹ |
| 89–92 | `denial_reason_1..4` | VARCHAR | B | code arrays | ▦¹ |
| 93 | `tract_population` | DOUBLE | M | census context | · |
| 94 | `tract_minority_population_percent` | DOUBLE | M | census context | · |
| 95 | `ffiec_msa_md_median_family_income` | DOUBLE | M | dollars | · |
| 96 | `tract_to_msa_income_percentage` | DOUBLE | M | percent | · |
| 97 | `tract_owner_occupied_units` | DOUBLE | M | count | · |
| 98 | `tract_one_to_four_family_homes` | DOUBLE | M | count | · |
| 99 | `tract_median_age_of_housing_units` | DOUBLE | M | years | · |
| 100 | `dataset_year` | SMALLINT | B | redundant year | · |
| 101 | `year` | BIGINT | B | hive partition | 🔑² |

¹ Index only the `_1` head of each code array if/when it becomes a live filter.
² `year`/`activity_year`/`dataset_year` collapse to one canonical int key at emit.

### 8.2 LAR — legacy-only columns (2007–2017; NULL for 2018+)

| Column | Type | Domain / sample | Idx |
| --- | --- | --- | :-: |
| `legacy_respondent_id` | VARCHAR | 10-char zero-padded / tax-id (`0000031286`, `26-4599244`) | 🔑 |
| `legacy_agency_code` | VARCHAR | 1,2,3,5,7,9 | ▦ |
| `legacy_property_type` | VARCHAR | 1,2,3 (pre-2018 property type) | ▦ |
| `legacy_sequence_number` | VARCHAR | per-respondent record seq | · |
| `legacy_edit_status` | VARCHAR | edit/validity flag | ▦ |
| `legacy_application_date_indicator` | VARCHAR | date-completeness flag | ▦ |

### 8.3 Panel — `panel_2023` (5,113 rows; LEI = primary key)

| Column | Type | Role | Idx |
| --- | --- | --- | :-: |
| `activity_year` | VARCHAR | `2023` | · |
| `lei` | VARCHAR | **PK**; 5,113 distinct | 🔑 |
| `tax_id` | VARCHAR | `NN-NNNNNNN` form | 🔑 |
| `agency_code` | VARCHAR | 6 distinct regulators | ▦ |
| `id_2017` | VARCHAR | pre-2018 panel id (4,364 distinct) | 🔑 |
| `respondent_rssd` | VARCHAR | RSSD id (4,751 distinct) | 🔑 |
| `respondent_name` | VARCHAR | institution name | · |
| `respondent_state` | VARCHAR | 2-char | ▦ |
| `respondent_city` | VARCHAR | city | · |
| `assets` | VARCHAR | integer string; `-1` = missing | · |
| `other_lender_code` | VARCHAR | code | ▦ |
| `parent_rssd` | VARCHAR | parent RSSD (`-1` missing) | 🔑 |
| `parent_name` | VARCHAR | parent name | · |
| `topholder_rssd` | VARCHAR | top-holder RSSD (`-1` missing) | 🔑 |
| `topholder_name` | VARCHAR | top-holder name | · |
| `dataset_year` / `year` | SMALLINT / BIGINT | partition/year | · |

---

## 9. Open items / blueprint flags (decisions for the formal review)

1. **Panel coverage is 2023-only.** No `lei → institution` panel for 2018–2022/2024 and no
   legacy `respondent_id → lei` crosswalk. Cross-era entity resolution cannot be closed from
   staged data — source the full FFIEC panel series before promising institution lineage.
2. **ULI does not exist here.** Any ULI-keyed requirement must come from the non-public HMDA
   snapshot; it is not in `dex-raw-landing-zone`.
3. **Sentinel policy.** `'NA'` (geo, DTI) and `'Exempt'` (DTI and other 2018+ fields) are
   load-bearing reporting states. Recommendation: keep verbatim in the lossless Lance table;
   normalize to typed NULL in downstream read-views only.
4. **Year-column redundancy.** Collapse `activity_year` / `dataset_year` / hive `year` to one
   canonical INT at emit.
5. **`loan_amount` typing.** Integral and midpoint-rounded — emit as BIGINT, not DOUBLE.
6. **`interest_rate` max = 60.0** — apply a downstream plausibility bound; likely
   tail/erroneous reports, not a blocker.
7. **Provenance.** These are already DuckDB-written, not raw upstream — confirm the prior
   normalization is the intended canonical projection before re-deriving, to avoid a
   double-transform.

---

## Appendix — reproduction (read-only)

R2 credentials resolve from Doppler `hq-x/prd` (`R2_ENDPOINT`, `R2_ACCESS_KEY_ID`,
`R2_SECRET_ACCESS_KEY`). R2 is S3-compatible; all probes are read-only.

```bash
# Topology
doppler run --project hq-x --config prd -- bash -c '
  export AWS_ACCESS_KEY_ID=$R2_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY=$R2_SECRET_ACCESS_KEY AWS_DEFAULT_REGION=auto
  aws s3 ls s3://dex-raw-landing-zone/hmda/ --recursive --human-readable --summarize --endpoint-url "$R2_ENDPOINT"'

# Schema / cardinality / footer (DuckDB httpfs, credential_chain over AWS_* env)
#   INSTALL httpfs; LOAD httpfs;
#   CREATE SECRET r2 (TYPE s3, PROVIDER credential_chain, ENDPOINT '<host>', REGION 'auto', URL_STYLE 'path', USE_SSL true);
#   DESCRIBE SELECT * FROM read_parquet('s3://dex-raw-landing-zone/hmda/lar/year=2024/lar_2024.parquet');
#   SELECT num_rows, created_by FROM parquet_file_metadata('s3://.../lar_2024.parquet');
#   SELECT compression, count(*) FROM parquet_metadata('s3://.../lar_2024.parquet') GROUP BY 1;
```
