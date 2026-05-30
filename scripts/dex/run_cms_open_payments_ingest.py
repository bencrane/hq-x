#!/usr/bin/env python3
"""CMS Open Payments (Sunshine Act) — streaming CSV ingest to data-engine-x.

Ingests three feeds from the CMS Open Payments program (manufacturer/GPO
payments to physicians and teaching hospitals under the Physician Payments
Sunshine Act) into entities.source_cms_open_payments_{general,research,ownership}.
Source files are discovered at runtime by querying the openpaymentsdata.cms.gov
DKAN metastore API (the legacy download.cms.gov/openpayments/PGYY_*.ZIP pattern
returns 404; current distributions are plain CSV files whose URLs rotate with
each CMS refresh cycle). Files are large (multi-GB for post-2020 General
Payment) and are streamed directly via httpx without local materialisation.
The upsert path (ON CONFLICT (record_id) DO UPDATE) handles CMS quarterly
corrections without duplication. Each ingest attempt is recorded in
ops.cms_open_payments_ingest_runs.

Usage:
  PYTHONPATH=. doppler run -p hq-all -c prd -- \\
    python3 scripts/run_cms_open_payments_ingest.py general 2024
  PYTHONPATH=. doppler run -p hq-all -c prd -- \\
    python3 scripts/run_cms_open_payments_ingest.py general 2024 --skip-if-unchanged
  PYTHONPATH=. doppler run -p hq-all -c prd -- \\
    python3 scripts/run_cms_open_payments_ingest.py general 2024 --recon-only
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import psycopg


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("cms-op-ingest")


log = _logger()

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

METASTORE_URL = "https://openpaymentsdata.cms.gov/api/1/metastore/schemas/dataset/items"
DEFAULT_BATCH_SIZE = 25_000
MAX_RETRIES = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}

# --------------------------------------------------------------------------- #
# Column lists — order matches migration table definitions exactly.
# record_id (PK) and provenance columns (source_file_last_modified, ingested_at,
# program_year) are managed separately.
# --------------------------------------------------------------------------- #

GENERAL_COLS: list[str] = [
    "change_type",
    "covered_recipient_type",
    "teaching_hospital_ccn",
    "teaching_hospital_id",
    "teaching_hospital_name",
    "covered_recipient_profile_id",
    "covered_recipient_npi",
    "covered_recipient_first_name",
    "covered_recipient_middle_name",
    "covered_recipient_last_name",
    "covered_recipient_name_suffix",
    "recipient_primary_business_street_address_line1",
    "recipient_primary_business_street_address_line2",
    "recipient_city",
    "recipient_state",
    "recipient_zip_code",
    "recipient_country",
    "recipient_province",
    "recipient_postal_code",
    "covered_recipient_primary_type_1",
    "covered_recipient_primary_type_2",
    "covered_recipient_primary_type_3",
    "covered_recipient_primary_type_4",
    "covered_recipient_primary_type_5",
    "covered_recipient_primary_type_6",
    "covered_recipient_specialty_1",
    "covered_recipient_specialty_2",
    "covered_recipient_specialty_3",
    "covered_recipient_specialty_4",
    "covered_recipient_specialty_5",
    "covered_recipient_specialty_6",
    "covered_recipient_license_state_code1",
    "covered_recipient_license_state_code2",
    "covered_recipient_license_state_code3",
    "covered_recipient_license_state_code4",
    "covered_recipient_license_state_code5",
    "submitting_applicable_manufacturer_or_applicable_gpo_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_id",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_state",
    # Truncated by Postgres to 63 chars (identifier limit):
    "applicable_manufacturer_or_applicable_gpo_making_payment_countr",
    "total_amount_of_payment_usdollars",
    "date_of_payment",
    "number_of_payments_included_in_total_amount",
    "form_of_payment_or_transfer_of_value",
    "nature_of_payment_or_transfer_of_value",
    "city_of_travel",
    "state_of_travel",
    "country_of_travel",
    "physician_ownership_indicator",
    "third_party_payment_recipient_indicator",
    # Truncated by Postgres to 63 chars (identifier limit):
    "name_of_third_party_entity_receiving_payment_or_transfer_of_val",
    "charity_indicator",
    "third_party_equals_covered_recipient_indicator",
    "contextual_information",
    "delay_in_publication_indicator",
    "dispute_status_for_publication",
    "related_product_indicator",
    "covered_or_noncovered_indicator_1",
    "indicate_drug_or_biological_or_device_or_medical_supply_1",
    "product_category_or_therapeutic_area_1",
    "name_of_drug_or_biological_or_device_or_medical_supply_1",
    "associated_drug_or_biological_ndc_1",
    "associated_device_or_medical_supply_pdi_1",
    "covered_or_noncovered_indicator_2",
    "indicate_drug_or_biological_or_device_or_medical_supply_2",
    "product_category_or_therapeutic_area_2",
    "name_of_drug_or_biological_or_device_or_medical_supply_2",
    "associated_drug_or_biological_ndc_2",
    "associated_device_or_medical_supply_pdi_2",
    "covered_or_noncovered_indicator_3",
    "indicate_drug_or_biological_or_device_or_medical_supply_3",
    "product_category_or_therapeutic_area_3",
    "name_of_drug_or_biological_or_device_or_medical_supply_3",
    "associated_drug_or_biological_ndc_3",
    "associated_device_or_medical_supply_pdi_3",
    "covered_or_noncovered_indicator_4",
    "indicate_drug_or_biological_or_device_or_medical_supply_4",
    "product_category_or_therapeutic_area_4",
    "name_of_drug_or_biological_or_device_or_medical_supply_4",
    "associated_drug_or_biological_ndc_4",
    "associated_device_or_medical_supply_pdi_4",
    "covered_or_noncovered_indicator_5",
    "indicate_drug_or_biological_or_device_or_medical_supply_5",
    "product_category_or_therapeutic_area_5",
    "name_of_drug_or_biological_or_device_or_medical_supply_5",
    "associated_drug_or_biological_ndc_5",
    "associated_device_or_medical_supply_pdi_5",
    "payment_publication_date",
]

GENERAL_NUMERIC_COLS: set[str] = {
    "total_amount_of_payment_usdollars",
    "number_of_payments_included_in_total_amount",
}
GENERAL_DATE_COLS: set[str] = {"date_of_payment", "payment_publication_date"}

RESEARCH_COLS: list[str] = [
    "change_type",
    "covered_recipient_type",
    "noncovered_recipient_entity_name",
    "teaching_hospital_ccn",
    "teaching_hospital_id",
    "teaching_hospital_name",
    "covered_recipient_profile_id",
    "covered_recipient_npi",
    "covered_recipient_first_name",
    "covered_recipient_middle_name",
    "covered_recipient_last_name",
    "covered_recipient_name_suffix",
    "recipient_primary_business_street_address_line1",
    "recipient_primary_business_street_address_line2",
    "recipient_city",
    "recipient_state",
    "recipient_zip_code",
    "recipient_country",
    "recipient_province",
    "recipient_postal_code",
    "covered_recipient_primary_type_1",
    "covered_recipient_primary_type_2",
    "covered_recipient_primary_type_3",
    "covered_recipient_primary_type_4",
    "covered_recipient_primary_type_5",
    "covered_recipient_primary_type_6",
    "covered_recipient_specialty_1",
    "covered_recipient_specialty_2",
    "covered_recipient_specialty_3",
    "covered_recipient_specialty_4",
    "covered_recipient_specialty_5",
    "covered_recipient_specialty_6",
    "covered_recipient_license_state_code1",
    "covered_recipient_license_state_code2",
    "covered_recipient_license_state_code3",
    "covered_recipient_license_state_code4",
    "covered_recipient_license_state_code5",
    "principal_investigator_1_covered_recipient_type",
    "principal_investigator_1_profile_id",
    "principal_investigator_1_npi",
    "principal_investigator_1_first_name",
    "principal_investigator_1_middle_name",
    "principal_investigator_1_last_name",
    "principal_investigator_1_name_suffix",
    "principal_investigator_1_business_street_address_line1",
    "principal_investigator_1_business_street_address_line2",
    "principal_investigator_1_city",
    "principal_investigator_1_state",
    "principal_investigator_1_zip_code",
    "principal_investigator_1_country",
    "principal_investigator_1_province",
    "principal_investigator_1_postal_code",
    "principal_investigator_1_primary_type_1",
    "principal_investigator_1_primary_type_2",
    "principal_investigator_1_primary_type_3",
    "principal_investigator_1_primary_type_4",
    "principal_investigator_1_primary_type_5",
    "principal_investigator_1_primary_type_6",
    "principal_investigator_1_specialty_1",
    "principal_investigator_1_specialty_2",
    "principal_investigator_1_specialty_3",
    "principal_investigator_1_specialty_4",
    "principal_investigator_1_specialty_5",
    "principal_investigator_1_specialty_6",
    "principal_investigator_1_license_state_code1",
    "principal_investigator_1_license_state_code2",
    "principal_investigator_1_license_state_code3",
    "principal_investigator_1_license_state_code4",
    "principal_investigator_1_license_state_code5",
    "submitting_applicable_manufacturer_or_applicable_gpo_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_id",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_state",
    # Truncated by Postgres to 63 chars (identifier limit):
    "applicable_manufacturer_or_applicable_gpo_making_payment_countr",
    "related_product_indicator",
    "covered_or_noncovered_indicator_1",
    "indicate_drug_or_biological_or_device_or_medical_supply_1",
    "product_category_or_therapeutic_area_1",
    "name_of_drug_or_biological_or_device_or_medical_supply_1",
    "associated_drug_or_biological_ndc_1",
    "associated_device_or_medical_supply_pdi_1",
    "covered_or_noncovered_indicator_2",
    "indicate_drug_or_biological_or_device_or_medical_supply_2",
    "product_category_or_therapeutic_area_2",
    "name_of_drug_or_biological_or_device_or_medical_supply_2",
    "associated_drug_or_biological_ndc_2",
    "associated_device_or_medical_supply_pdi_2",
    "covered_or_noncovered_indicator_3",
    "indicate_drug_or_biological_or_device_or_medical_supply_3",
    "product_category_or_therapeutic_area_3",
    "name_of_drug_or_biological_or_device_or_medical_supply_3",
    "associated_drug_or_biological_ndc_3",
    "associated_device_or_medical_supply_pdi_3",
    "covered_or_noncovered_indicator_4",
    "indicate_drug_or_biological_or_device_or_medical_supply_4",
    "product_category_or_therapeutic_area_4",
    "name_of_drug_or_biological_or_device_or_medical_supply_4",
    "associated_drug_or_biological_ndc_4",
    "associated_device_or_medical_supply_pdi_4",
    "covered_or_noncovered_indicator_5",
    "indicate_drug_or_biological_or_device_or_medical_supply_5",
    "product_category_or_therapeutic_area_5",
    "name_of_drug_or_biological_or_device_or_medical_supply_5",
    "associated_drug_or_biological_ndc_5",
    "associated_device_or_medical_supply_pdi_5",
    "total_amount_of_payment_usdollars",
    "date_of_payment",
    "form_of_payment_or_transfer_of_value",
    "expenditure_category1",
    "expenditure_category2",
    "expenditure_category3",
    "expenditure_category4",
    "expenditure_category5",
    "expenditure_category6",
    "preclinical_research_indicator",
    "delay_in_publication_indicator",
    "name_of_study",
    "dispute_status_for_publication",
    "payment_publication_date",
    "clinicaltrials_gov_identifier",
    "research_information_link",
    "context_of_research",
]

RESEARCH_NUMERIC_COLS: set[str] = {"total_amount_of_payment_usdollars"}
RESEARCH_DATE_COLS: set[str] = {"date_of_payment", "payment_publication_date"}

OWNERSHIP_COLS: list[str] = [
    "change_type",
    "physician_profile_id",
    "physician_npi",
    "physician_first_name",
    "physician_middle_name",
    "physician_last_name",
    "physician_name_suffix",
    "recipient_primary_business_street_address_line1",
    "recipient_primary_business_street_address_line2",
    "recipient_city",
    "recipient_state",
    "recipient_zip_code",
    "recipient_country",
    "recipient_province",
    "recipient_postal_code",
    "physician_primary_type",
    "physician_specialty",
    "total_amount_invested_usdollars",
    "value_of_interest",
    "terms_of_interest",
    "submitting_applicable_manufacturer_or_applicable_gpo_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_id",
    "applicable_manufacturer_or_applicable_gpo_making_payment_name",
    "applicable_manufacturer_or_applicable_gpo_making_payment_state",
    # Truncated by Postgres to 63 chars (identifier limit):
    "applicable_manufacturer_or_applicable_gpo_making_payment_countr",
    "dispute_status_for_publication",
    "interest_held_by_physician_or_an_immediate_family_member",
    "payment_publication_date",
]

OWNERSHIP_NUMERIC_COLS: set[str] = {
    "total_amount_invested_usdollars",
    "value_of_interest",
}
OWNERSHIP_DATE_COLS: set[str] = {"payment_publication_date"}


# --------------------------------------------------------------------------- #
# Per-feed configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class FormConfig:
    feed: str                   # 'general' | 'research' | 'ownership'
    schema: str                 # 'entities'
    table: str                  # 'source_cms_open_payments_<feed>'
    cols: list[str]             # data columns (excludes record_id + provenance)
    numeric_cols: set[str]
    date_cols: set[str]
    pk_cols: tuple[str, ...]    # ('record_id',)
    npi_col: str                # dataset-native NPI column name

    @property
    def fully_qualified(self) -> str:
        return f"{self.schema}.{self.table}"

    @property
    def stage_table(self) -> str:
        return f"_stage_{self.table}"


GENERAL_FORM = FormConfig(
    feed="general",
    schema="entities",
    table="source_cms_open_payments_general",
    cols=GENERAL_COLS,
    numeric_cols=GENERAL_NUMERIC_COLS,
    date_cols=GENERAL_DATE_COLS,
    pk_cols=("record_id",),
    npi_col="covered_recipient_npi",
)

RESEARCH_FORM = FormConfig(
    feed="research",
    schema="entities",
    table="source_cms_open_payments_research",
    cols=RESEARCH_COLS,
    numeric_cols=RESEARCH_NUMERIC_COLS,
    date_cols=RESEARCH_DATE_COLS,
    pk_cols=("record_id",),
    npi_col="covered_recipient_npi",
)

OWNERSHIP_FORM = FormConfig(
    feed="ownership",
    schema="entities",
    table="source_cms_open_payments_ownership",
    cols=OWNERSHIP_COLS,
    numeric_cols=OWNERSHIP_NUMERIC_COLS,
    date_cols=OWNERSHIP_DATE_COLS,
    pk_cols=("record_id",),
    npi_col="physician_npi",
)

FORMS: dict[str, FormConfig] = {
    f.feed: f for f in (GENERAL_FORM, RESEARCH_FORM, OWNERSHIP_FORM)
}


# --------------------------------------------------------------------------- #
# URL resolution via DKAN metastore
# --------------------------------------------------------------------------- #


def resolve_metastore_url(feed: str, program_year: int) -> tuple[str, datetime]:
    """GET the DKAN metastore, filter by title regex, return (downloadURL, modified).

    Title pattern: '^{year} (General|Research|Ownership) Payment Data$'.
    Ownership capitalises as 'Ownership'; General as 'General'; Research as 'Research'.
    The 5 grouped/rollup variants per program-year are intentionally excluded by
    the tight regex — out of scope per directive kickoff.
    """
    feed_title = feed.capitalize()  # general->General, research->Research, ownership->Ownership
    title_re = re.compile(rf"^{program_year} {feed_title} Payment Data$")

    with httpx.Client(
        headers={"User-Agent": "data-engine-x/cms-op-ingest"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        r = client.get(METASTORE_URL, params={"limit": 300})
        r.raise_for_status()
        items = r.json()

    for item in items:
        title = item.get("title", "")
        if title_re.match(title):
            distributions = item.get("distribution", [])
            if not distributions:
                raise RuntimeError(
                    f"Metastore item '{title}' has no distributions"
                )
            dist = distributions[0]
            download_url = dist.get("downloadURL")
            if not download_url:
                raise RuntimeError(
                    f"Metastore item '{title}' distribution[0] has no downloadURL"
                )
            modified_str = item.get("modified", "")
            # modified is a date-only string e.g. "2026-01-27"; promote to UTC timestamptz
            try:
                modified = datetime.fromisoformat(modified_str + "T00:00:00+00:00")
            except (ValueError, TypeError):
                modified = datetime.now(timezone.utc)
            log.info(
                "resolved feed=%s year=%d title=%r url=%s modified=%s",
                feed, program_year, title, download_url, modified_str,
            )
            return download_url, modified

    raise RuntimeError(
        f"No metastore item matched '{program_year} {feed_title} Payment Data' "
        f"(searched {len(items)} datasets)"
    )


# --------------------------------------------------------------------------- #
# DB helpers — ops.cms_open_payments_ingest_runs
# --------------------------------------------------------------------------- #


def insert_run_row(
    conn: psycopg.Connection,
    feed: str,
    program_year: int,
    attempt: int,
    source_url: str,
    source_filename: str | None,
    source_last_modified: datetime | None,
    invoked_by: str | None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a pending run row; return (run_id, row_id)."""
    run_id = uuid.uuid4()
    sql = """
    INSERT INTO ops.cms_open_payments_ingest_runs (
        run_id, feed_name, program_year, attempt,
        source_url, source_filename, source_last_modified,
        status, started_at, invoked_by
    ) VALUES (
        %s, %s, %s, %s,
        %s, %s, %s,
        'pending', now(), %s
    ) RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            str(run_id), feed, program_year, attempt,
            source_url, source_filename, source_last_modified,
            invoked_by,
        ))
        row_id = uuid.UUID(str(cur.fetchone()[0]))
    conn.commit()
    return run_id, row_id


def finalize_run_row(
    conn: psycopg.Connection,
    row_id: uuid.UUID,
    status: str,
    *,
    rows_loaded: int | None = None,
    rows_inserted: int | None = None,
    rows_updated: int | None = None,
    duration_seconds: float | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> None:
    """UPDATE the run row with completion fields."""
    sql = """
    UPDATE ops.cms_open_payments_ingest_runs
       SET status           = %s,
           completed_at     = now(),
           rows_loaded      = COALESCE(%s, rows_loaded),
           rows_inserted    = COALESCE(%s, rows_inserted),
           rows_updated     = COALESCE(%s, rows_updated),
           duration_seconds = COALESCE(%s, duration_seconds),
           error_class      = COALESCE(%s, error_class),
           error_message    = COALESCE(%s, error_message)
     WHERE id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            status,
            rows_loaded, rows_inserted, rows_updated,
            duration_seconds,
            error_class, error_message,
            str(row_id),
        ))
    conn.commit()


