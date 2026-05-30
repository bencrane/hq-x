"""HMDA LAR legacy → modern schema mapping.

Maps the CFPB-historic LAR CSV columns (used 2007-2017) onto the modern
post-Dodd-Frank schema (used 2018+, 99 columns). This lets legacy years
slot into the same R2 layout (`hmda/lar/year=*/lar_*.parquet`) and the
same RisingWave glob match_pattern that PR #211's `source_hmda_lar_r2`
table reads from — no DDL changes required.

The `LEGACY_TO_MODERN` map is the source of truth. It declares, for each
modern column, how to populate it from the legacy columns:

  - direct copy:        ('legacy_col_name', 'copy')
  - column rename:      ('legacy_col_name', 'rename')   # alias for 'copy'
  - unit conversion:    ('legacy_col_name', 'x1000')    # multiply VARCHAR-as-DOUBLE
  - NULL-fill:          (None,            'null')

The companion `LEGACY_ONLY_PRESERVED` set names columns that exist in
legacy but have no modern equivalent — they're carried forward as
`legacy_*` columns so respondent-id-based joins against FFIEC Panel still
work. The numerous `_name` companion columns (e.g.,
`agency_name`, `loan_type_name`) are dropped — they're human-readable
labels that mirror the code values, recoverable from CFPB code dictionaries.

Source: 78-column schema observed in
`hmda_2017_nationwide_all-records_labels.csv` (CFPB historic data
mirror at `files.consumerfinance.gov/hmda-historic-loan-data/`).
Verified against `lar_record_format.pdf` (CFPB historic data
dictionaries). The CFPB-republished historic files normalized the
shape of 2007-2017 to a single consistent schema; the underlying FFIEC
raw archive (1990-2006) uses a different layout and is out of scope
for this module.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Modern schema (101 cols) introspected from
# s3://dex-raw-landing-zone/hmda/lar/year=2024/lar_2024.parquet on 2026-05-08.
# Excludes Hive partition `year` (auto-derived from path); includes
# `dataset_year` (written by the transform) at the end. Order matches the
# modern Parquet for downstream visual diffing.
# ---------------------------------------------------------------------------

MODERN_COLUMNS: tuple[str, ...] = (
    "activity_year",
    "lei",
    "derived_msa_md",
    "state_code",
    "county_code",
    "census_tract",
    "conforming_loan_limit",
    "derived_loan_product_type",
    "derived_dwelling_category",
    "derived_ethnicity",
    "derived_race",
    "derived_sex",
    "action_taken",
    "purchaser_type",
    "preapproval",
    "loan_type",
    "loan_purpose",
    "lien_status",
    "reverse_mortgage",
    "open_end_line_of_credit",
    "business_or_commercial_purpose",
    "loan_amount",
    "combined_loan_to_value_ratio",
    "interest_rate",
    "rate_spread",
    "hoepa_status",
    "total_loan_costs",
    "total_points_and_fees",
    "origination_charges",
    "discount_points",
    "lender_credits",
    "loan_term",
    "prepayment_penalty_term",
    "intro_rate_period",
    "negative_amortization",
    "interest_only_payment",
    "balloon_payment",
    "other_nonamortizing_features",
    "property_value",
    "construction_method",
    "occupancy_type",
    "manufactured_home_secured_property_type",
    "manufactured_home_land_property_interest",
    "total_units",
    "multifamily_affordable_units",
    "income",
    "debt_to_income_ratio",
    "applicant_credit_score_type",
    "co_applicant_credit_score_type",
    "applicant_ethnicity_1",
    "applicant_ethnicity_2",
    "applicant_ethnicity_3",
    "applicant_ethnicity_4",
    "applicant_ethnicity_5",
    "co_applicant_ethnicity_1",
    "co_applicant_ethnicity_2",
    "co_applicant_ethnicity_3",
    "co_applicant_ethnicity_4",
    "co_applicant_ethnicity_5",
    "applicant_ethnicity_observed",
    "co_applicant_ethnicity_observed",
    "applicant_race_1",
    "applicant_race_2",
    "applicant_race_3",
    "applicant_race_4",
    "applicant_race_5",
    "co_applicant_race_1",
    "co_applicant_race_2",
    "co_applicant_race_3",
    "co_applicant_race_4",
    "co_applicant_race_5",
    "applicant_race_observed",
    "co_applicant_race_observed",
    "applicant_sex",
    "co_applicant_sex",
    "applicant_sex_observed",
    "co_applicant_sex_observed",
    "applicant_age",
    "co_applicant_age",
    "applicant_age_above_62",
    "co_applicant_age_above_62",
    "submission_of_application",
    "initially_payable_to_institution",
    "aus_1",
    "aus_2",
    "aus_3",
    "aus_4",
    "aus_5",
    "denial_reason_1",
    "denial_reason_2",
    "denial_reason_3",
    "denial_reason_4",
    "tract_population",
    "tract_minority_population_percent",
    "ffiec_msa_md_median_family_income",
    "tract_to_msa_income_percentage",
    "tract_owner_occupied_units",
    "tract_one_to_four_family_homes",
    "tract_median_age_of_housing_units",
)

# Modern columns that are DOUBLE in the modern Parquet — must be
# TRY_CAST(... AS DOUBLE) at projection time.
MODERN_NUMERIC_COLUMNS: frozenset[str] = frozenset({
    "loan_amount",
    "combined_loan_to_value_ratio",
    "interest_rate",
    "rate_spread",
    "total_loan_costs",
    "total_points_and_fees",
    "origination_charges",
    "discount_points",
    "lender_credits",
    "loan_term",
    "prepayment_penalty_term",
    "intro_rate_period",
    "property_value",
    "total_units",
    "multifamily_affordable_units",
    "income",
    "tract_population",
    "tract_minority_population_percent",
    "ffiec_msa_md_median_family_income",
    "tract_to_msa_income_percentage",
    "tract_owner_occupied_units",
    "tract_one_to_four_family_homes",
    "tract_median_age_of_housing_units",
})


# ---------------------------------------------------------------------------
# Legacy → modern projection.
#
# (legacy_col, mode) per modern column. mode ∈ {'copy', 'x1000', 'null'}.
# ---------------------------------------------------------------------------

# Sentinel for "no legacy source — NULL-fill"
_NULL: tuple[None, str] = (None, "null")


LEGACY_TO_MODERN: dict[str, tuple[str | None, str]] = {
    "activity_year":                            ("as_of_year",              "copy"),
    "lei":                                      _NULL,  # no LEI before 2018; respondent_id preserved as legacy_*
    "derived_msa_md":                           ("msamd",                   "copy"),
    "state_code":                               ("state_code",              "copy"),
    "county_code":                              ("county_code",             "copy"),
    "census_tract":                             ("census_tract_number",     "copy"),
    "conforming_loan_limit":                    _NULL,
    "derived_loan_product_type":                _NULL,
    "derived_dwelling_category":                _NULL,
    "derived_ethnicity":                        _NULL,
    "derived_race":                             _NULL,
    "derived_sex":                              _NULL,
    "action_taken":                             ("action_taken",            "copy"),
    "purchaser_type":                           ("purchaser_type",          "copy"),
    "preapproval":                              ("preapproval",             "copy"),
    "loan_type":                                ("loan_type",               "copy"),
    "loan_purpose":                             ("loan_purpose",            "copy"),
    "lien_status":                              ("lien_status",             "copy"),
    "reverse_mortgage":                         _NULL,
    "open_end_line_of_credit":                  _NULL,
    "business_or_commercial_purpose":           _NULL,
    "loan_amount":                              ("loan_amount_000s",        "x1000"),
    "combined_loan_to_value_ratio":             _NULL,
    "interest_rate":                            _NULL,
    "rate_spread":                              ("rate_spread",             "copy"),
    "hoepa_status":                             ("hoepa_status",            "copy"),
    "total_loan_costs":                         _NULL,
    "total_points_and_fees":                    _NULL,
    "origination_charges":                      _NULL,
    "discount_points":                          _NULL,
    "lender_credits":                           _NULL,
    "loan_term":                                _NULL,
    "prepayment_penalty_term":                  _NULL,
    "intro_rate_period":                        _NULL,
    "negative_amortization":                    _NULL,
    "interest_only_payment":                    _NULL,
    "balloon_payment":                          _NULL,
    "other_nonamortizing_features":             _NULL,
    "property_value":                           _NULL,
    "construction_method":                      _NULL,
    "occupancy_type":                           ("owner_occupancy",         "copy"),
    "manufactured_home_secured_property_type":  _NULL,
    "manufactured_home_land_property_interest": _NULL,
    "total_units":                              _NULL,
    "multifamily_affordable_units":             _NULL,
    "income":                                   ("applicant_income_000s",   "x1000"),
    "debt_to_income_ratio":                     _NULL,
    "applicant_credit_score_type":              _NULL,
    "co_applicant_credit_score_type":           _NULL,
    "applicant_ethnicity_1":                    ("applicant_ethnicity",     "copy"),
    "applicant_ethnicity_2":                    _NULL,
    "applicant_ethnicity_3":                    _NULL,
    "applicant_ethnicity_4":                    _NULL,
    "applicant_ethnicity_5":                    _NULL,
    "co_applicant_ethnicity_1":                 ("co_applicant_ethnicity",  "copy"),
    "co_applicant_ethnicity_2":                 _NULL,
    "co_applicant_ethnicity_3":                 _NULL,
    "co_applicant_ethnicity_4":                 _NULL,
    "co_applicant_ethnicity_5":                 _NULL,
    "applicant_ethnicity_observed":             _NULL,
    "co_applicant_ethnicity_observed":          _NULL,
    "applicant_race_1":                         ("applicant_race_1",        "copy"),
    "applicant_race_2":                         ("applicant_race_2",        "copy"),
    "applicant_race_3":                         ("applicant_race_3",        "copy"),
    "applicant_race_4":                         ("applicant_race_4",        "copy"),
    "applicant_race_5":                         ("applicant_race_5",        "copy"),
    "co_applicant_race_1":                      ("co_applicant_race_1",     "copy"),
    "co_applicant_race_2":                      ("co_applicant_race_2",     "copy"),
    "co_applicant_race_3":                      ("co_applicant_race_3",     "copy"),
    "co_applicant_race_4":                      ("co_applicant_race_4",     "copy"),
    "co_applicant_race_5":                      ("co_applicant_race_5",     "copy"),
    "applicant_race_observed":                  _NULL,
    "co_applicant_race_observed":               _NULL,
    "applicant_sex":                            ("applicant_sex",           "copy"),
    "co_applicant_sex":                         ("co_applicant_sex",        "copy"),
    "applicant_sex_observed":                   _NULL,
    "co_applicant_sex_observed":                _NULL,
    "applicant_age":                            _NULL,
    "co_applicant_age":                         _NULL,
    "applicant_age_above_62":                   _NULL,
    "co_applicant_age_above_62":                _NULL,
    "submission_of_application":                _NULL,
    "initially_payable_to_institution":         _NULL,
    "aus_1":                                    _NULL,
    "aus_2":                                    _NULL,
    "aus_3":                                    _NULL,
    "aus_4":                                    _NULL,
    "aus_5":                                    _NULL,
    "denial_reason_1":                          ("denial_reason_1",         "copy"),
    "denial_reason_2":                          ("denial_reason_2",         "copy"),
    "denial_reason_3":                          ("denial_reason_3",         "copy"),
    "denial_reason_4":                          _NULL,  # only 4-slot in modern
    "tract_population":                         ("population",              "copy"),
    "tract_minority_population_percent":        ("minority_population",     "copy"),
    "ffiec_msa_md_median_family_income":        ("hud_median_family_income", "copy"),
    "tract_to_msa_income_percentage":           ("tract_to_msamd_income",   "copy"),
    "tract_owner_occupied_units":               ("number_of_owner_occupied_units", "copy"),
    "tract_one_to_four_family_homes":           ("number_of_1_to_4_family_units",  "copy"),
    "tract_median_age_of_housing_units":        _NULL,
}

assert set(LEGACY_TO_MODERN.keys()) == set(MODERN_COLUMNS), (
    "LEGACY_TO_MODERN must declare exactly the modern columns"
)


# Legacy-only fields preserved as `legacy_*` columns. Respondent ID is the
# pre-LEI lender identifier — joinable against FFIEC Panel for the same
# institution-grain entity resolution lei powers in modern data.
LEGACY_ONLY_PRESERVED: tuple[tuple[str, str], ...] = (
    ("respondent_id",              "legacy_respondent_id"),
    ("agency_code",                "legacy_agency_code"),
    ("property_type",              "legacy_property_type"),
    ("sequence_number",            "legacy_sequence_number"),
    ("edit_status",                "legacy_edit_status"),
    ("application_date_indicator", "legacy_application_date_indicator"),
)


def build_select_clauses(
    csv_columns: list[str],
    *,
    year: int,
) -> tuple[list[str], list[str]]:
    """Build SQL SELECT projection from legacy CSV columns to modern schema.

    Returns (select_clauses, missing_legacy_columns):
    - select_clauses: list of SQL fragments like 'TRY_CAST("..." AS DOUBLE) AS "..."'
      one per modern column + one per legacy_* preserved column + `dataset_year`.
    - missing_legacy_columns: legacy source columns referenced by the map but
      not present in the CSV (informational; surfaces upstream schema drift).
    """
    csv_set = set(csv_columns)
    select_parts: list[str] = []
    missing: list[str] = []

    for modern_col in MODERN_COLUMNS:
        legacy_src, mode = LEGACY_TO_MODERN[modern_col]
        if mode == "null":
            null_type = "DOUBLE" if modern_col in MODERN_NUMERIC_COLUMNS else "VARCHAR"
            select_parts.append(f'CAST(NULL AS {null_type}) AS "{modern_col}"')
            continue

        assert legacy_src is not None  # for the type checker
        if legacy_src not in csv_set:
            missing.append(legacy_src)
            null_type = "DOUBLE" if modern_col in MODERN_NUMERIC_COLUMNS else "VARCHAR"
            select_parts.append(f'CAST(NULL AS {null_type}) AS "{modern_col}"')
            continue

        if mode == "x1000":
            # 000s units → raw dollars; TRY_CAST DOUBLE then multiply.
            select_parts.append(
                f'TRY_CAST("{legacy_src}" AS DOUBLE) * 1000 AS "{modern_col}"'
            )
        elif mode == "copy":
            if modern_col in MODERN_NUMERIC_COLUMNS:
                select_parts.append(
                    f'TRY_CAST("{legacy_src}" AS DOUBLE) AS "{modern_col}"'
                )
            else:
                select_parts.append(f'"{legacy_src}" AS "{modern_col}"')
        else:
            raise ValueError(f"unknown mode {mode!r} for {modern_col}")

    for legacy_src, dst_col in LEGACY_ONLY_PRESERVED:
        if legacy_src in csv_set:
            select_parts.append(f'"{legacy_src}" AS "{dst_col}"')
        else:
            missing.append(legacy_src)
            select_parts.append(f'CAST(NULL AS VARCHAR) AS "{dst_col}"')

    select_parts.append(f"CAST({year} AS SMALLINT) AS dataset_year")
    return select_parts, missing


def coverage_report(csv_columns: list[str]) -> dict[str, int | float]:
    """Return per-projection counts for surfacing in the ingest log."""
    csv_set = set(csv_columns)
    populated = 0
    null_filled = 0
    missing_in_csv = 0
    for modern_col in MODERN_COLUMNS:
        legacy_src, mode = LEGACY_TO_MODERN[modern_col]
        if mode == "null":
            null_filled += 1
        elif legacy_src not in csv_set:
            missing_in_csv += 1
        else:
            populated += 1
    legacy_preserved_present = sum(
        1 for src, _ in LEGACY_ONLY_PRESERVED if src in csv_set
    )
    return {
        "modern_cols_total":         len(MODERN_COLUMNS),
        "modern_cols_populated":     populated,
        "modern_cols_null_by_design": null_filled,
        "modern_cols_missing_in_csv": missing_in_csv,
        "legacy_only_present":       legacy_preserved_present,
        "legacy_only_total":         len(LEGACY_ONLY_PRESERVED),
        "populated_pct": round(
            100.0 * populated / len(MODERN_COLUMNS), 1
        ),
    }
