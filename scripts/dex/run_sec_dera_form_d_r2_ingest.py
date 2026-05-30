#!/usr/bin/env python3
"""SEC DERA Form D Data Sets → R2 ingest.

Parses quarterly archive index at https://www.sec.gov/dera/data/form-d
to discover all available quarters (do NOT hardcode — DERA re-publishes
historical zips occasionally). For each quarter:
  1. HEAD-check zip URL → capture Last-Modified.
  2. Skip-if-unchanged: if Last-Modified <= prior completed (release, table_name)
     run's source_observed_at, write no_change audit rows and continue.
  3. Download zip via L55 User-Agent.
  4. Unzip into staging dir; expect 6 TSV files.
  5. Per TSV: DuckDB read_csv(..., all_varchar=TRUE, quote='"', escape='"',
     null_padding=TRUE, strict_mode=FALSE, columns=<canonical 2026q1 set per table>)
     → COPY TO ZSTD Parquet (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000).
  6. boto3 upload to s3://dex-raw-landing-zone/sec-dera/form-d/release=YYYYqQ/<table_lower>.parquet
     with ExtraArgs={"ContentType":"application/x-parquet"} ONLY.
     No extra encoding header (L42 — only ContentType set).
  7. Per (release, table_name): write/update ops.sec_dera_form_d_r2_ingest_runs row
     with L4 status set (pending → running → completed/failed/no_change).

CLI:
  --apply / --dry-run (mutually exclusive)
  --quarters YYYYqQ,YYYYqQ,...     # backfill subset
  --skip-if-unchanged              # default for incremental cadence
  --max-rows N                     # smoke

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_form_d_r2_ingest.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_form_d_r2_ingest.py --apply --skip-if-unchanged
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_form_d_r2_ingest.py --apply --quarters 2026q1,2025q4
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_form_d_r2_ingest.py --dry-run --quarters 2008q1
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None  # type: ignore

LOG = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

USER_AGENT = "Mozilla/5.0 (compatible; data-engine-x/1.0; +tools@substrate.build)"
R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "sec-dera/form-d"
DERA_INDEX_URL = "https://www.sec.gov/dera/data/form-d"

# Canonical column sets per table — pinned to 2026q1 schema (union of all
# post-2014 columns). all_varchar=TRUE means type is VARCHAR for all; the
# columns dict is the set of expected column names → DuckDB type.
# null_padding=TRUE + strict_mode=FALSE handles pre-2014 schema drift:
# older quarters get NULL on missing columns; extra columns are dropped silently.

# Canonical column sets per table — derived from 2026q1 L44 probe of the
# actual TSV files (FORMDSUBMISSION.tsv 9c, ISSUERS.tsv 23c, OFFERING.tsv 41c,
# RECIPIENTS.tsv 15c, RELATEDPERSONS.tsv 15c, SIGNATURES.tsv 7c).
CANONICAL_SUBMISSION_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "FILE_NUM": "VARCHAR",
    "FILING_DATE": "VARCHAR",
    "SIC_CODE": "VARCHAR",
    "SCHEMAVERSION": "VARCHAR",
    "SUBMISSIONTYPE": "VARCHAR",
    "TESTORLIVE": "VARCHAR",
    "OVER100PERSONSFLAG": "VARCHAR",
    "OVER100ISSUERFLAG": "VARCHAR",
}

CANONICAL_ISSUERS_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "IS_PRIMARYISSUER_FLAG": "VARCHAR",
    "ISSUER_SEQ_KEY": "VARCHAR",
    "CIK": "VARCHAR",
    "ENTITYNAME": "VARCHAR",
    "STREET1": "VARCHAR",
    "STREET2": "VARCHAR",
    "CITY": "VARCHAR",
    "STATEORCOUNTRY": "VARCHAR",
    "STATEORCOUNTRYDESCRIPTION": "VARCHAR",
    "ZIPCODE": "VARCHAR",
    "ISSUERPHONENUMBER": "VARCHAR",
    "JURISDICTIONOFINC": "VARCHAR",
    "ISSUER_PREVIOUSNAME_1": "VARCHAR",
    "ISSUER_PREVIOUSNAME_2": "VARCHAR",
    "ISSUER_PREVIOUSNAME_3": "VARCHAR",
    "EDGAR_PREVIOUSNAME_1": "VARCHAR",
    "EDGAR_PREVIOUSNAME_2": "VARCHAR",
    "EDGAR_PREVIOUSNAME_3": "VARCHAR",
    "ENTITYTYPE": "VARCHAR",
    "ENTITYTYPEOTHERDESC": "VARCHAR",
    "YEAROFINC_TIMESPAN_CHOICE": "VARCHAR",
    "YEAROFINC_VALUE_ENTERED": "VARCHAR",
}

CANONICAL_OFFERING_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "INDUSTRYGROUPTYPE": "VARCHAR",
    "INVESTMENTFUNDTYPE": "VARCHAR",
    "IS40ACT": "VARCHAR",
    "REVENUERANGE": "VARCHAR",
    "AGGREGATENETASSETVALUERANGE": "VARCHAR",
    "FEDERALEXEMPTIONS_ITEMS_LIST": "VARCHAR",
    "ISAMENDMENT": "VARCHAR",
    "PREVIOUSACCESSIONNUMBER": "VARCHAR",
    "SALE_DATE": "VARCHAR",
    "YETTOOCCUR": "VARCHAR",
    "MORETHANONEYEAR": "VARCHAR",
    "ISEQUITYTYPE": "VARCHAR",
    "ISDEBTTYPE": "VARCHAR",
    "ISOPTIONTOACQUIRETYPE": "VARCHAR",
    "ISSECURITYTOBEACQUIREDTYPE": "VARCHAR",
    "ISPOOLEDINVESTMENTFUNDTYPE": "VARCHAR",
    "ISTENANTINCOMMONTYPE": "VARCHAR",
    "ISMINERALPROPERTYTYPE": "VARCHAR",
    "ISOTHERTYPE": "VARCHAR",
    "DESCRIPTIONOFOTHERTYPE": "VARCHAR",
    "ISBUSINESSCOMBINATIONTRANS": "VARCHAR",
    "BUSCOMBCLARIFICATIONOFRESP": "VARCHAR",
    "MINIMUMINVESTMENTACCEPTED": "VARCHAR",
    "OVER100RECIPIENTFLAG": "VARCHAR",
    "TOTALOFFERINGAMOUNT": "VARCHAR",
    "TOTALAMOUNTSOLD": "VARCHAR",
    "TOTALREMAINING": "VARCHAR",
    "SALESAMTCLARIFICATIONOFRESP": "VARCHAR",
    "HASNONACCREDITEDINVESTORS": "VARCHAR",
    "NUMBERNONACCREDITEDINVESTORS": "VARCHAR",
    "TOTALNUMBERALREADYINVESTED": "VARCHAR",
    "SALESCOMM_DOLLARAMOUNT": "VARCHAR",
    "SALESCOMM_ISESTIMATE": "VARCHAR",
    "FINDERSFEE_DOLLARAMOUNT": "VARCHAR",
    "FINDERSFEE_ISESTIMATE": "VARCHAR",
    "FINDERFEECLARIFICATIONOFRESP": "VARCHAR",
    "GROSSPROCEEDSUSED_DOLLARAMOUNT": "VARCHAR",
    "GROSSPROCEEDSUSED_ISESTIMATE": "VARCHAR",
    "GROSSPROCEEDSUSED_CLAROFRESP": "VARCHAR",
    "AUTHORIZEDREPRESENTATIVE": "VARCHAR",
}

CANONICAL_RECIPIENTS_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "RECIPIENT_SEQ_KEY": "VARCHAR",
    "RECIPIENTNAME": "VARCHAR",
    "RECIPIENTCRDNUMBER": "VARCHAR",
    "ASSOCIATEDBDNAME": "VARCHAR",
    "ASSOCIATEDBDCRDNUMBER": "VARCHAR",
    "STREET1": "VARCHAR",
    "STREET2": "VARCHAR",
    "CITY": "VARCHAR",
    "STATEORCOUNTRY": "VARCHAR",
    "STATEORCOUNTRYDESCRIPTION": "VARCHAR",
    "ZIPCODE": "VARCHAR",
    "STATES_OR_VALUE_LIST": "VARCHAR",
    "DESCRIPTIONS_LIST": "VARCHAR",
    "FOREIGNSOLICITATION": "VARCHAR",
}

CANONICAL_RELATED_PERSONS_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "RELATEDPERSON_SEQ_KEY": "VARCHAR",
    "FIRSTNAME": "VARCHAR",
    "MIDDLENAME": "VARCHAR",
    "LASTNAME": "VARCHAR",
    "STREET1": "VARCHAR",
    "STREET2": "VARCHAR",
    "CITY": "VARCHAR",
    "STATEORCOUNTRY": "VARCHAR",
    "STATEORCOUNTRYDESCRIPTION": "VARCHAR",
    "ZIPCODE": "VARCHAR",
    "RELATIONSHIP_1": "VARCHAR",
    "RELATIONSHIP_2": "VARCHAR",
    "RELATIONSHIP_3": "VARCHAR",
    "RELATIONSHIPCLARIFICATION": "VARCHAR",
}

CANONICAL_SIGNATURES_COLS = {
    "ACCESSIONNUMBER": "VARCHAR",
    "SIGNATURE_SEQ_KEY": "VARCHAR",
    "ISSUERNAME": "VARCHAR",
    "SIGNATURENAME": "VARCHAR",
    "NAMEOFSIGNER": "VARCHAR",
    "SIGNATURETITLE": "VARCHAR",
    "SIGNATUREDATE": "VARCHAR",
}

# 6 TSV table definitions: (tsv_filename, table_name_lower, canonical_cols_dict)
FORM_D_TABLES: tuple[tuple[str, str, dict], ...] = (
    ("FORMDSUBMISSION.tsv", "submission",      CANONICAL_SUBMISSION_COLS),
    ("ISSUERS.tsv",         "issuers",         CANONICAL_ISSUERS_COLS),
    ("OFFERING.tsv",        "offering",        CANONICAL_OFFERING_COLS),
    ("RECIPIENTS.tsv",      "recipients",      CANONICAL_RECIPIENTS_COLS),
    ("RELATEDPERSONS.tsv",  "related_persons", CANONICAL_RELATED_PERSONS_COLS),
    ("SIGNATURES.tsv",      "signatures",      CANONICAL_SIGNATURES_COLS),
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _db_conn() -> psycopg.Connection:
    url = os.environ["DEX_DB_URL_DIRECT"]
    return psycopg.connect(url)


def _write_audit_row(
    conn: psycopg.Connection,
    release: str,
    table_name: str,
    status: str,
    *,
    source_observed_at: datetime | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    rows_written: int | None = None,
    r2_key: str | None = None,
    error_message: str | None = None,
) -> None:
    if started_at is None:
        started_at = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.sec_dera_form_d_r2_ingest_runs
                (release, table_name, status, source_observed_at, started_at,
                 completed_at, rows_written, r2_key, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                release, table_name, status, source_observed_at, started_at,
                completed_at, rows_written, r2_key, error_message,
            ),
        )
    conn.commit()


def _get_prior_completed(
    conn: psycopg.Connection, release: str, table_name: str,
) -> datetime | None:
    """Return source_observed_at of the most recent completed/no_change run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_observed_at FROM ops.sec_dera_form_d_r2_ingest_runs
            WHERE release = %s AND table_name = %s
              AND status IN ('completed', 'no_change')
            ORDER BY started_at DESC LIMIT 1
            """,
            (release, table_name),
        )
        row = cur.fetchone()
    if row and row[0]:
        return row[0] if row[0].tzinfo else row[0].replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _make_client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
        timeout=120.0,
    )


def _head_last_modified(client: httpx.Client, url: str) -> datetime | None:
    try:
        resp = client.head(url)
        if resp.status_code == 200:
            lm = resp.headers.get("last-modified")
            if lm:
                return parsedate_to_datetime(lm).astimezone(timezone.utc)
    except Exception as e:
        LOG.warning("HEAD %s failed: %s", url, e)
    return None


# ---------------------------------------------------------------------------
# DERA index parse
# ---------------------------------------------------------------------------

def parse_quarters_from_dera_index(client: httpx.Client) -> list[str]:
    """Scrape the DERA Form D index page; return sorted list of quarter strings
    like ['2008q1', '2008q2', ..., '2026q1']. Does NOT hardcode the list."""
    LOG.info("Fetching DERA Form D index: %s", DERA_INDEX_URL)
    resp = client.get(DERA_INDEX_URL)
    resp.raise_for_status()

    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "lxml")
        links = soup.find_all("a", href=True)
    else:
        # Fallback: regex on raw HTML
        links = [type("L", (), {"get": lambda self, k, d=None: d})() for _ in []]
        links = []

    quarters: list[str] = []
    # DERA zip URL forms on the current SEC index page (verified 2026-05-18):
    #   /files/structureddata/data/form-d-data-sets/2026q1_d.zip
    #   /files/structureddata/data/form-d-data-sets/2008q2_d_0.zip   (older era)
    # Older 2008q2..2013q4 quarters use the `_d_0.zip` suffix; newer use `_d.zip`.
    # Match BOTH forms so the older 22 quarters aren't silently dropped.
    zip_re = re.compile(r"/(\d{4}q[1-4])_d(?:_0)?\.zip", re.IGNORECASE)

    if BeautifulSoup is not None:
        for link in links:
            href = link.get("href", "")
            m = zip_re.search(href)
            if m:
                quarters.append(m.group(1).lower())
    else:
        for m in zip_re.finditer(resp.text):
            quarters.append(m.group(1).lower())

    if not quarters:
        raise SystemExit(
            f"FAIL: no quarterly zips found in DERA index at {DERA_INDEX_URL}"
        )
    quarters = sorted(set(quarters))
    LOG.info("Discovered %d quarters: %s .. %s", len(quarters), quarters[0], quarters[-1])
    return quarters


def _quarter_zip_url(quarter: str) -> str:
    """Derive the DERA zip URL for a quarter string like '2026q1'.

    Real URL pattern (verified 2026-05-18 against sec.gov):
        https://www.sec.gov/files/structureddata/data/form-d-data-sets/2026q1_d.zip
    Older quarters 2008q2..2013q4 (excluding 2008q1 and 2012q1) carry a `_d_0.zip`
    suffix; newer quarters use the clean `_d.zip` form. The 22 `_d_0.zip` quarters
    enumerated by direct inspection of the SEC index page.
    """
    _D0_QUARTERS = {
        "2008q2", "2008q3", "2008q4",
        "2009q1", "2009q2", "2009q3", "2009q4",
        "2010q1", "2010q2", "2010q3", "2010q4",
        "2011q1", "2011q2", "2011q3", "2011q4",
        "2012q2", "2012q3", "2012q4",
        "2013q1", "2013q2", "2013q3", "2013q4",
    }
    base = "https://www.sec.gov/files/structureddata/data/form-d-data-sets"
    if quarter in _D0_QUARTERS:
        return f"{base}/{quarter}_d_0.zip"
    return f"{base}/{quarter}_d.zip"


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------

def transcode_tsv_to_parquet(
    tsv_path: Path,
    parquet_path: Path,
    columns: dict[str, str],
    max_rows: int | None = None,
) -> int:
    """Read TSV with DuckDB, write ZSTD Parquet. Returns row count written."""
    con = duckdb.connect()
    cols_str = str(columns)  # Python dict literal is valid DuckDB syntax

    limit_clause = f"LIMIT {max_rows}" if max_rows else ""
    con.execute(f"""
        COPY (
            SELECT * FROM read_csv(
                '{tsv_path}',
                delim='\\t',
                quote='"',
                escape='"',
                header=TRUE,
                all_varchar=TRUE,
                null_padding=TRUE,
                strict_mode=FALSE,
                columns={cols_str}
            )
            {limit_clause}
        ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
    """)
    count = con.execute(f"SELECT count(*) FROM read_parquet('{parquet_path}')").fetchone()[0]
    con.close()
    return count


# ---------------------------------------------------------------------------
# R2 upload
# ---------------------------------------------------------------------------

def _make_s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def upload_to_r2(s3: Any, parquet_path: Path, release: str, table_name_lower: str) -> str:
    """Upload parquet to R2; return the R2 key."""
    key = f"{R2_PREFIX}/release={release}/{table_name_lower}.parquet"
    s3.upload_file(
        str(parquet_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # NO ContentEncoding (L42)
    )
    return key


# ---------------------------------------------------------------------------
# Per-quarter ingest
# ---------------------------------------------------------------------------

def ingest_quarter(
    quarter: str,
    client: httpx.Client,
    s3: Any,
    conn: psycopg.Connection,
    *,
    apply: bool,
    skip_if_unchanged: bool,
    max_rows: int | None,
    staging_dir: Path,
) -> dict[str, str]:
    """Ingest one quarter. Returns {table_name: status} per table."""
    zip_url = _quarter_zip_url(quarter)
    result: dict[str, str] = {}

    # HEAD-check: capture Last-Modified
    last_modified = _head_last_modified(client, zip_url)
    LOG.info("[%s] Last-Modified: %s", quarter, last_modified)

    # Check skip-if-unchanged per each table
    if skip_if_unchanged and last_modified:
        all_unchanged = True
        for _tsv_name, table_name, _cols in FORM_D_TABLES:
            prior = _get_prior_completed(conn, quarter, table_name)
            if prior is None or last_modified > prior:
                all_unchanged = False
                break
        if all_unchanged:
            LOG.info("[%s] All 6 tables unchanged (Last-Modified <= prior). Skipping.", quarter)
            for _tsv_name, table_name, _cols in FORM_D_TABLES:
                _write_audit_row(
                    conn, quarter, table_name, "no_change",
                    source_observed_at=last_modified,
                    completed_at=datetime.now(timezone.utc),
                )
                result[table_name] = "no_change"
            return result

    if not apply:
        LOG.info("[%s] DRY RUN — would download %s", quarter, zip_url)
        for _tsv_name, table_name, _cols in FORM_D_TABLES:
            result[table_name] = "dry_run"
        return result

    # Write 'running' audit rows
    run_started_at = datetime.now(timezone.utc)
    for _tsv_name, table_name, _cols in FORM_D_TABLES:
        _write_audit_row(
            conn, quarter, table_name, "running",
            source_observed_at=last_modified,
            started_at=run_started_at,
        )

    # Download zip
    LOG.info("[%s] Downloading %s ...", quarter, zip_url)
    try:
        resp = client.get(zip_url)
        resp.raise_for_status()
    except Exception as e:
        LOG.error("[%s] Download failed: %s", quarter, e)
        for _tsv_name, table_name, _cols in FORM_D_TABLES:
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            result[table_name] = "failed"
        return result

    zip_data = resp.content
    LOG.info("[%s] Downloaded %d bytes", quarter, len(zip_data))

    # Extract and transcode each table
    quarter_staging = staging_dir / quarter
    quarter_staging.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            zf.extractall(quarter_staging)
    except Exception as e:
        LOG.error("[%s] Unzip failed: %s", quarter, e)
        for _tsv_name, table_name, _cols in FORM_D_TABLES:
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"unzip: {e}",
            )
            result[table_name] = "failed"
        return result

    for tsv_name, table_name, columns in FORM_D_TABLES:
        # Find the TSV — may be in a subdirectory inside the zip
        tsv_candidates = list(quarter_staging.rglob(tsv_name))
        if not tsv_candidates:
            LOG.warning("[%s] %s not found in zip (skipping)", quarter, tsv_name)
            _write_audit_row(
                conn, quarter, table_name, "skipped",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"{tsv_name} not in zip",
            )
            result[table_name] = "skipped"
            continue

        tsv_path = tsv_candidates[0]
        parquet_path = quarter_staging / f"{table_name}.parquet"

        try:
            rows_written = transcode_tsv_to_parquet(
                tsv_path, parquet_path, columns, max_rows=max_rows
            )
        except Exception as e:
            LOG.error("[%s] %s transcode failed: %s", quarter, tsv_name, e)
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"transcode: {e}",
            )
            result[table_name] = "failed"
            continue

        LOG.info("[%s] %s → %d rows", quarter, tsv_name, rows_written)

        try:
            r2_key = upload_to_r2(s3, parquet_path, quarter, table_name)
        except Exception as e:
            LOG.error("[%s] %s R2 upload failed: %s", quarter, tsv_name, e)
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                rows_written=rows_written,
                error_message=f"upload: {e}",
            )
            result[table_name] = "failed"
            continue

        _write_audit_row(
            conn, quarter, table_name, "completed",
            source_observed_at=last_modified,
            started_at=run_started_at,
            completed_at=datetime.now(timezone.utc),
            rows_written=rows_written,
            r2_key=r2_key,
        )
        result[table_name] = "completed"
        LOG.info("[%s] %s → R2 key=%s rows=%d", quarter, table_name, r2_key, rows_written)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SEC DERA Form D → R2 ingest")
    mode_grp = ap.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--apply", action="store_true", help="Download and upload to R2")
    mode_grp.add_argument("--dry-run", action="store_true", help="Discover quarters only, no download/upload")
    ap.add_argument(
        "--quarters",
        type=str,
        default=None,
        help="Comma-separated subset of quarters to process (e.g. 2026q1,2025q4). "
             "Default: all quarters from DERA index.",
    )
    ap.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        default=False,
        help="Skip quarters where Last-Modified <= prior completed run's source_observed_at.",
    )
    ap.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Limit rows per table (smoke test).",
    )
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "DEX_DB_URL_DIRECT"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            sys.exit(64)

    staging_dir = Path("/tmp/sec-dera-form-d")
    staging_dir.mkdir(parents=True, exist_ok=True)

    apply = args.apply
    dry_run = args.dry_run

    with _make_client() as client:
        quarters = parse_quarters_from_dera_index(client)

        if args.quarters:
            requested = [q.strip().lower() for q in args.quarters.split(",")]
            quarters = [q for q in quarters if q in requested]
            if not quarters:
                LOG.error("FAIL: none of the requested quarters found in DERA index: %s", requested)
                sys.exit(1)
            LOG.info("Scoped to %d quarter(s): %s", len(quarters), quarters)

        if dry_run:
            LOG.info("DRY RUN — %d quarters discovered. Exiting.", len(quarters))
            sys.exit(0)

        s3 = _make_s3_client()
        conn = _db_conn()

        totals: dict[str, int] = {"completed": 0, "failed": 0, "skipped": 0, "no_change": 0, "dry_run": 0}
        for quarter in quarters:
            LOG.info("=" * 60)
            LOG.info("Processing quarter: %s", quarter)
            result = ingest_quarter(
                quarter, client, s3, conn,
                apply=apply,
                skip_if_unchanged=args.skip_if_unchanged,
                max_rows=args.max_rows,
                staging_dir=staging_dir,
            )
            for status in result.values():
                totals[status] = totals.get(status, 0) + 1
            # Brief pause to avoid hammering SEC servers
            time.sleep(1)

        conn.close()
        LOG.info("=" * 60)
        LOG.info("Done. Totals: %s", totals)
        if totals.get("failed", 0) > 0:
            LOG.warning("WARNING: %d table-quarters failed (see ops.sec_dera_form_d_r2_ingest_runs for details)", totals["failed"])


if __name__ == "__main__":
    main()