def check_skip_if_unchanged(
    conn: psycopg.Connection,
    feed: str,
    program_year: int,
    source_last_modified: datetime,
) -> bool:
    """Return True if a prior completed run already covers this source_last_modified.

    Queries the latest 'completed' run's source_last_modified for (feed, program_year).
    If source_last_modified <= prior, the caller should write a 'no_change' row and exit.
    """
    sql = """
    SELECT source_last_modified
      FROM ops.cms_open_payments_ingest_runs
     WHERE feed_name = %s
       AND program_year = %s
       AND status = 'completed'
     ORDER BY started_at DESC
     LIMIT 1;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (feed, program_year))
        row = cur.fetchone()
    if row is None:
        return False
    prior: datetime | None = row[0]
    if prior is None:
        return False
    if source_last_modified <= prior:
        log.info(
            "skip-if-unchanged: feed=%s year=%d source_modified=%s <= prior=%s",
            feed, program_year, source_last_modified.isoformat(), prior.isoformat(),
        )
        return True
    return False


# --------------------------------------------------------------------------- #
# CSV → Postgres COPY pipeline
# --------------------------------------------------------------------------- #


def _stage_create_sql(cfg: FormConfig) -> str:
    col_defs = ",\n  ".join(
        f"{c} {'numeric' if c in cfg.numeric_cols else 'text'}"
        for c in cfg.cols
    )
    return f"""
