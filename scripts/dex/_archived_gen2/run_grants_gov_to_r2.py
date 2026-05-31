"""grants.gov daily open-opportunities ingest — R2 + Lance × 2 + Polaris.

Downloads today's grants.gov bulk zip (full cumulative snapshot), transcodes
the XML into two partitioned Parquet files on R2:

    s3://dex-raw-landing-zone/grants-gov/release={YYYY-MM-DD}/synopsis/data.parquet
    s3://dex-raw-landing-zone/grants-gov/release={YYYY-MM-DD}/forecast/data.parquet

Then emits both as Lance datasets (Pattern A × 2, direct-emit):

    s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_synopsis_lance/
    s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_forecast_lance/

BTREE scalar index on opportunity_id for both. Polaris Generic Table registration
(idempotent). Audit ledger row in ops.grants_gov_r2_ingest_runs.

Idempotency: HEAD-checks the R2 synopsis key; if already present for today, inserts
a `no_change` ledger row and returns early without re-downloading.

Architecture ref:
    DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A — Direct source hydration"
    DATA-FACTORY-DATASET-LIFECYCLE-PLAYBOOK.md §"Stage 5 — Lance emit (Pattern A)"

Cite:
    run_ca_sos_entities_lance_emit.py:40,65-67,151,210-219,223-224 — direct-emit shape
    run_usaspending_api_daily_assistance_ingest.py:39-100 — daily ingest entrypoint shape
    scripts/_lib/lance_commit_lock.py:63-95 — context-manager signature
    apps/data-engine-x/CLAUDE.md §"Source ingest invariant" — R2→Lance carve-out
    DATA-FACTORY-LESSONS-LEARNED.md §L4 — 5-status CHECK
    DATA-FACTORY-LESSONS-LEARNED.md §L42 — plain .parquet, no Content-Encoding: zstd
    DATA-FACTORY-LESSONS-LEARNED.md §L47 — modal run --detach for >5min jobs
    DATA-FACTORY-LESSONS-LEARNED.md §L50 — ops.data_sources 5-col, format=lance
    DATA-FACTORY-LESSONS-LEARNED.md §L54 — pipe-delimited VARCHAR for multi-value cols
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
import urllib.request
import uuid
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger("grants-gov-ingest")

# ── Canonical constants (single source of truth; imported by Modal app) ─────
GRANTS_GOV_ZIP_URL = (
    "https://prod-grants-gov-chatbot.s3.amazonaws.com/extracts/"
    "GrantsDBExtract{YYYYMMDD}v2.zip"
)
R2_BUCKET = "dex-raw-landing-zone"

# Canonical R2 keys per DATA-FACTORY-DATASET-LIFECYCLE-PLAYBOOK §"Stage 2"
def r2_synopsis_key(feed_date: date) -> str:
    return f"grants-gov/release={feed_date.isoformat()}/synopsis/data.parquet"

def r2_forecast_key(feed_date: date) -> str:
    return f"grants-gov/release={feed_date.isoformat()}/forecast/data.parquet"

# Canonical Lance URIs — SINGLE SOURCE OF TRUTH (validator p3 risk mitigation)
SYNOPSIS_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_synopsis_lance"
)
FORECAST_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/grants_gov/opportunity_forecast_lance"
)
SYNOPSIS_DATASET_SLUG = "grants_gov_opportunity_synopsis_lance"
FORECAST_DATASET_SLUG = "grants_gov_opportunity_forecast_lance"

MIN_SYNOPSIS_ROWS = 75_000  # c2 floor
MIN_FORECAST_ROWS = 1_000   # c2 floor

# ── Pyarrow schemas (explicit; drift-resistant per FABS precedent) ───────────
# Date fields are MMDDYYYY in XML → transcoded to pa.date32() per directive
# OpportunityID is INT in source but INT8 per directive (the PK for BTREE)
# Multi-value repeating elements (EligibleApplicants, CategoryOfFundingActivity)
# encoded as pipe-delimited VARCHAR per L54 (dodges LIST<VARCHAR> buffer cap)

SYNOPSIS_SCHEMA = pa.schema([
    pa.field("opportunity_id",             pa.int64(),   nullable=False),
    pa.field("opportunity_title",          pa.string(),  nullable=True),
    pa.field("opportunity_number",         pa.string(),  nullable=True),
    pa.field("opportunity_category",       pa.string(),  nullable=True),
    pa.field("opportunity_category_explanation", pa.string(), nullable=True),
    pa.field("cfda_numbers",               pa.string(),  nullable=True),
    pa.field("agency_code",                pa.string(),  nullable=True),
    pa.field("agency_name",                pa.string(),  nullable=True),
    pa.field("post_date",                  pa.date32(),  nullable=True),
    pa.field("close_date",                 pa.date32(),  nullable=True),
    pa.field("archive_date",               pa.date32(),  nullable=True),
    pa.field("last_updated_date",          pa.date32(),  nullable=True),
    pa.field("award_ceiling",              pa.int64(),   nullable=True),
    pa.field("award_floor",                pa.int64(),   nullable=True),
    pa.field("estimated_total_program_funding", pa.int64(), nullable=True),
    pa.field("expected_number_of_awards",  pa.int64(),   nullable=True),
    pa.field("eligible_applicants",        pa.string(),  nullable=True),  # pipe-delimited L54
    pa.field("category_of_funding_activity", pa.string(), nullable=True), # pipe-delimited L54
    pa.field("category_explanation",       pa.string(),  nullable=True),
    pa.field("description",                pa.string(),  nullable=True),
    pa.field("cost_sharing_or_matching_requirement", pa.string(), nullable=True),
    pa.field("funding_instrument_type",    pa.string(),  nullable=True),
    pa.field("version",                    pa.string(),  nullable=True),
    pa.field("additional_information_url", pa.string(),  nullable=True),
    pa.field("additional_information_text", pa.string(), nullable=True),
    pa.field("additional_information_on_eligibility", pa.string(), nullable=True),
    pa.field("grantor_contact_email",      pa.string(),  nullable=True),
    pa.field("grantor_contact_email_description", pa.string(), nullable=True),
    pa.field("grantor_contact_name",       pa.string(),  nullable=True),
    pa.field("grantor_contact_phone_number", pa.string(), nullable=True),
    pa.field("grantor_contact_text",       pa.string(),  nullable=True),
    pa.field("close_date_explanation",     pa.string(),  nullable=True),
])

FORECAST_SCHEMA = pa.schema([
    pa.field("opportunity_id",             pa.int64(),   nullable=False),
    pa.field("opportunity_title",          pa.string(),  nullable=True),
    pa.field("opportunity_number",         pa.string(),  nullable=True),
    pa.field("opportunity_category",       pa.string(),  nullable=True),
    pa.field("cfda_numbers",               pa.string(),  nullable=True),
    pa.field("agency_code",                pa.string(),  nullable=True),
    pa.field("agency_name",                pa.string(),  nullable=True),
    pa.field("eligible_applicants",        pa.string(),  nullable=True),  # pipe-delimited L54
    pa.field("category_of_funding_activity", pa.string(), nullable=True), # pipe-delimited L54
    pa.field("description",                pa.string(),  nullable=True),
    pa.field("award_ceiling",              pa.int64(),   nullable=True),
    pa.field("award_floor",                pa.int64(),   nullable=True),
    pa.field("estimated_total_program_funding", pa.int64(), nullable=True),
    pa.field("expected_number_of_awards",  pa.int64(),   nullable=True),
    pa.field("cost_sharing_or_matching_requirement", pa.string(), nullable=True),
    pa.field("version",                    pa.string(),  nullable=True),
    # Forecast-only date fields
    pa.field("estimated_synopsis_post_date",  pa.date32(), nullable=True),
    pa.field("estimated_synopsis_close_date", pa.date32(), nullable=True),
    pa.field("estimated_synopsis_close_date_explanation", pa.string(), nullable=True),
    pa.field("estimated_award_date",          pa.date32(), nullable=True),
    pa.field("estimated_project_start_date",  pa.date32(), nullable=True),
    pa.field("fiscal_year",                   pa.string(), nullable=True),
    pa.field("grantor_contact_email",      pa.string(),  nullable=True),
    pa.field("grantor_contact_name",       pa.string(),  nullable=True),
    pa.field("grantor_contact_phone_number", pa.string(), nullable=True),
    pa.field("grantor_contact_text",       pa.string(),  nullable=True),
])


def _parse_mmddyyyy(val: str | None) -> date | None:
    """MMDDYYYY → Python date. Returns None on empty / malformed."""
    if not val or len(val) < 8:
        return None
    try:
        return date(int(val[4:8]), int(val[0:2]), int(val[2:4]))
    except (ValueError, IndexError):
        return None


def _int_or_none(val: str | None) -> int | None:
    if not val:
        return None
    try:
        return int(val.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _text_or_none(val: str | None) -> str | None:
    return val.strip() if val and val.strip() else None


def _parse_synopsis(elem) -> dict[str, Any]:
    """Extract one OpportunitySynopsisDetail_1_0 element → dict row."""
    g = lambda tag: _text_or_none(
        elem.findtext(f"{{http://apply.grants.gov/system/OpportunityDetail-V1.0}}{tag}")
    )
    # Repeating elements — collect all, pipe-join per L54
    def _multi(tag: str) -> str | None:
        ns = "{http://apply.grants.gov/system/OpportunityDetail-V1.0}"
        vals = [
            e.text.strip()
            for e in elem.findall(f"{ns}{tag}")
            if e.text and e.text.strip()
        ]
        return "|".join(vals) if vals else None

    return {
        "opportunity_id":             _int_or_none(g("OpportunityID")),
        "opportunity_title":          g("OpportunityTitle"),
        "opportunity_number":         g("OpportunityNumber"),
        "opportunity_category":       g("OpportunityCategory"),
        "opportunity_category_explanation": g("OpportunityCategoryExplanation"),
        "cfda_numbers":               g("CFDANumbers"),
        "agency_code":                g("AgencyCode"),
        "agency_name":                g("AgencyName"),
        "post_date":                  _parse_mmddyyyy(g("PostDate")),
        "close_date":                 _parse_mmddyyyy(g("CloseDate")),
        "archive_date":               _parse_mmddyyyy(g("ArchiveDate")),
        "last_updated_date":          _parse_mmddyyyy(g("LastUpdatedDate")),
        "award_ceiling":              _int_or_none(g("AwardCeiling")),
        "award_floor":                _int_or_none(g("AwardFloor")),
        "estimated_total_program_funding": _int_or_none(g("EstimatedTotalProgramFunding")),
        "expected_number_of_awards":  _int_or_none(g("ExpectedNumberOfAwards")),
        "eligible_applicants":        _multi("EligibleApplicants"),
        "category_of_funding_activity": _multi("CategoryOfFundingActivity"),
        "category_explanation":       g("CategoryExplanation"),
        "description":                g("Description"),
        "cost_sharing_or_matching_requirement": g("CostSharingOrMatchingRequirement"),
        "funding_instrument_type":    g("FundingInstrumentType"),
        "version":                    g("Version"),
        "additional_information_url": g("AdditionalInformationURL"),
        "additional_information_text": g("AdditionalInformationText"),
        "additional_information_on_eligibility": g("AdditionalInformationOnEligibility"),
        "grantor_contact_email":      g("GrantorContactEmail"),
        "grantor_contact_email_description": g("GrantorContactEmailDescription"),
        "grantor_contact_name":       g("GrantorContactName"),
        "grantor_contact_phone_number": g("GrantorContactPhoneNumber"),
        "grantor_contact_text":       g("GrantorContactText"),
        "close_date_explanation":     g("CloseDateExplanation"),
    }


def _parse_forecast(elem) -> dict[str, Any]:
    """Extract one OpportunityForecastDetail_1_0 element → dict row."""
    g = lambda tag: _text_or_none(
        elem.findtext(f"{{http://apply.grants.gov/system/OpportunityDetail-V1.0}}{tag}")
    )
    def _multi(tag: str) -> str | None:
        ns = "{http://apply.grants.gov/system/OpportunityDetail-V1.0}"
        vals = [
            e.text.strip()
            for e in elem.findall(f"{ns}{tag}")
            if e.text and e.text.strip()
        ]
        return "|".join(vals) if vals else None

    return {
        "opportunity_id":             _int_or_none(g("OpportunityID")),
        "opportunity_title":          g("OpportunityTitle"),
        "opportunity_number":         g("OpportunityNumber"),
        "opportunity_category":       g("OpportunityCategory"),
        "cfda_numbers":               g("CFDANumbers"),
        "agency_code":                g("AgencyCode"),
        "agency_name":                g("AgencyName"),
        "eligible_applicants":        _multi("EligibleApplicants"),
        "category_of_funding_activity": _multi("CategoryOfFundingActivity"),
        "description":                g("Description"),
        "award_ceiling":              _int_or_none(g("AwardCeiling")),
        "award_floor":                _int_or_none(g("AwardFloor")),
        "estimated_total_program_funding": _int_or_none(g("EstimatedTotalProgramFunding")),
        "expected_number_of_awards":  _int_or_none(g("ExpectedNumberOfAwards")),
        "cost_sharing_or_matching_requirement": g("CostSharingOrMatchingRequirement"),
        "version":                    g("Version"),
        "estimated_synopsis_post_date":  _parse_mmddyyyy(g("EstimatedSynopsisPostDate")),
        "estimated_synopsis_close_date": _parse_mmddyyyy(g("EstimatedSynopsisCloseDate")),
        "estimated_synopsis_close_date_explanation": g("EstimatedSynopsisCloseDateExplanation"),
        "estimated_award_date":          _parse_mmddyyyy(g("EstimatedAwardDate")),
        "estimated_project_start_date":  _parse_mmddyyyy(g("EstimatedProjectStartDate")),
        "fiscal_year":                   g("FiscalYear"),
        "grantor_contact_email":      g("GrantorContactEmail"),
        "grantor_contact_name":       g("GrantorContactName"),
        "grantor_contact_phone_number": g("GrantorContactPhoneNumber"),
        "grantor_contact_text":       g("GrantorContactText"),
    }


def _transcode_xml_to_parquet(
    xml_bytes: bytes,
    synopsis_path: str,
    forecast_path: str,
) -> tuple[int, int]:
    """Stream-parse XML with lxml iterparse. Returns (synopsis_count, forecast_count).

    Uses chunked pyarrow ParquetWriter to bound memory on 317 MB XML.
    Writes to local tmp paths; caller uploads to R2.
    """
    from lxml import etree  # noqa: lazy import; Modal image must have lxml

    SYNOPSIS_TAG = "{http://apply.grants.gov/system/OpportunityDetail-V1.0}OpportunitySynopsisDetail_1_0"
    FORECAST_TAG = "{http://apply.grants.gov/system/OpportunityDetail-V1.0}OpportunityForecastDetail_1_0"

    CHUNK = 5_000  # rows per pyarrow batch flush

    syn_buf: list[dict] = []
    fct_buf: list[dict] = []
    syn_count = 0
    fct_count = 0

    syn_writer: pq.ParquetWriter | None = None
    fct_writer: pq.ParquetWriter | None = None

    def flush_synopsis():
        nonlocal syn_writer, syn_buf
        if not syn_buf:
            return
        tbl = pa.Table.from_pylist(syn_buf, schema=SYNOPSIS_SCHEMA)
        if syn_writer is None:
            syn_writer = pq.ParquetWriter(synopsis_path, SYNOPSIS_SCHEMA)
        syn_writer.write_table(tbl)
        syn_buf = []

    def flush_forecast():
        nonlocal fct_writer, fct_buf
        if not fct_buf:
            return
        tbl = pa.Table.from_pylist(fct_buf, schema=FORECAST_SCHEMA)
        if fct_writer is None:
            fct_writer = pq.ParquetWriter(forecast_path, FORECAST_SCHEMA)
        fct_writer.write_table(tbl)
        fct_buf = []

    context = etree.iterparse(io.BytesIO(xml_bytes), events=("end",))
    for _event, elem in context:
        tag = elem.tag
        if tag == SYNOPSIS_TAG:
            syn_buf.append(_parse_synopsis(elem))
            syn_count += 1
            if len(syn_buf) >= CHUNK:
                flush_synopsis()
            elem.clear()
        elif tag == FORECAST_TAG:
            fct_buf.append(_parse_forecast(elem))
            fct_count += 1
            if len(fct_buf) >= CHUNK:
                flush_forecast()
            elem.clear()

    flush_synopsis()
    flush_forecast()

    if syn_writer is not None:
        syn_writer.close()
    if fct_writer is not None:
        fct_writer.close()

    LOG.info("transcode: synopsis=%d forecast=%d", syn_count, fct_count)
    return syn_count, fct_count


def _s3_client():
    """Boto3 S3 client pointing at R2."""
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _r2_key_exists(s3, key: str) -> bool:
    """HEAD-check idempotency per DATA-FACTORY-DATASET-LIFECYCLE-PLAYBOOK §"Stage 3"."""
    try:
        s3.head_object(Bucket=R2_BUCKET, Key=key)
        return True
    except Exception:
        return False


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _write_ledger_row(
    *,
    run_id: str,
    feed_date: date,
    status: str,
    source_url: str | None = None,
    zip_bytes: int | None = None,
    synopsis_row_count: int | None = None,
    forecast_row_count: int | None = None,
    r2_synopsis_key: str | None = None,
    r2_forecast_key: str | None = None,
    r2_synopsis_bytes: int | None = None,
    r2_forecast_bytes: int | None = None,
    lance_synopsis_rows: int | None = None,
    lance_forecast_rows: int | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    duration_seconds: float | None = None,
    error_message: str | None = None,
) -> None:
    import psycopg

    db_url = (
        os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DATABASE_URL")
    )
    if not db_url:
        LOG.warning("no DB URL available; skipping ledger write")
        return

    with psycopg.connect(db_url) as conn:
        conn.execute(
            """
            INSERT INTO ops.grants_gov_r2_ingest_runs (
                id, feed_date, status,
                source_url, zip_bytes,
                synopsis_row_count, forecast_row_count,
                r2_bucket, r2_synopsis_key, r2_forecast_key,
                r2_synopsis_bytes, r2_forecast_bytes,
                lance_synopsis_rows, lance_forecast_rows,
                lance_synopsis_uri, lance_forecast_uri,
                started_at, finished_at, duration_seconds,
                error_message
            ) VALUES (
                %s::uuid, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s, %s,
                %s, %s,
                %s, %s,
                %s, %s, %s,
                %s
            )
            ON CONFLICT (id) DO UPDATE
              SET status            = EXCLUDED.status,
                  finished_at       = EXCLUDED.finished_at,
                  duration_seconds  = EXCLUDED.duration_seconds,
                  synopsis_row_count= EXCLUDED.synopsis_row_count,
                  forecast_row_count= EXCLUDED.forecast_row_count,
                  r2_synopsis_bytes = EXCLUDED.r2_synopsis_bytes,
                  r2_forecast_bytes = EXCLUDED.r2_forecast_bytes,
                  lance_synopsis_rows = EXCLUDED.lance_synopsis_rows,
                  lance_forecast_rows = EXCLUDED.lance_forecast_rows,
                  error_message     = EXCLUDED.error_message
            """,
            (
                run_id, feed_date.isoformat(), status,
                source_url, zip_bytes,
                synopsis_row_count, forecast_row_count,
                R2_BUCKET, r2_synopsis_key, r2_forecast_key,
                r2_synopsis_bytes, r2_forecast_bytes,
                lance_synopsis_rows, lance_forecast_rows,
                SYNOPSIS_LANCE_URI, FORECAST_LANCE_URI,
                started_at or datetime.now(timezone.utc),
                finished_at,
                duration_seconds,
                error_message,
            ),
        )
        conn.commit()


def _lance_emit_both(
    synopsis_local_path: str,
    forecast_local_path: str,
) -> tuple[int, int]:
    """Emit both Lance datasets from local Parquet files.

    Pattern A direct-emit per DATA-FACTORY-ARCHITECTURE-PATTERNS.md
    §"Pattern A — Template — direct emit":
    - lance_commit_lock wrapping REQUIRED (c8)
    - LANCE_BYPASS_SPILLING=true + TMPDIR=/tmp/lance
    - mode="overwrite" (current-state semantics)
    - create_scalar_index("opportunity_id", BTREE)
    - compact_files() + cleanup_old_versions(7 days)

    Cite: run_ca_sos_entities_lance_emit.py:151,210-219,223-224
    """
    from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa
    from datetime import timedelta
    import lance

    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = "/tmp/lance"
    Path("/tmp/lance").mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANCE_INDEX_CACHE_SIZE", "1g")

    storage_options = _storage_options()

    # ── Synopsis Lance emit ──────────────────────────────────────────────────
    LOG.info("Lance emit: synopsis → %s", SYNOPSIS_LANCE_URI)
    syn_tbl = pq.read_table(synopsis_local_path, schema=SYNOPSIS_SCHEMA)
    syn_rows = len(syn_tbl)
    LOG.info("synopsis parquet rows: %d (floor %d)", syn_rows, MIN_SYNOPSIS_ROWS)
    if syn_rows < MIN_SYNOPSIS_ROWS:
        raise RuntimeError(
            f"synopsis row count {syn_rows} below floor {MIN_SYNOPSIS_ROWS}"
        )

    t0 = time.time()
    with lance_commit_lock(SYNOPSIS_DATASET_SLUG):
        ds_syn = lance.write_dataset(
            syn_tbl,
            SYNOPSIS_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        ds_syn.create_scalar_index("opportunity_id", index_type="BTREE", replace=True)
        LOG.info("BTREE on synopsis.opportunity_id: OK")
        try:
            ds_syn.optimize.compact_files()
            ds_syn.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            LOG.warning("synopsis optimize/cleanup (non-fatal): %s", e)
    syn_lance_rows = ds_syn.count_rows()
    LOG.info(
        "synopsis lance done: %d rows in %.1fs",
        syn_lance_rows, time.time() - t0,
    )

    # ── Forecast Lance emit ──────────────────────────────────────────────────
    LOG.info("Lance emit: forecast → %s", FORECAST_LANCE_URI)
    fct_tbl = pq.read_table(forecast_local_path, schema=FORECAST_SCHEMA)
    fct_rows = len(fct_tbl)
    LOG.info("forecast parquet rows: %d (floor %d)", fct_rows, MIN_FORECAST_ROWS)
    if fct_rows < MIN_FORECAST_ROWS:
        raise RuntimeError(
            f"forecast row count {fct_rows} below floor {MIN_FORECAST_ROWS}"
        )

    t1 = time.time()
    with lance_commit_lock(FORECAST_DATASET_SLUG):
        ds_fct = lance.write_dataset(
            fct_tbl,
            FORECAST_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        ds_fct.create_scalar_index("opportunity_id", index_type="BTREE", replace=True)
        LOG.info("BTREE on forecast.opportunity_id: OK")
        try:
            ds_fct.optimize.compact_files()
            ds_fct.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            LOG.warning("forecast optimize/cleanup (non-fatal): %s", e)
    fct_lance_rows = ds_fct.count_rows()
    LOG.info(
        "forecast lance done: %d rows in %.1fs",
        fct_lance_rows, time.time() - t1,
    )

    return syn_lance_rows, fct_lance_rows


def _polaris_register() -> None:
    """Idempotent Polaris Generic Table registration × 2.

    Cite: init_polaris_lance_generic.py:49,66,98-156 — get_token,
    ensure_namespace, ensure_generic_table.
    """
    import subprocess
    scripts_dir = Path(__file__).resolve().parent
    for table, doc in [
        (
            "opportunity_synopsis_lance",
            "grants.gov open opportunities — synopsis grain (posted opportunities). "
            "BTREE on opportunity_id. Floor 75,000 rows.",
        ),
        (
            "opportunity_forecast_lance",
            "grants.gov open opportunities — forecast grain (upcoming opportunities). "
            "BTREE on opportunity_id. Floor 1,000 rows.",
        ),
    ]:
        result = subprocess.run(
            [
                sys.executable,
                str(scripts_dir / "init_polaris_lance_generic.py"),
                "--namespace", "grants_gov",
                "--table", table,
                "--doc", doc,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            LOG.error(
                "Polaris registration failed for %s: %s %s",
                table, result.stdout[-500:], result.stderr[-500:],
            )
            raise RuntimeError(f"Polaris registration failed for grants_gov.{table}")
        LOG.info("Polaris registration OK: grants_gov.%s", table)


def run_ingest(
    *,
    feed_date: date,
    local_zip_path: str | None = None,
    dry_run: bool = False,
    skip_polaris: bool = False,
) -> dict[str, Any]:
    """Main entry point. Called by Modal app + CLI."""
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    LOG.info(
        "grants-gov ingest start: feed_date=%s run_id=%s dry_run=%s",
        feed_date, run_id, dry_run,
    )

    s3 = _s3_client()
    syn_key = r2_synopsis_key(feed_date)
    fct_key = r2_forecast_key(feed_date)

    # ── Idempotency HEAD-check ───────────────────────────────────────────────
    if not dry_run and _r2_key_exists(s3, syn_key):
        LOG.info("R2 synopsis key already exists for %s — no_change", feed_date)
        _write_ledger_row(
            run_id=run_id,
            feed_date=feed_date,
            status="no_change",
            r2_synopsis_key=syn_key,
            r2_forecast_key=fct_key,
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
        )
        return {"status": "no_change", "run_id": run_id, "feed_date": feed_date.isoformat()}

    # ── Write pending ledger row ─────────────────────────────────────────────
    if not dry_run:
        _write_ledger_row(
            run_id=run_id,
            feed_date=feed_date,
            status="running",
            started_at=started_at,
        )

    try:
        # ── Download zip ────────────────────────────────────────────────────
        zip_url = GRANTS_GOV_ZIP_URL.format(
            YYYYMMDD=feed_date.strftime("%Y%m%d")
        )
        LOG.info("downloading zip from %s", zip_url)

        if local_zip_path and os.path.exists(local_zip_path):
            LOG.info("reusing local zip: %s", local_zip_path)
            zip_data = Path(local_zip_path).read_bytes()
        else:
            req = urllib.request.Request(
                zip_url,
                headers={"User-Agent": "data-engine-x/1.0"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                zip_data = resp.read()

        zip_bytes = len(zip_data)
        LOG.info("zip downloaded: %d bytes", zip_bytes)

        # ── Unzip XML ────────────────────────────────────────────────────────
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            xml_names = [n for n in zf.namelist() if n.endswith(".xml")]
            if not xml_names:
                raise ValueError("No .xml file found in grants.gov zip")
            xml_name = xml_names[0]
            LOG.info("extracting %s ...", xml_name)
            xml_bytes = zf.read(xml_name)
        LOG.info("xml unzipped: %d bytes", len(xml_bytes))

        # ── Transcode XML → Parquet ──────────────────────────────────────────
        tmp_dir = Path("/tmp/grants-gov-ingest")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        syn_tmp = str(tmp_dir / "synopsis.parquet")
        fct_tmp = str(tmp_dir / "forecast.parquet")

        syn_rows, fct_rows = _transcode_xml_to_parquet(xml_bytes, syn_tmp, fct_tmp)
        del xml_bytes  # free memory before R2 upload

        if dry_run:
            LOG.info("dry_run: synopsis=%d forecast=%d; skipping R2 upload", syn_rows, fct_rows)
            return {
                "status": "dry_run",
                "run_id": run_id,
                "feed_date": feed_date.isoformat(),
                "synopsis_rows": syn_rows,
                "forecast_rows": fct_rows,
            }

        # ── Upload Parquet to R2 (L42: plain .parquet, no Content-Encoding: zstd) ──
        LOG.info("uploading synopsis to R2 at %s ...", syn_key)
        s3.upload_file(
            syn_tmp,
            R2_BUCKET,
            syn_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        syn_obj = s3.head_object(Bucket=R2_BUCKET, Key=syn_key)
        syn_bytes = syn_obj.get("ContentLength", 0)
        LOG.info("synopsis uploaded: %d bytes", syn_bytes)

        LOG.info("uploading forecast to R2 at %s ...", fct_key)
        s3.upload_file(
            fct_tmp,
            R2_BUCKET,
            fct_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        fct_obj = s3.head_object(Bucket=R2_BUCKET, Key=fct_key)
        fct_bytes = fct_obj.get("ContentLength", 0)
        LOG.info("forecast uploaded: %d bytes", fct_bytes)

        # ── Lance emit × 2 ──────────────────────────────────────────────────
        syn_lance_rows, fct_lance_rows = _lance_emit_both(syn_tmp, fct_tmp)

        # ── Polaris registration (idempotent) ────────────────────────────────
        if not skip_polaris:
            _polaris_register()

        # ── Completed ledger row ─────────────────────────────────────────────
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        _write_ledger_row(
            run_id=run_id,
            feed_date=feed_date,
            status="completed",
            source_url=zip_url,
            zip_bytes=zip_bytes,
            synopsis_row_count=syn_rows,
            forecast_row_count=fct_rows,
            r2_synopsis_key=syn_key,
            r2_forecast_key=fct_key,
            r2_synopsis_bytes=syn_bytes,
            r2_forecast_bytes=fct_bytes,
            lance_synopsis_rows=syn_lance_rows,
            lance_forecast_rows=fct_lance_rows,
            started_at=started_at,
            finished_at=finished_at,
            duration_seconds=duration,
        )
        LOG.info(
            "grants-gov ingest complete: synopsis=%d forecast=%d duration=%.1fs",
            syn_rows, fct_rows, duration,
        )
        return {
            "status": "completed",
            "run_id": run_id,
            "feed_date": feed_date.isoformat(),
            "synopsis_rows": syn_rows,
            "forecast_rows": fct_rows,
            "syn_lance_rows": syn_lance_rows,
            "fct_lance_rows": fct_lance_rows,
            "duration_seconds": duration,
        }

    except Exception as exc:
        finished_at = datetime.now(timezone.utc)
        duration = (finished_at - started_at).total_seconds()
        LOG.error("grants-gov ingest failed: %s", exc, exc_info=True)
        try:
            _write_ledger_row(
                run_id=run_id,
                feed_date=feed_date,
                status="failed",
                started_at=started_at,
                finished_at=finished_at,
                duration_seconds=duration,
                error_message=str(exc)[:4000],
            )
        except Exception as e2:
            LOG.warning("failed to write failure ledger row: %s", e2)
        raise


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--feed-date",
        type=lambda s: date.fromisoformat(s),
        default=None,
        help="Date to ingest (default: today UTC)",
    )
    parser.add_argument(
        "--local-zip",
        default=None,
        help="Path to a local zip file (skips download; useful for dev)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-polaris", action="store_true")
    return parser.parse_args(argv)


if __name__ == "__main__":
    import json as _json

    args = _parse_args(sys.argv[1:])
    feed_date = args.feed_date or datetime.now(timezone.utc).date()

    # Local CLI needs scripts._lib importable
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

    result = run_ingest(
        feed_date=feed_date,
        local_zip_path=args.local_zip,
        dry_run=args.dry_run,
        skip_polaris=args.skip_polaris,
    )
    print(_json.dumps(result, default=str, indent=2))
