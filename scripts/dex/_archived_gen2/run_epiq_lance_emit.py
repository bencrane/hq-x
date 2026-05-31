#!/usr/bin/env python3
"""Epiq11 R2 Parquet → Lance emit (Pattern A canonical).

For each of the three surfaces (cases / claims / dockets) reads the latest
parquet objects from R2 via DuckDB httpfs, dedupes by primary key (most
recent `ingested_at` wins), projects to a typed schema, and writes a Lance
dataset with BTREE scalar indexes on the load-bearing resolution keys.

Lance URIs:
    s3://dex-raw-landing-zone/polaris-warehouse/epiq/cases_lance
    s3://dex-raw-landing-zone/polaris-warehouse/epiq/claims_lance
    s3://dex-raw-landing-zone/polaris-warehouse/epiq/dockets_lance

Each commit is wrapped in `lance_commit_lock(slug)` for cross-writer safety.

Doppler-injected env required:
    R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY
    DEX_DB_URL_DIRECT (for the advisory lock)

Usage:

    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_lance_emit.py cases [--apply]

    # All three at once:
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_epiq_lance_emit.py all --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("epiq-lance-emit")

LANCE_BASE = "s3://dex-raw-landing-zone/polaris-warehouse/epiq"
R2_PARQUET_BASE = "s3://dex-raw-landing-zone/epiq"
TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='16GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE s3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{os.environ["R2_ENDPOINT"].replace("https://", "")}',
            REGION 'us-east-1',
            URL_STYLE 'path'
        )
        """
    )
    return con


# --------------------------------------------------------------------------- #
# Per-surface projections
# --------------------------------------------------------------------------- #


# cases: one row per Epiq case (the universe / index).
# Primary key for dedupe is the canon (URL slug — what claims/dockets use
# as projectCode). For dates the API only returns the display string; we
# also try a TRY_STRPTIME parse for filed_date so queries can range-scan it.
_CASES_SELECT = f"""
SELECT
    canon                                                        AS project_code,
    projectCode                                                  AS epiq_short_code,
    CAST(projectId AS BIGINT)                                    AS project_id,
    caseName                                                     AS case_name,
    caseNumber                                                   AS case_number,
    industry,
    jurisdiction,
    judge,
    dbSource                                                     AS db_source,
    CAST(active AS BOOLEAN)                                      AS active,
    CAST(isBankruptcy AS BOOLEAN)                                AS is_bankruptcy,
    CAST(isReceivership AS BOOLEAN)                              AS is_receivership,
    CAST(isAccessible AS BOOLEAN)                                AS is_accessible,
    filedDateDisplay                                             AS filed_date_display,
    TRY_STRPTIME(filedDateDisplay, '%b %d %Y')::DATE             AS filed_date,
    projectUrl                                                   AS project_url,
    projectHomePage                                              AS project_home_page,
    logoImagePath                                                AS logo_image_path,
    logoUrl                                                      AS logo_url,
    projectSiteHeaderText                                        AS project_site_header_text,
    projectSiteHeaderPhone                                       AS project_site_header_phone,
    inquiryContacts                                              AS inquiry_contacts,
    overviewDescription                                          AS overview_description,
    docketsDescription                                           AS dockets_description,
    claimsDescription                                            AS claims_description,
    keyDocumentsDescription                                      AS key_documents_description,
    advProceedingsDescription                                    AS adv_proceedings_description,
    keyDates                                                     AS key_dates_json,
    translations                                                 AS translations_json,
    raw_source_row,
    source_run_id,
    ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY canon ORDER BY ingested_at DESC) AS _rn
    FROM read_parquet('{R2_PARQUET_BASE}/cases/**/*.parquet.zst', union_by_name=true)
) WHERE _rn = 1
"""

