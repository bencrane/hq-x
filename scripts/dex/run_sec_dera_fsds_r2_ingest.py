#!/usr/bin/env python3
"""SEC DERA Financial Statement Data Sets (FSDS) → R2 ingest.

Parses quarterly archive index at https://www.sec.gov/dera/data/financial-statement-data-sets.html
to discover all available quarters (do NOT hardcode — DERA re-publishes historical zips
occasionally). For each quarter:
  1. HEAD-check zip URL → capture Last-Modified (RFC 1123 → tz-aware datetime via
     email.utils.parsedate_to_datetime per P3 fix — avoids timezone comparison drift).
  2. Skip-if-unchanged: if Last-Modified <= prior completed (release, table_name)
     run's source_observed_at, write no_change audit rows and continue.
  3. Download zip via L55 User-Agent (Mozilla/5.0).
  4. Unzip into staging dir; expect 4 .txt files (sub.txt, tag.txt, pre.txt, num.txt).
  5. Per table: DuckDB read_csv(..., delim='\\t', all_varchar=TRUE, null_padding=TRUE,
     strict_mode=FALSE, columns=<CANONICAL_*_COLS verified 2026-05-18 from 2026q1 probe>)
     → COPY TO ZSTD Parquet (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000).
  6. boto3 upload to s3://dex-raw-landing-zone/sec-dera/fsds/release=YYYYqQ/<table>.parquet
     with ExtraArgs={"ContentType":"application/x-parquet"} ONLY.
     No extra encoding header (L42 — only ContentType set).
  7. Per (release, table_name): write/update ops.sec_dera_fsds_r2_ingest_runs row
     with L4 status set (pending → running → completed/failed/no_change).

URL pattern (verified L43 probe 2026-05-18):
    https://www.sec.gov/files/dera/data/financial-statement-data-sets/YYYYqQ.zip
NO suffix variants (_d.zip / _d_0.zip) — FSDS zips are simple YYYYqQ.zip throughout.
69 quarters back to 2009q1.

CLI:
  --apply / --dry-run (mutually exclusive)
  --quarters YYYYqQ,YYYYqQ,...     # backfill subset
  --skip-if-unchanged              # default for incremental cadence
  --max-rows N                     # smoke test

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_fsds_r2_ingest.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_fsds_r2_ingest.py --apply --skip-if-unchanged
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_fsds_r2_ingest.py --apply --quarters 2026q1,2025q4
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_dera_fsds_r2_ingest.py --dry-run --quarters 2009q1
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
R2_PREFIX = "sec-dera/fsds"
DERA_INDEX_URL = "https://www.sec.gov/dera/data/financial-statement-data-sets.html"

# ---------------------------------------------------------------------------
# Column dicts — VERIFIED 2026-05-18 from 2026q1 + 2009q1 actual file headers.
# Reviewer downloaded both boundary quarters and read head -1 of each .txt file.
# Schema is stable 2009q1 → 2026q1 (SEC has not added/removed columns since adoption).
# DuckDB read_csv(header=TRUE, columns={dict}, all_varchar=TRUE, null_padding=TRUE,
#                 strict_mode=FALSE) — matching by header NAME not position.
# A column in the file but NOT in dict → SILENTLY DROPPED (data loss).
# A column in dict but NOT in file → added as all-NULL (wasted space, no corruption).
# null_padding=TRUE + strict_mode=FALSE handles optional columns (coreg in pre.txt,
# segments/coreg as mostly-null in num.txt) silently.
# ---------------------------------------------------------------------------

# sub.txt — 36 columns. Per FSDS Documentation §"Submission Data Set (SUB)".
# PK: adsh (SEC accession number, 20-char string, format NNNNNNNNNN-NN-NNNNNN).
CANONICAL_SUB_COLS = {
    "adsh":            "VARCHAR",  # primary key
    "cik":             "VARCHAR",  # CIK as left-zero-padded numeric string
    "name":            "VARCHAR",
    "sic":             "VARCHAR",
    "countryba":       "VARCHAR",  # business-address country
    "stprba":          "VARCHAR",  # state/province of business address
    "cityba":          "VARCHAR",
    "zipba":           "VARCHAR",
    "bas1":            "VARCHAR",  # business-address street 1
    "bas2":            "VARCHAR",
    "baph":            "VARCHAR",  # business-address phone
    "countryma":       "VARCHAR",  # mailing-address country
    "stprma":          "VARCHAR",
    "cityma":          "VARCHAR",
    "zipma":           "VARCHAR",
    "mas1":            "VARCHAR",
    "mas2":            "VARCHAR",
    "countryinc":      "VARCHAR",  # country of incorporation
    "stprinc":         "VARCHAR",  # state/province of incorporation
    "ein":             "VARCHAR",
    "former":          "VARCHAR",  # former name
    "changed":         "VARCHAR",  # date former-name changed
    "afs":             "VARCHAR",  # accelerated-filer status
    "wksi":            "VARCHAR",  # well-known seasoned issuer flag
    "fye":             "VARCHAR",  # fiscal year-end (MMDD)
    "form":            "VARCHAR",  # 10-K / 10-Q / 10-K/A / 10-Q/A / etc.
    "period":          "VARCHAR",  # period-of-report (YYYYMMDD)
    "fy":              "VARCHAR",  # fiscal year
    "fp":              "VARCHAR",  # fiscal period (FY, Q1, Q2, Q3)
    "filed":           "VARCHAR",  # filed date (YYYYMMDD)
    "accepted":        "VARCHAR",  # accepted timestamp (YYYY-MM-DD HH:MM:SS)
    "prevrpt":         "VARCHAR",  # previous-report-pending flag
    "detail":          "VARCHAR",  # detail-level flag (0/1)
    "instance":        "VARCHAR",  # XBRL instance filename
    "nciks":           "VARCHAR",  # number of co-registrant CIKs
    "aciks":           "VARCHAR",  # space-separated additional CIKs
}

# tag.txt — 9 columns. Per FSDS Documentation §"Tag Data Set (TAG)".
# Composite key: (tag, version). Heavy cross-quarter overlap (XBRL tags reused).
CANONICAL_TAG_COLS = {
    "tag":      "VARCHAR",  # XBRL tag name (e.g., 'Assets')
    "version":  "VARCHAR",  # XBRL namespace (e.g., 'us-gaap/2024')
    "custom":   "VARCHAR",  # 1 if custom tag (filer-defined), 0 if standard
    "abstract": "VARCHAR",  # 1 if abstract, 0 if numeric
    "datatype": "VARCHAR",  # XBRL datatype (monetaryItemType / sharesItemType / etc.)
    "iord":     "VARCHAR",  # I (instant) or D (duration)
    "crdr":     "VARCHAR",  # C (credit) or D (debit) — null for non-monetary
    "tlabel":   "VARCHAR",  # primary label
    "doc":      "VARCHAR",  # tag definition / documentation string
}

# pre.txt — 10 columns. Per FSDS Documentation §"Presentation Data Set (PRE)".
# Composite key: (adsh, report, line). FK on (adsh) → sub, (tag, version) → tag.
CANONICAL_PRE_COLS = {
    "adsh":     "VARCHAR",  # FK → sub.adsh
    "report":   "VARCHAR",  # statement number within filing (1..N)
    "line":     "VARCHAR",  # line number within statement
    "stmt":     "VARCHAR",  # BS / IS / CF / EQ / CI / SI / UN  (statement type)
    "inpth":    "VARCHAR",  # 1 if presented parenthetically
    "rfile":    "VARCHAR",  # rendering: H (HTML) or X (XML)
    "tag":      "VARCHAR",  # FK → tag.tag
    "version":  "VARCHAR",  # FK → tag.version
    "plabel":   "VARCHAR",  # preferred label (presentation-context)
    "negating": "VARCHAR",  # 1 if negating label (display sign flip)
}

# num.txt — 10 columns. Per FSDS Documentation §"Number Data Set (NUM)".
# Composite key: (adsh, tag, version, ddate, qtrs, uom, segments, coreg).
# FK on (adsh) → sub. Largest table — ~5M rows/quarter, ~200-350M rows historical.
# CORRECTED by reviewer 2026-05-18 from live 2026q1 + 2009q1 probe (schema stable
# across full historical range): order is adsh→tag→version→ddate→qtrs→uom→segments
# →coreg→value→footnote. Earlier audit draft had `coreg` mis-positioned, omitted
# `segments` (XBRL multi-dimensional context data — would have been SILENTLY
# DROPPED by DuckDB strict_mode=FALSE), and invented a non-existent `footlen`
# column (would have been added as all-NULL).
CANONICAL_NUM_COLS = {
    "adsh":     "VARCHAR",  # FK → sub.adsh
    "tag":      "VARCHAR",  # FK → tag.tag
    "version":  "VARCHAR",  # FK → tag.version
    "ddate":    "VARCHAR",  # date of fact (YYYYMMDD)
    "qtrs":     "VARCHAR",  # number of quarters in duration (0 for instant)
    "uom":      "VARCHAR",  # unit of measure (USD / shares / pure / etc.)
    "segments": "VARCHAR",  # XBRL segment/scenario context (semicolon-encoded dims; mostly null)
    "coreg":    "VARCHAR",  # co-registrant CIK (mostly null)
    "value":    "VARCHAR",  # numeric value (kept VARCHAR per all_varchar=TRUE)
    "footnote": "VARCHAR",  # footnote reference (mostly null)
}

# 4 TSV table definitions: (filename, table_name, canonical_cols_dict)
FSDS_TABLES: tuple[tuple[str, str, dict], ...] = (
    ("sub.txt",  "sub",  CANONICAL_SUB_COLS),
    ("tag.txt",  "tag",  CANONICAL_TAG_COLS),
    ("pre.txt",  "pre",  CANONICAL_PRE_COLS),
    ("num.txt",  "num",  CANONICAL_NUM_COLS),
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
            INSERT INTO ops.sec_dera_fsds_r2_ingest_runs
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
            SELECT source_observed_at FROM ops.sec_dera_fsds_r2_ingest_runs
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
    """HEAD-request url; parse Last-Modified via email.utils.parsedate_to_datetime
    (RFC 1123 → tz-aware datetime in UTC) per P3 fix — avoids timezone comparison drift."""
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
    """Scrape the DERA FSDS index page; return sorted list of quarter strings
    like ['2009q1', '2009q2', ..., '2026q1']. Does NOT hardcode the list."""
    LOG.info("Fetching DERA FSDS index: %s", DERA_INDEX_URL)
    resp = client.get(DERA_INDEX_URL)
    resp.raise_for_status()

    # FSDS URL pattern (verified L43 probe 2026-05-18):
    #   /files/dera/data/financial-statement-data-sets/2026q1.zip
    # NO _d.zip / _d_0.zip suffix variants (unlike Form D).
    # 69 quarters back to 2009q1.
    zip_re = re.compile(r"/(\d{4}q[1-4])\.zip", re.IGNORECASE)

    quarters: list[str] = []

    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "lxml")
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            m = zip_re.search(href)
            if m:
                quarters.append(m.group(1).lower())
    else:
        for m in zip_re.finditer(resp.text):
            quarters.append(m.group(1).lower())

    if not quarters:
        raise SystemExit(
            f"FAIL: no quarterly zips found in DERA FSDS index at {DERA_INDEX_URL}"
        )
    quarters = sorted(set(quarters))
    LOG.info("Discovered %d quarters: %s .. %s", len(quarters), quarters[0], quarters[-1])
    return quarters


def _quarter_zip_url(quarter: str) -> str:
    """Derive the DERA FSDS zip URL for a quarter string like '2026q1'.

    Verified pattern (L43 probe 2026-05-18):
        https://www.sec.gov/files/dera/data/financial-statement-data-sets/YYYYqQ.zip
    NO _d.zip / _d_0.zip suffix variants — FSDS zips are simple YYYYqQ.zip throughout.
    """
    return f"https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------

def transcode_txt_to_parquet(
    txt_path: Path,
    parquet_path: Path,
    columns: dict[str, str],
    max_rows: int | None = None,
) -> int:
    """Read tab-separated .txt with DuckDB, write ZSTD Parquet. Returns row count written."""
    con = duckdb.connect()
    cols_str = str(columns)  # Python dict literal is valid DuckDB syntax

    limit_clause = f"LIMIT {max_rows}" if max_rows else ""
    con.execute(f"""
        COPY (
            SELECT * FROM read_csv(
                '{txt_path}',
                delim='\\t',
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

    # HEAD-check: capture Last-Modified (parsedate_to_datetime → UTC-aware per P3 fix)
    last_modified = _head_last_modified(client, zip_url)
    LOG.info("[%s] Last-Modified: %s", quarter, last_modified)

    # Check skip-if-unchanged per each table
    if skip_if_unchanged and last_modified:
        all_unchanged = True
        for _txt_name, table_name, _cols in FSDS_TABLES:
            prior = _get_prior_completed(conn, quarter, table_name)
            if prior is None or last_modified > prior:
                all_unchanged = False
                break
        if all_unchanged:
            LOG.info("[%s] All 4 tables unchanged (Last-Modified <= prior). Skipping.", quarter)
            for _txt_name, table_name, _cols in FSDS_TABLES:
                _write_audit_row(
                    conn, quarter, table_name, "no_change",
                    source_observed_at=last_modified,
                    completed_at=datetime.now(timezone.utc),
                )
                result[table_name] = "no_change"
            return result

    if not apply:
        LOG.info("[%s] DRY RUN — would download %s", quarter, zip_url)
        for _txt_name, table_name, _cols in FSDS_TABLES:
            result[table_name] = "dry_run"
        return result

    # Write 'running' audit rows
    run_started_at = datetime.now(timezone.utc)
    for _txt_name, table_name, _cols in FSDS_TABLES:
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
        for _txt_name, table_name, _cols in FSDS_TABLES:
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
        for _txt_name, table_name, _cols in FSDS_TABLES:
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"unzip: {e}",
            )
            result[table_name] = "failed"
        return result

    for txt_name, table_name, columns in FSDS_TABLES:
        # Find the .txt file — may be in a subdirectory inside the zip
        txt_candidates = list(quarter_staging.rglob(txt_name))
        if not txt_candidates:
            LOG.warning("[%s] %s not found in zip (skipping)", quarter, txt_name)
            _write_audit_row(
                conn, quarter, table_name, "skipped",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"{txt_name} not in zip",
            )
            result[table_name] = "skipped"
            continue

        txt_path = txt_candidates[0]
        parquet_path = quarter_staging / f"{table_name}.parquet"

        try:
            rows_written = transcode_txt_to_parquet(
                txt_path, parquet_path, columns, max_rows=max_rows
            )
        except Exception as e:
            LOG.error("[%s] %s transcode failed: %s", quarter, txt_name, e)
            _write_audit_row(
                conn, quarter, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"transcode: {e}",
            )
            result[table_name] = "failed"
            continue

        LOG.info("[%s] %s → %d rows", quarter, txt_name, rows_written)

        try:
            r2_key = upload_to_r2(s3, parquet_path, quarter, table_name)
        except Exception as e:
            LOG.error("[%s] %s R2 upload failed: %s", quarter, txt_name, e)
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
    ap = argparse.ArgumentParser(description="SEC DERA FSDS → R2 ingest")
    mode_grp = ap.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--apply", action="store_true", help="Download and upload to R2")
    mode_grp.add_argument("--dry-run", action="store_true", help="Discover quarters only, no download/upload")
    ap.add_argument(
        "--quarters",
        type=str,
        default=None,
        help="Comma-separated subset of quarters to process (e.g. 2026q1,2025q4). "
             "Default: all quarters from DERA FSDS index.",
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

    staging_dir = Path("/tmp/sec-dera-fsds")
    staging_dir.mkdir(parents=True, exist_ok=True)

    apply = args.apply
    dry_run = args.dry_run

    with _make_client() as client:
        quarters = parse_quarters_from_dera_index(client)

        if args.quarters:
            requested = [q.strip().lower() for q in args.quarters.split(",")]
            quarters = [q for q in quarters if q in requested]
            if not quarters:
                LOG.error("FAIL: none of the requested quarters found in DERA FSDS index: %s", requested)
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
            LOG.warning(
                "WARNING: %d table-quarters failed (see ops.sec_dera_fsds_r2_ingest_runs for details)",
                totals["failed"],
            )


if __name__ == "__main__":
    main()
