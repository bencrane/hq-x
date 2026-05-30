"""SEC EDGAR DERA Form 345 (Form 3 / 4 / 5) bulk-extract pinned schemas.

Source landing page:
  https://www.sec.gov/dera/data/insider-transactions-data-sets.html
ZIP URL pattern (verified 2026-05-09 against quarters 2006-Q1 .. 2026-Q1):
  https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/{YYYY}q{N}_form345.zip

Each quarterly ZIP contains 8 TSVs + 2 metadata files (json/htm). Column
sets are stable across the 2006-Q1 .. 2026-Q1 window per probe on
2026-05-09 *with one exception:* SUBMISSION grew from 13 to 14 columns
post-2022 — `AFF10B5ONE` was added when SEC required Section 16 Rule
10b5-1 trading-plan disclosure.

The script logs schema drift relative to these pinned lists and records
it in the audit ledger's ``notes`` column. Drift does not fail the run;
the pinned list gets updated in a follow-up. All TSVs are tab-delimited
with a single header row, UTF-8 encoded.
"""

from __future__ import annotations

# ---------------------------------------------------------------------- #
# Pinned column lists (modern era — 2024+ shape).
# ---------------------------------------------------------------------- #

SUBMISSION_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "FILING_DATE",
    "PERIOD_OF_REPORT",
    "DATE_OF_ORIG_SUB",
    "NO_SECURITIES_OWNED",
    "NOT_SUBJECT_SEC16",
    "FORM3_HOLDINGS_REPORTED",
    "FORM4_TRANS_REPORTED",
    "DOCUMENT_TYPE",
    "ISSUERCIK",
    "ISSUERNAME",
    "ISSUERTRADINGSYMBOL",
    "REMARKS",
    "AFF10B5ONE",
)

REPORTING_OWNER_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "RPTOWNERCIK",
    "RPTOWNERNAME",
    "RPTOWNER_RELATIONSHIP",
    "RPTOWNER_TITLE",
    "RPTOWNER_TXT",
    "RPTOWNER_STREET1",
    "RPTOWNER_STREET2",
    "RPTOWNER_CITY",
    "RPTOWNER_STATE",
    "RPTOWNER_ZIPCODE",
    "RPTOWNER_STATE_DESC",
    "FILE_NUMBER",
)

NON_DERIV_TRANS_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "NONDERIV_TRANS_SK",
    "SECURITY_TITLE",
    "SECURITY_TITLE_FN",
    "TRANS_DATE",
    "TRANS_DATE_FN",
    "DEEMED_EXECUTION_DATE",
    "DEEMED_EXECUTION_DATE_FN",
    "TRANS_FORM_TYPE",
    "TRANS_CODE",
    "EQUITY_SWAP_INVOLVED",
    "EQUITY_SWAP_TRANS_CD_FN",
    "TRANS_TIMELINESS",
    "TRANS_TIMELINESS_FN",
    "TRANS_SHARES",
    "TRANS_SHARES_FN",
    "TRANS_PRICEPERSHARE",
    "TRANS_PRICEPERSHARE_FN",
    "TRANS_ACQUIRED_DISP_CD",
    "TRANS_ACQUIRED_DISP_CD_FN",
    "SHRS_OWND_FOLWNG_TRANS",
    "SHRS_OWND_FOLWNG_TRANS_FN",
    "VALU_OWND_FOLWNG_TRANS",
    "VALU_OWND_FOLWNG_TRANS_FN",
    "DIRECT_INDIRECT_OWNERSHIP",
    "DIRECT_INDIRECT_OWNERSHIP_FN",
    "NATURE_OF_OWNERSHIP",
    "NATURE_OF_OWNERSHIP_FN",
)

NON_DERIV_HOLDING_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "NONDERIV_HOLDING_SK",
    "SECURITY_TITLE",
    "SECURITY_TITLE_FN",
    "TRANS_FORM_TYPE",
    "TRANS_FORM_TYPE_FN",
    "SHRS_OWND_FOLWNG_TRANS",
    "SHRS_OWND_FOLWNG_TRANS_FN",
    "VALU_OWND_FOLWNG_TRANS",
    "VALU_OWND_FOLWNG_TRANS_FN",
    "DIRECT_INDIRECT_OWNERSHIP",
    "DIRECT_INDIRECT_OWNERSHIP_FN",
    "NATURE_OF_OWNERSHIP",
    "NATURE_OF_OWNERSHIP_FN",
)