# claims: one row per filed claim or scheduled liability per case.
# Primary key for dedupe is (project_code, id) where `id` is the upstream
# composite e.g. 'Stellar-C41670'. We preserve the verbatim API row in
# raw_source_row so every URL/document reference survives.
_CLAIMS_SELECT = f"""
SELECT
    project_code,
    id                                                           AS source_notice_id,
    CAST(claimId AS BIGINT)                                      AS claim_id,
    claimNumber                                                  AS claim_number,
    claimNumberSorting                                           AS claim_number_sorting,
    CAST(scheduleId AS BIGINT)                                   AS schedule_id,
    scheduleNumber                                               AS schedule_number,
    scheduleNumberDisplay                                        AS schedule_number_display,
    searchType                                                   AS search_type,
    caseName                                                     AS case_name,
    caseNumber                                                   AS case_number,
    debtorId                                                     AS debtor_id,
    debtorName                                                   AS debtor_name,
    creditorName                                                 AS creditor_name,
    CAST(redactCreditorName AS BOOLEAN)                          AS redact_creditor_name,
    CAST(redactCreditorAddress AS BOOLEAN)                       AS redact_creditor_address,
    creditorAddressList                                          AS creditor_address_list_json,
    filedDateDisplay                                             AS filed_date_display,
    TRY_STRPTIME(filedDateDisplay, '%b %d %Y')::DATE             AS filed_date,
    valueDisplay                                                 AS value_display,
    amountList                                                   AS amount_list_json,
    documentUrls                                                 AS document_urls,
    imageDocumentId                                              AS image_document_id,
    redactedDocumentId                                           AS redacted_document_id,
    CAST(suppressImage AS BOOLEAN)                               AS suppress_image,
    CAST(liabilityId AS BIGINT)                                  AS liability_id,
    CAST(scheduleG AS BOOLEAN)                                   AS schedule_g,
    dockets                                                      AS dockets_json,
    docketNumbers                                                AS docket_numbers_json,
    remarks,
    valuesList                                                   AS values_list_json,
    imageText                                                    AS image_text,
    redactedImageText                                            AS redacted_image_text,
    suppressedImageText                                          AS suppressed_image_text,
    CAST(isDetailMarked AS BOOLEAN)                              AS is_detail_marked,
    CAST(isAccessible AS BOOLEAN)                                AS is_accessible,
    dbSource                                                     AS db_source,
    CAST(projectId AS BIGINT)                                    AS epiq_project_id,
    projectCode                                                  AS epiq_short_code,
    projectHomePage                                              AS project_home_page,
    raw_source_row,
    source_run_id,
    ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY project_code, id ORDER BY ingested_at DESC) AS _rn
    FROM read_parquet('{R2_PARQUET_BASE}/claims/**/*.parquet.zst', union_by_name=true)
) WHERE _rn = 1
"""

# dockets: one row per docket entry per case. PK (project_code, id).
# docket_documents preserves the document-attachment list verbatim (jsonb-as-text);
# each element carries documentId etc. resolvable to a PDF download URL.
_DOCKETS_SELECT = f"""
SELECT
    project_code,
    id                                                           AS source_notice_id,
    CAST(docketId AS BIGINT)                                     AS docket_id,
    docketNumber                                                 AS docket_number,
    docketNumberSorting                                          AS docket_number_sorting,
    TRY_CAST(docketFiledDate AS TIMESTAMP)                       AS docket_filed_date,
    docketFiledDateDisplay                                       AS docket_filed_date_display,
    docketName                                                   AS docket_name,
    docketText                                                   AS docket_text,
    relatedDocketsNumbers                                        AS related_dockets_numbers_json,
    CAST(isAdversaryProceeding AS BOOLEAN)                       AS is_adversary_proceeding,
    CAST(isProjectActive AS BOOLEAN)                             AS is_project_active,
    CAST(isAccessible AS BOOLEAN)                                AS is_accessible,
    caseName                                                     AS case_name,
    jurisdictionName                                             AS jurisdiction_name,
    debtorId                                                     AS debtor_id,
    debtorName                                                   AS debtor_name,
    debtorNumber                                                 AS case_number,
    docketDocuments                                              AS docket_documents_json,
    dbSource                                                     AS db_source,
    CAST(projectId AS BIGINT)                                    AS epiq_project_id,
    projectCode                                                  AS epiq_short_code,
    projectHomePage                                              AS project_home_page,
    webMenu                                                      AS web_menu,
    raw_source_row,
    source_run_id,
    ingested_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY project_code, id ORDER BY ingested_at DESC) AS _rn
    FROM read_parquet('{R2_PARQUET_BASE}/dockets/**/*.parquet.zst', union_by_name=true)
) WHERE _rn = 1
"""