CREATE TEMP TABLE IF NOT EXISTS {cfg.stage_table} (
  record_id text,
  program_year smallint,
  {col_defs},
  source_file_last_modified timestamptz
);
"""


def _truncate_stage_sql(cfg: FormConfig) -> str:
    return f"TRUNCATE {cfg.stage_table};"


def _copy_sql(cfg: FormConfig) -> str:
    all_cols = ["record_id", "program_year"] + list(cfg.cols) + ["source_file_last_modified"]
    return f"COPY {cfg.stage_table} ({', '.join(all_cols)}) FROM STDIN"


def _upsert_from_stage_sql(cfg: FormConfig) -> str:
    data_cols = list(cfg.cols)
    target_cols = ["record_id", "program_year"] + data_cols + ["source_file_last_modified", "ingested_at"]
    select_cols = ["record_id", "program_year"] + data_cols + ["source_file_last_modified", "now()"]
    pk = "record_id"
    update_assigns = ",\n      ".join(
        f"{c} = EXCLUDED.{c}" for c in (["program_year"] + data_cols + ["source_file_last_modified"])
    ) + ",\n      ingested_at = now()"
    where_clause = " OR ".join(
        f"{cfg.fully_qualified}.{c} IS DISTINCT FROM EXCLUDED.{c}"
        for c in (["program_year"] + data_cols + ["source_file_last_modified"])
    )
    return f"""