DERIV_TRANS_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "DERIV_TRANS_SK",
    "SECURITY_TITLE",
    "SECURITY_TITLE_FN",
    "CONV_EXERCISE_PRICE",
    "CONV_EXERCISE_PRICE_FN",
    "TRANS_DATE",
    "TRANS_DATE_FN",
    "DEEMED_EXECUTION_DATE",
    "DEEMED_EXECUTION_DATE_FN",
    "TRANS_FORM_TYPE",
    "TRANS_CODE",
    "EQUITY_SWAP_INVOLVED",
    "EQUITY_SWAP_TRANS_CD_FN",
    "TRANS_TIMELINESS",
    "TRANS_TIMELINESS_FN",
    "TRANS_SHARES",
    "TRANS_SHARES_FN",
    "TRANS_TOTAL_VALUE",
    "TRANS_TOTAL_VALUE_FN",
    "TRANS_PRICEPERSHARE",
    "TRANS_PRICEPERSHARE_FN",
    "TRANS_ACQUIRED_DISP_CD",
    "TRANS_ACQUIRED_DISP_CD_FN",
    "EXCERCISE_DATE",
    "EXCERCISE_DATE_FN",
    "EXPIRATION_DATE",
    "EXPIRATION_DATE_FN",
    "UNDLYNG_SEC_TITLE",
    "UNDLYNG_SEC_TITLE_FN",
    "UNDLYNG_SEC_SHARES",
    "UNDLYNG_SEC_SHARES_FN",
    "UNDLYNG_SEC_VALUE",
    "UNDLYNG_SEC_VALUE_FN",
    "SHRS_OWND_FOLWNG_TRANS",
    "SHRS_OWND_FOLWNG_TRANS_FN",
    "VALU_OWND_FOLWNG_TRANS",
    "VALU_OWND_FOLWNG_TRANS_FN",
    "DIRECT_INDIRECT_OWNERSHIP",
    "DIRECT_INDIRECT_OWNERSHIP_FN",
    "NATURE_OF_OWNERSHIP",
    "NATURE_OF_OWNERSHIP_FN",
)

DERIV_HOLDING_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "DERIV_HOLDING_SK",
    "SECURITY_TITLE",
    "SECURITY_TITLE_FN",
    "CONV_EXERCISE_PRICE",
    "CONV_EXERCISE_PRICE_FN",
    "TRANS_FORM_TYPE",
    "TRANS_FORM_TYPE_FN",
    "EXERCISE_DATE",
    "EXERCISE_DATE_FN",
    "EXPIRATION_DATE",
    "EXPIRATION_DATE_FN",
    "UNDLYNG_SEC_TITLE",
    "UNDLYNG_SEC_TITLE_FN",
    "UNDLYNG_SEC_SHARES",
    "UNDLYNG_SEC_SHARES_FN",
    "UNDLYNG_SEC_VALUE",
    "UNDLYNG_SEC_VALUE_FN",
    "SHRS_OWND_FOLWNG_TRANS",
    "SHRS_OWND_FOLWNG_TRANS_FN",
    "VALU_OWND_FOLWNG_TRANS",
    "VALU_OWND_FOLWNG_TRANS_FN",
    "DIRECT_INDIRECT_OWNERSHIP",
    "DIRECT_INDIRECT_OWNERSHIP_FN",
    "NATURE_OF_OWNERSHIP",
    "NATURE_OF_OWNERSHIP_FN",
)

FOOTNOTES_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "FOOTNOTE_ID",
    "FOOTNOTE_TXT",
)

OWNER_SIGNATURE_COLUMNS: tuple[str, ...] = (
    "ACCESSION_NUMBER",
    "OWNERSIGNATURENAME",
    "OWNERSIGNATUREDATE",
)

# ---------------------------------------------------------------------- #
# Table registry
# ---------------------------------------------------------------------- #

# (logical_table_name, tsv_filename, pinned_columns)
TABLES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("submission",        "SUBMISSION.tsv",        SUBMISSION_COLUMNS),
    ("reporting_owner",   "REPORTINGOWNER.tsv",    REPORTING_OWNER_COLUMNS),
    ("non_deriv_trans",   "NONDERIV_TRANS.tsv",    NON_DERIV_TRANS_COLUMNS),
    ("non_deriv_holding", "NONDERIV_HOLDING.tsv",  NON_DERIV_HOLDING_COLUMNS),
    ("deriv_trans",       "DERIV_TRANS.tsv",       DERIV_TRANS_COLUMNS),
    ("deriv_holding",     "DERIV_HOLDING.tsv",     DERIV_HOLDING_COLUMNS),
    ("footnotes",         "FOOTNOTES.tsv",         FOOTNOTES_COLUMNS),
    ("owner_signature",   "OWNER_SIGNATURE.tsv",   OWNER_SIGNATURE_COLUMNS),
)

LOGICAL_TABLE_NAMES: tuple[str, ...] = tuple(t[0] for t in TABLES)


def diff_columns(
    pinned: tuple[str, ...], observed: tuple[str, ...],
) -> dict[str, list[str]]:
    """Return per-column-set drift between pinned and observed.

    Used at ingest time to record schema-drift in the audit-ledger
    ``notes`` column. Empty lists mean no drift.
    """
    pinned_set = set(pinned)
    observed_set = set(observed)
    return {
        "missing_from_observed": [c for c in pinned if c not in observed_set],
        "new_in_observed":       [c for c in observed if c not in pinned_set],
        "order_drift":           [] if list(pinned) == list(observed) else list(observed),
    }