_SURFACES = {
    "cases": {
        "select": _CASES_SELECT,
        "lance_uri": f"{LANCE_BASE}/cases_lance",
        "slug": "epiq_cases_lance",
        "btree": ["project_code", "case_number", "industry"],
        "glob": f"{R2_PARQUET_BASE}/cases/**/*.parquet.zst",
    },
    "claims": {
        "select": _CLAIMS_SELECT,
        "lance_uri": f"{LANCE_BASE}/claims_lance",
        "slug": "epiq_claims_lance",
        "btree": ["project_code", "claim_id", "creditor_name"],
        "glob": f"{R2_PARQUET_BASE}/claims/**/*.parquet.zst",
    },
    "dockets": {
        "select": _DOCKETS_SELECT,
        "lance_uri": f"{LANCE_BASE}/dockets_lance",
        "slug": "epiq_dockets_lance",
        "btree": ["project_code", "docket_id", "case_number"],
        "glob": f"{R2_PARQUET_BASE}/dockets/**/*.parquet.zst",
    },
}


def _emit_one(surface: str, *, apply: bool) -> int:
    cfg = _SURFACES[surface]
    log.info(
        "EMIT surface=%s lance_uri=%s slug=%s btree=%s apply=%s",
        surface, cfg["lance_uri"], cfg["slug"], cfg["btree"], apply,
    )

    con = _duckdb_conn()

    rowcount = con.execute(
        f"SELECT count(*) FROM read_parquet('{cfg['glob']}', union_by_name=true)"
    ).fetchone()[0]
    log.info("source parquet rows (pre-dedupe): %d", rowcount)
    if rowcount == 0:
        log.error("no rows in source glob — aborting"); return 1

    deduped_rowcount = con.execute(
        f"SELECT count(*) FROM ({cfg['select']}) t"
    ).fetchone()[0]
    log.info("deduped rows (post latest-per-key): %d", deduped_rowcount)

    if not apply:
        log.info("DRY-RUN: would write %d rows to %s — pass --apply", deduped_rowcount, cfg["lance_uri"])
        return 0

    import lance
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    reader = con.execute(cfg["select"]).fetch_record_batch(rows_per_batch=50_000)

    storage_options = _storage_options()

    with lance_commit_lock(cfg["slug"]):
        log.info("writing Lance dataset (mode=overwrite) ...")
        ds = lance.write_dataset(
            reader,
            cfg["lance_uri"],
            mode="overwrite",
            storage_options=storage_options,
        )
        lance_rows = ds.count_rows()
        log.info("Lance written: %d rows version=%s", lance_rows, ds.version)

        for col in cfg["btree"]:
            log.info("creating BTREE on %s ...", col)
            try:
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("BTREE on %s failed (non-fatal): %s", col, exc)

        try:
            ds.optimize.compact_files()
        except Exception as exc:  # noqa: BLE001
            log.warning("compact_files failed (non-fatal): %s", exc)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:  # noqa: BLE001
            log.warning("cleanup_old_versions failed (non-fatal): %s", exc)

    log.info("OK surface=%s lance_rows=%d uri=%s", surface, lance_rows, cfg["lance_uri"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "surface", choices=["cases", "claims", "dockets", "all"],
        help="Which surface to emit; 'all' emits cases→claims→dockets in order.",
    )
    ap.add_argument("--apply", action="store_true", default=False)
    args = ap.parse_args()

    if args.surface == "all":
        for s in ("cases", "claims", "dockets"):
            rc = _emit_one(s, apply=args.apply)
            if rc != 0:
                return rc
        return 0
    return _emit_one(args.surface, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