WITH upserted AS (
  INSERT INTO {cfg.fully_qualified} ({', '.join(target_cols)})
  SELECT {', '.join(select_cols)}
    FROM {cfg.stage_table}
   WHERE record_id IS NOT NULL AND record_id <> ''
   ON CONFLICT (record_id) DO UPDATE SET
      {update_assigns}
   WHERE {where_clause}
   RETURNING (xmax = 0) AS inserted
)
SELECT
  count(*) FILTER (WHERE inserted)     AS rows_inserted,
  count(*) FILTER (WHERE NOT inserted) AS rows_updated
FROM upserted;
"""


def _copy_chunk_to_stage(
    conn: psycopg.Connection,
    cfg: FormConfig,
    rows: list[tuple[Any, ...]],
) -> tuple[int, int]:
    """COPY chunk into staging, upsert into target, commit. Returns (inserted, updated)."""
    if not rows:
        return 0, 0
    with conn.cursor() as cur:
        cur.execute(_truncate_stage_sql(cfg))
        with cur.copy(_copy_sql(cfg)) as copy:
            for row in rows:
                copy.write_row(row)
        cur.execute(_upsert_from_stage_sql(cfg))
        ins, upd = cur.fetchone()
    conn.commit()
    return int(ins), int(upd)


def stream_csv_to_db(
    url: str,
    conn: psycopg.Connection,
    cfg: FormConfig,
    program_year: int,
    source_file_last_modified: datetime | None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_rows: int | None = None,
) -> tuple[int, int, int]:
    """Stream CSV from URL, COPY into temp staging, ON CONFLICT (record_id) DO UPDATE.

    CMS CSV headers use Title_Case_With_Underscores (e.g. Covered_Recipient_NPI).
    We lowercase them at read time and map to cfg.cols (lower_snake_case).

    For Ownership feeds in program-years before 2020, the Program_Year column
    may be absent from the CSV. program_year from the CLI arg is always stamped
    into the row regardless of whether the CSV carries it.

    Returns (rows_inserted, rows_updated, rows_seen).
    """
    log_prefix = f"[{cfg.feed} {program_year}]"
    log.info("%s streaming from %s", log_prefix, url)

    # Ensure the temp staging table exists for this connection session
    with conn.cursor() as cur:
        cur.execute(_stage_create_sql(cfg))
    conn.commit()

    total_inserted = total_updated = rows_seen = 0
    chunk: list[tuple[Any, ...]] = []
    page_started = time.monotonic()

    with httpx.stream(
        "GET", url,
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
        headers={"User-Agent": "data-engine-x/cms-op-ingest"},
    ) as r:
        if r.status_code in RETRY_STATUSES:
            raise httpx.HTTPStatusError(
                f"HTTP {r.status_code} streaming {url}",
                request=r.request,
                response=r,
            )
        r.raise_for_status()

        text_wrapper = io.TextIOWrapper(
            r.iter_bytes(chunk_size=1 << 20),  # type: ignore[arg-type]
            encoding="utf-8",
            errors="replace",
            newline="",
        )
        reader = csv.reader(text_wrapper)

        try:
            raw_header = next(reader)
        except StopIteration:
            log.warning("%s CSV appears empty", log_prefix)
            return 0, 0, 0

        # Lower-case the CSV header to match migration column names
        header_lower = [h.strip().lower() for h in raw_header]
        idx_by_name = {name: i for i, name in enumerate(header_lower)}

        record_id_idx = idx_by_name.get("record_id")
        if record_id_idx is None:
            raise RuntimeError(f"{log_prefix} CSV header missing 'record_id' column: {raw_header[:10]}")

        # Build per-column index lookups; None means column absent in this file
        col_indexes = [idx_by_name.get(c.lower()) for c in cfg.cols]

        for raw in reader:
            rows_seen += 1
            if max_rows is not None and rows_seen > max_rows:
                log.info("%s --limit %d reached, stopping read", log_prefix, max_rows)
                break

            if record_id_idx >= len(raw):
                continue
            record_id = raw[record_id_idx].strip() if raw[record_id_idx] is not None else ""
            if not record_id:
                continue

            out: list[Any] = [record_id, program_year]
            for col, idx in zip(cfg.cols, col_indexes):
                if idx is None or idx >= len(raw):
                    out.append(None)
                    continue
                v = raw[idx].strip() if raw[idx] is not None else ""
                if v == "":
                    out.append(None)
                else:
                    out.append(v)
            out.append(source_file_last_modified)
            chunk.append(tuple(out))

            if len(chunk) >= batch_size:
                ins, upd = _copy_chunk_to_stage(conn, cfg, chunk)
                total_inserted += ins
                total_updated += upd
                log.info(
                    "%s chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
                    log_prefix, rows_seen, ins, upd,
                    total_inserted, total_updated,
                    time.monotonic() - page_started,
                )
                chunk.clear()
                page_started = time.monotonic()

    if chunk:
        ins, upd = _copy_chunk_to_stage(conn, cfg, chunk)
        total_inserted += ins
        total_updated += upd
        log.info(
            "%s final chunk: rows_seen=%d ins=%d upd=%d (cum ins=%d upd=%d) elapsed=%.1fs",
            log_prefix, rows_seen, ins, upd,
            total_inserted, total_updated,
            time.monotonic() - page_started,
        )

    return total_inserted, total_updated, rows_seen


# --------------------------------------------------------------------------- #
# Recon-only mode
# --------------------------------------------------------------------------- #


def recon_only(feed: str, program_year: int) -> None:
    """Resolve the metastore URL, HEAD it, and print the first 10 column names."""
    url, modified = resolve_metastore_url(feed, program_year)
    print(f"feed={feed}  year={program_year}")
    print(f"url={url}")
    print(f"source_last_modified={modified.isoformat()}")

    with httpx.Client(
        headers={"User-Agent": "data-engine-x/cms-op-ingest"},
        timeout=30.0,
        follow_redirects=True,
    ) as client:
        hr = client.head(url)
        print(f"HEAD status={hr.status_code}")
        print(f"content-length={hr.headers.get('content-length', 'unknown')}")
        print(f"last-modified={hr.headers.get('last-modified', 'unknown')}")

        # Fetch first 64KB to parse the CSV header
        with client.stream("GET", url, follow_redirects=True) as r:
            r.raise_for_status()
            chunk = next(r.iter_bytes(65536))

    first_line = chunk.split(b"\n")[0].decode("utf-8", errors="replace")
    cols = [c.strip() for c in first_line.split(",")]
    print(f"columns (first 10 of {len(cols)}): {cols[:10]}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _database_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED is not set. Run under doppler run -p hq-all -c prd --")
    return url


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("feed", choices=list(FORMS.keys()),
                   help="Feed to ingest: general, research, or ownership.")
    p.add_argument("program_year", type=int,
                   help="CMS program year (e.g. 2024).")
    p.add_argument("--skip-if-unchanged", action="store_true",
                   help="No-op if source_last_modified has not advanced since "
                        "the prior completed run.")
    p.add_argument("--recon-only", action="store_true",
                   help="Resolve and HEAD the metastore URL, print column names; "
                        "no DB writes.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap rows read (smoke testing only).")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = FORMS[args.feed]
    log_prefix = f"[{args.feed} {args.program_year}]"

    if args.recon_only:
        recon_only(args.feed, args.program_year)
        return 0

    started_wall = time.monotonic()

    # Resolve URL before opening the DB connection (fail fast if metastore is down)
    try:
        source_url, source_last_modified = resolve_metastore_url(args.feed, args.program_year)
    except Exception as exc:
        log.exception("%s resolve_metastore_url failed", log_prefix)
        # No ops row to finalize — we never made it to DB
        return 1

    source_filename = source_url.rstrip("/").split("/")[-1]

    invoked_by = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"

    conn_url = _database_url()
    row_id: uuid.UUID | None = None

    try:
        with psycopg.connect(conn_url) as conn:
            if args.skip_if_unchanged and check_skip_if_unchanged(
                conn, args.feed, args.program_year, source_last_modified
            ):
                _run_id, row_id = insert_run_row(
                    conn, args.feed, args.program_year, 1,
                    source_url, source_filename, source_last_modified, invoked_by,
                )
                finalize_run_row(conn, row_id, "no_change")
                log.info("%s no_change — source unchanged, exiting", log_prefix)
                return 0

            _run_id, row_id = insert_run_row(
                conn, args.feed, args.program_year, 1,
                source_url, source_filename, source_last_modified, invoked_by,
            )

            try:
                ins, upd, seen = stream_csv_to_db(
                    source_url, conn, cfg, args.program_year,
                    source_file_last_modified=source_last_modified,
                    batch_size=DEFAULT_BATCH_SIZE,
                    max_rows=args.limit,
                )
                duration = round(time.monotonic() - started_wall, 3)
                finalize_run_row(
                    conn, row_id, "completed",
                    rows_loaded=seen,
                    rows_inserted=ins,
                    rows_updated=upd,
                    duration_seconds=duration,
                )
                log.info(
                    "%s DONE rows_seen=%d ins=%d upd=%d wall=%.1fs",
                    log_prefix, seen, ins, upd, duration,
                )
                return 0

            except (httpx.HTTPError, httpx.StreamError, IOError) as exc:
                duration = round(time.monotonic() - started_wall, 3)
                finalize_run_row(
                    conn, row_id, "failed",
                    duration_seconds=duration,
                    error_class="download_failure",
                    error_message=str(exc),
                )
                log.exception("%s download_failure", log_prefix)
                raise

            except psycopg.Error as exc:
                duration = round(time.monotonic() - started_wall, 3)
                finalize_run_row(
                    conn, row_id, "failed",
                    duration_seconds=duration,
                    error_class="db_failure",
                    error_message=str(exc),
                )
                log.exception("%s db_failure", log_prefix)
                raise

            except Exception as exc:
                duration = round(time.monotonic() - started_wall, 3)
                finalize_run_row(
                    conn, row_id, "failed",
                    duration_seconds=duration,
                    error_class="unknown",
                    error_message=str(exc),
                )
                log.exception("%s unknown failure", log_prefix)
                raise

    except (httpx.HTTPError, httpx.StreamError, IOError, psycopg.Error):
        return 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
