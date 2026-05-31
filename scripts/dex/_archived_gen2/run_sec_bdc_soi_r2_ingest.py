#!/usr/bin/env python3
"""SEC BDC (Business Development Company) Schedule of Investments → R2 ingest.

Discovers BDC Data Set periods from the SEC landing page
    https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets
and ingests each {period}_bdc.zip into Cloudflare R2 as ZSTD Parquet.

The period series is QUARTERLY 2022q4..2025q1 then MONTHLY 2025_04..present —
SEC switched the BDC Data Sets from quarterly to monthly cadence after 2025q1
(2025q2/q3/q4 zips 404). Discovery tolerates BOTH the {YYYY}q{N} and {YYYY}_{MM}
period forms and skips any HTTP 404 without failing the run.

Each zip ships:
  - soi.tsv               — the pre-pivoted Schedule of Investments report
                            (one row per portfolio investment). Column count
                            varies by period (51 monthly delta, 213 quarterly
                            snapshot) — read EVERY column dynamically, NO pinned
                            column dict (L56 / directive §"Goal restated").
  - datasets/<table>.tsv  — BDC-filtered FSNDS-style tables: sub tag num pre
                            cal txt non.

Per period:
  1. HEAD-check the zip URL → capture Last-Modified (RFC 1123 → tz-aware UTC via
     email.utils.parsedate_to_datetime — avoids timezone comparison drift).
  2. Skip-if-unchanged: if Last-Modified <= the prior completed/no_change run's
     source_observed_at for every table, write no_change audit rows and continue.
  3. Download {period}_bdc.zip via L55 User-Agent (Mozilla/5.0).
  4. Unzip; transcode soi.tsv + every datasets/*.tsv → ZSTD Parquet via DuckDB
     read_csv(all_varchar=TRUE, header=TRUE, null_padding=TRUE, strict_mode=FALSE)
     with NO pinned column dict (L56).
  5. boto3 upload to s3://dex-raw-landing-zone/sec-bdc/<table>/release=<period>/
     data.parquet with ExtraArgs={"ContentType":"application/x-parquet"} ONLY —
     no Content-Encoding header (L42).
  6. Per (release, table_name): write ops.sec_bdc_soi_ingest_runs row with the
     L4 status set (pending → running → completed/failed/no_change).

Pattern reference: scripts/run_sec_dera_fsds_r2_ingest.py.

CLI:
  --apply / --dry-run (mutually exclusive)
  --periods 2025q1,2026_04,...     # backfill subset
  --skip-if-unchanged              # default for incremental cadence
  --max-rows N                     # smoke test

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_bdc_soi_r2_ingest.py --apply
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_bdc_soi_r2_ingest.py --apply --skip-if-unchanged
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_sec_bdc_soi_r2_ingest.py --dry-run
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
R2_PREFIX = "sec-bdc"
BDC_LANDING_URL = "https://www.sec.gov/data-research/sec-markets-data/bdc-data-sets"
BDC_ZIP_BASE = (
    "https://www.sec.gov/files/structureddata/data/"
    "business-development-company-bdc-data-sets"
)

# datasets/<name>.tsv tables shipped inside each BDC zip, alongside the
# root-level soi.tsv. The column count is NOT pinned — DuckDB read_csv reads
# every header column dynamically (L56). "soi" is the pre-pivoted Schedule of
# Investments report; the rest are BDC-filtered FSNDS-style tables.
DATASET_TABLES: tuple[str, ...] = (
    "sub", "tag", "num", "pre", "cal", "txt", "non",
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
            INSERT INTO ops.sec_bdc_soi_ingest_runs
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
            SELECT source_observed_at FROM ops.sec_bdc_soi_ingest_runs
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
        timeout=180.0,
    )


def _head_last_modified(client: httpx.Client, url: str) -> datetime | None:
    """HEAD-request url; parse Last-Modified via email.utils.parsedate_to_datetime
    (RFC 1123 → tz-aware datetime in UTC) — avoids timezone comparison drift."""
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
# Period discovery
# ---------------------------------------------------------------------------

def parse_periods_from_landing(client: httpx.Client) -> list[str]:
    """Scrape the BDC Data Sets landing page; return a sorted list of period
    strings. Tolerates BOTH the quarterly {YYYY}q{N} and monthly {YYYY}_{MM}
    forms — SEC switched the series from quarterly to monthly after 2025q1.
    Does NOT hardcode the list (FSDS-style discovery)."""
    LOG.info("Fetching BDC Data Sets landing page: %s", BDC_LANDING_URL)
    resp = client.get(BDC_LANDING_URL)
    resp.raise_for_status()

    # Zip URL pattern on the landing page:
    #   /files/structureddata/data/business-development-company-bdc-data-sets/
    #   {period}_bdc.zip  where {period} is {YYYY}q{N} OR {YYYY}_{MM}.
    zip_re = re.compile(
        r"business-development-company-bdc-data-sets/"
        r"(\d{4}(?:q[1-4]|_\d{2}))_bdc\.zip",
        re.IGNORECASE,
    )

    periods: list[str] = []
    if BeautifulSoup is not None:
        soup = BeautifulSoup(resp.text, "lxml")
        for link in soup.find_all("a", href=True):
            m = zip_re.search(link.get("href", ""))
            if m:
                periods.append(m.group(1).lower())
    if not periods:
        # fall back to a raw-text scan (covers no-bs4 + non-anchor refs)
        for m in zip_re.finditer(resp.text):
            periods.append(m.group(1).lower())

    if not periods:
        raise SystemExit(
            f"FAIL: no BDC zips found on landing page at {BDC_LANDING_URL}"
        )
    periods = sorted(set(periods))
    LOG.info(
        "Discovered %d periods: %s .. %s", len(periods), periods[0], periods[-1],
    )
    return periods


def _period_zip_url(period: str) -> str:
    """Derive the BDC Data Set zip URL for a period string (e.g. '2025q1' or
    '2026_04')."""
    return f"{BDC_ZIP_BASE}/{period}_bdc.zip"


# ---------------------------------------------------------------------------
# Transcode
# ---------------------------------------------------------------------------

def transcode_tsv_to_parquet(
    tsv_path: Path,
    parquet_path: Path,
    max_rows: int | None = None,
) -> int:
    """Read a tab-separated .tsv with DuckDB and write ZSTD Parquet.

    NO pinned column dict — read_csv reads every header column dynamically
    (all_varchar=TRUE), so the 51↔213 column drift between monthly deltas and
    quarterly snapshots is absorbed without data loss (L56). Returns the row
    count written.
    """
    con = duckdb.connect()
    limit_clause = f"LIMIT {max_rows}" if max_rows else ""
    con.execute(f"""
        COPY (
            SELECT * FROM read_csv(
                '{tsv_path}',
                delim='\\t',
                header=TRUE,
                all_varchar=TRUE,
                null_padding=TRUE,
                strict_mode=FALSE
            )
            {limit_clause}
        ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
    """)
    count = con.execute(
        f"SELECT count(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
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


def upload_to_r2(s3: Any, parquet_path: Path, release: str, table_name: str) -> str:
    """Upload parquet to R2 sec-bdc/<table>/release=<period>/data.parquet;
    return the R2 key. ContentType only — no Content-Encoding (L42)."""
    key = f"{R2_PREFIX}/{table_name}/release={release}/data.parquet"
    s3.upload_file(
        str(parquet_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # NO ContentEncoding (L42)
    )
    return key


# ---------------------------------------------------------------------------
# Per-period ingest
# ---------------------------------------------------------------------------

def _expected_tables() -> list[str]:
    """soi (root) + the datasets/ tables."""
    return ["soi", *DATASET_TABLES]


def ingest_period(
    period: str,
    client: httpx.Client,
    s3: Any,
    conn: psycopg.Connection,
    *,
    apply: bool,
    skip_if_unchanged: bool,
    max_rows: int | None,
    staging_dir: Path,
) -> dict[str, str]:
    """Ingest one BDC period. Returns {table_name: status} per table."""
    zip_url = _period_zip_url(period)
    result: dict[str, str] = {}
    tables = _expected_tables()

    # HEAD-check: capture Last-Modified. A 404 (e.g. 2025q2/q3/q4) yields None;
    # the run continues without failing (directive §"Period enumeration").
    last_modified = _head_last_modified(client, zip_url)
    LOG.info("[%s] Last-Modified: %s", period, last_modified)

    # skip-if-unchanged: unchanged iff every table's prior completed/no_change
    # run's source_observed_at is >= the current Last-Modified.
    if skip_if_unchanged and last_modified:
        all_unchanged = True
        for table_name in tables:
            prior = _get_prior_completed(conn, period, table_name)
            if prior is None or last_modified > prior:
                all_unchanged = False
                break
        if all_unchanged:
            LOG.info("[%s] All tables unchanged (Last-Modified <= prior). Skipping.", period)
            for table_name in tables:
                _write_audit_row(
                    conn, period, table_name, "no_change",
                    source_observed_at=last_modified,
                    completed_at=datetime.now(timezone.utc),
                )
                result[table_name] = "no_change"
            return result

    if not apply:
        LOG.info("[%s] DRY RUN — would download %s", period, zip_url)
        for table_name in tables:
            result[table_name] = "dry_run"
        return result

    # Write 'running' audit rows.
    run_started_at = datetime.now(timezone.utc)
    for table_name in tables:
        _write_audit_row(
            conn, period, table_name, "running",
            source_observed_at=last_modified,
            started_at=run_started_at,
        )

    # Download zip.
    LOG.info("[%s] Downloading %s ...", period, zip_url)
    try:
        resp = client.get(zip_url)
        resp.raise_for_status()
    except Exception as e:
        LOG.error("[%s] Download failed: %s", period, e)
        for table_name in tables:
            _write_audit_row(
                conn, period, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=str(e),
            )
            result[table_name] = "failed"
        return result

    zip_data = resp.content
    LOG.info("[%s] Downloaded %d bytes", period, len(zip_data))

    # Extract. SEC builds the BDC zips on Windows, so the datasets/ table
    # entries carry a BACKSLASH separator in their archive names
    # (`datasets\txt.tsv`). zipfile.extractall() does NOT translate `\` → `/`,
    # so it would write a literal-backslash filename in the period root and
    # the per-table rglob below would never match. Extract entry-by-entry,
    # normalizing the separator so `datasets\txt.tsv` lands at
    # `<period>/datasets/txt.tsv`.
    period_staging = staging_dir / period
    period_staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            for info in zf.infolist():
                norm = info.filename.replace("\\", "/")
                if norm.endswith("/"):
                    continue  # directory entry
                dest = period_staging / norm
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as out:
                    out.write(src.read())
    except Exception as e:
        LOG.error("[%s] Unzip failed: %s", period, e)
        for table_name in tables:
            _write_audit_row(
                conn, period, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"unzip: {e}",
            )
            result[table_name] = "failed"
        return result

    # Transcode + upload each table. soi.tsv is at the zip root; the rest live
    # under datasets/. rglob tolerates either placement.
    for table_name in tables:
        tsv_name = f"{table_name}.tsv"
        tsv_candidates = list(period_staging.rglob(tsv_name))
        if not tsv_candidates:
            LOG.warning("[%s] %s not found in zip (skipping)", period, tsv_name)
            _write_audit_row(
                conn, period, table_name, "skipped",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"{tsv_name} not in zip",
            )
            result[table_name] = "skipped"
            continue

        tsv_path = tsv_candidates[0]
        parquet_path = period_staging / f"{table_name}.parquet"

        try:
            rows_written = transcode_tsv_to_parquet(
                tsv_path, parquet_path, max_rows=max_rows,
            )
        except Exception as e:
            LOG.error("[%s] %s transcode failed: %s", period, tsv_name, e)
            _write_audit_row(
                conn, period, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                error_message=f"transcode: {e}",
            )
            result[table_name] = "failed"
            continue

        LOG.info("[%s] %s → %d rows", period, tsv_name, rows_written)

        try:
            r2_key = upload_to_r2(s3, parquet_path, period, table_name)
        except Exception as e:
            LOG.error("[%s] %s R2 upload failed: %s", period, tsv_name, e)
            _write_audit_row(
                conn, period, table_name, "failed",
                source_observed_at=last_modified,
                started_at=run_started_at,
                completed_at=datetime.now(timezone.utc),
                rows_written=rows_written,
                error_message=f"upload: {e}",
            )
            result[table_name] = "failed"
            continue

        _write_audit_row(
            conn, period, table_name, "completed",
            source_observed_at=last_modified,
            started_at=run_started_at,
            completed_at=datetime.now(timezone.utc),
            rows_written=rows_written,
            r2_key=r2_key,
        )
        result[table_name] = "completed"
        LOG.info("[%s] %s → R2 key=%s rows=%d", period, table_name, r2_key, rows_written)

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="SEC BDC Schedule of Investments → R2 ingest")
    mode_grp = ap.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--apply", action="store_true", help="Download and upload to R2")
    mode_grp.add_argument(
        "--dry-run", action="store_true",
        help="Discover periods only; no download/upload",
    )
    ap.add_argument(
        "--periods",
        type=str,
        default=None,
        help="Comma-separated subset of periods to process (e.g. 2025q1,2026_04). "
             "Default: all periods discovered from the BDC landing page.",
    )
    ap.add_argument(
        "--skip-if-unchanged",
        action="store_true",
        default=False,
        help="Skip periods where Last-Modified <= the prior completed run's "
             "source_observed_at.",
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

    staging_dir = Path("/tmp/sec-bdc-soi")
    staging_dir.mkdir(parents=True, exist_ok=True)

    with _make_client() as client:
        periods = parse_periods_from_landing(client)

        if args.periods:
            requested = [p.strip().lower() for p in args.periods.split(",")]
            periods = [p for p in periods if p in requested]
            if not periods:
                LOG.error(
                    "FAIL: none of the requested periods found on the BDC "
                    "landing page: %s", requested,
                )
                sys.exit(1)
            LOG.info("Scoped to %d period(s): %s", len(periods), periods)

        if args.dry_run:
            LOG.info("DRY RUN — %d periods discovered. Exiting.", len(periods))
            sys.exit(0)

        s3 = _make_s3_client()
        conn = _db_conn()

        totals: dict[str, int] = {
            "completed": 0, "failed": 0, "skipped": 0, "no_change": 0, "dry_run": 0,
        }
        for period in periods:
            LOG.info("=" * 60)
            LOG.info("Processing period: %s", period)
            result = ingest_period(
                period, client, s3, conn,
                apply=args.apply,
                skip_if_unchanged=args.skip_if_unchanged,
                max_rows=args.max_rows,
                staging_dir=staging_dir,
            )
            for status in result.values():
                totals[status] = totals.get(status, 0) + 1
            time.sleep(1)  # be polite to SEC servers

        conn.close()
        LOG.info("=" * 60)
        LOG.info("Done. Totals: %s", totals)
        if totals.get("failed", 0) > 0:
            LOG.warning(
                "WARNING: %d table-periods failed (see ops.sec_bdc_soi_ingest_runs)",
                totals["failed"],
            )


if __name__ == "__main__":
    main()
