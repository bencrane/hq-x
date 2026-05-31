"""Arizona Procurement Portal (APP) — Public RFP Browse → R2 ZSTD Parquet ingest.

Scrapes the Ivalua public-RFP browse table at app.az.gov, iterating all pages via
the AJAX endpoint (CSRFToken-protected form POST), writes ZSTD Parquet snapshot
to Cloudflare R2, and logs each run to ops.az_app_rfp_public_ingest_runs.

R2 layout (kebab-case per CLAUDE.md):
  s3://dex-raw-landing-zone/azstate/app-rfp-public/snapshot={YYYY-MM-DD}/data.parquet

Source shape (per 2026-05-19 probe):
  Server-rendered HTML grid at /page.aspx/en/rfp/request_browse_public.
  Client-side AJAX pagination at /ajax.aspx/en/rfp/request_browse_public.
  ~150 active RFPs / IFBs / sole-source notices, 15 rows per page, 10 pages max.
  12 visible columns: Editing column, Code, Label, Publication begin date (UTC-7),
  Commodity, Agency, Publication end date (UTC-7), Status, RFx Awarded,
  Remaining time, Begin (UTC-7), End (UTC-7). We surface 12 + rfp_id (from href URL).
  Natural PK: code (BPM number, e.g. BPM007599) — verified 150/150 unique.

Pagination contract:
  1. GET https://app.az.gov/page.aspx/en/rfp/request_browse_public to seed session
     cookies and extract CSRFToken + maxpageindex + rowcount.
  2. For page N (0-indexed) > 0, POST /ajax.aspx/en/rfp/request_browse_public with:
       CSRFToken=<from form>
       hdnCurrentPageIndexbody_x_grid_grd=N
       hdnRowCountbody_x_grid_grd=<total>
       maxpageindexbody_x_grid_grd=<max>
       ajaxrowsiscountedbody_x_grid_grd=False
       (X-Requested-With: XMLHttpRequest)
  3. Response is HTML containing the refreshed grid table at table#body_x_grid_grd.
  4. Refresh CSRFToken from each response before the next POST.

All-VARCHAR write per L9 (pandas dtype=str). Idempotent on snapshot partition.

Usage:
    cd ~/hq-all/apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_az_app_rfp_public_to_r2.py \\
        [--snapshot-date YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path

import boto3
import pandas as pd
import psycopg2
import pyarrow as pa
import pyarrow.parquet as pq
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ── load-bearing constants (verify harness greps for these) ─────────────────

BROWSE_URL = "https://app.az.gov/page.aspx/en/rfp/request_browse_public"
AJAX_URL   = "https://app.az.gov/ajax.aspx/en/rfp/request_browse_public"

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "azstate/app-rfp-public"

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Target column names (snake_case) for the 13 columns we emit
# (12 source columns + rfp_id extracted from the row's edit-link href).
_COLUMNS = [
    "rfp_id",
    "code",
    "label",
    "publication_begin_date_utc7",
    "commodity",
    "agency",
    "publication_end_date_utc7",
    "status",
    "rfx_awarded",
    "remaining_time",
    "begin_utc7",
    "end_utc7",
    "detail_url",
]

_RFP_ID_RE = re.compile(r"/process_manage_extranet/(\d+)$")


# ── helpers ──────────────────────────────────────────────────────────────────

def _dedup_repeated(text: str) -> str:
    """Strip the 'XX' → 'X' duplication that BeautifulSoup get_text often
    yields when a cell has both a span and its tooltip emitting the same
    string. E.g. 'City of ChandlerCity of Chandler' → 'City of Chandler'.

    Conservative: only dedups when text exactly bisects into two equal halves.
    """
    if not text:
        return text
    n = len(text)
    if n % 2 == 0 and text[: n // 2] == text[n // 2 :]:
        return text[: n // 2]
    return text


def _pg_conn():
    return psycopg2.connect(os.environ["DEX_DB_URL_DIRECT"])


def _record_run_start(conn, snapshot_date: datetime.date) -> str:
    run_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.az_app_rfp_public_ingest_runs
                (ingest_run_id, snapshot_date, started_at, status)
            VALUES (%s, %s, now(), 'running')
            """,
            (run_id, snapshot_date),
        )
    conn.commit()
    logger.info("started run %s snapshot=%s", run_id, snapshot_date)
    return run_id


def _record_run_complete(conn, run_id: str, rows_ingested: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.az_app_rfp_public_ingest_runs
               SET status = 'completed', completed_at = now(), rows_ingested = %s
             WHERE ingest_run_id = %s
            """,
            (rows_ingested, run_id),
        )
    conn.commit()
    logger.info("completed run %s rows=%d", run_id, rows_ingested)


def _record_run_failed(conn, run_id: str, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.az_app_rfp_public_ingest_runs
               SET status = 'failed', completed_at = now(), error_message = %s
             WHERE ingest_run_id = %s
            """,
            (error_message[:2000], run_id),
        )
    conn.commit()
    logger.error("failed run %s: %s", run_id, error_message[:200])


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _parse_grid_rows(html: str) -> tuple[list[list[str]], dict]:
    """Parse the body_x_grid_grd table from a page response. Returns
    (rows_excluding_header, meta) where meta carries CSRFToken + pagination
    hidden-field values for the next POST."""
    soup = BeautifulSoup(html, "html.parser")
    grd = soup.find("table", id="body_x_grid_grd")
    if not grd:
        raise RuntimeError("body_x_grid_grd table missing from response")
    rows = grd.find_all("tr")
    parsed: list[list[str]] = []
    for tr in rows[1:]:  # skip header row
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        edit_a = tr.find("a", href=_RFP_ID_RE)
        rfp_id = ""
        detail_url = ""
        if edit_a:
            m = _RFP_ID_RE.search(edit_a["href"])
            if m:
                rfp_id = m.group(1)
                detail_url = "https://app.az.gov" + edit_a["href"]
        cell_texts = [_dedup_repeated(c.get_text(strip=True)) for c in cells]
        # drop the editing-column (cell 0); we already pulled rfp_id from its href
        cell_texts = cell_texts[1:]
        # pad to 11 source-cell columns
        while len(cell_texts) < 11:
            cell_texts.append("")
        cell_texts = cell_texts[:11]
        parsed.append([rfp_id, *cell_texts, detail_url])

    meta = {}
    for field in (
        "CSRFToken",
        "hdnCurrentPageIndexbody_x_grid_grd",
        "hdnRowCountbody_x_grid_grd",
        "maxpageindexbody_x_grid_grd",
    ):
        el = soup.find("input", id=field)
        if el:
            meta[field] = el.get("value", "")
    return parsed, meta


def ingest(snapshot_date: datetime.date) -> None:
    """Iterate all pages of the AZ APP public-RFP grid, write ZSTD Parquet to R2."""
    conn = _pg_conn()
    run_id = _record_run_start(conn, snapshot_date)
    local_parquet = None
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": _USER_AGENT})

        logger.info("GET initial page %s", BROWSE_URL)
        r0 = session.get(BROWSE_URL, timeout=60)
        r0.raise_for_status()

        page0_rows, meta = _parse_grid_rows(r0.text)
        csrf = meta.get("CSRFToken", "")
        row_count_total = meta.get("hdnRowCountbody_x_grid_grd", "0")
        max_page_index = int(meta.get("maxpageindexbody_x_grid_grd", "0") or "0")
        logger.info(
            "page 0: %d rows (total=%s, max_page_index=%d, csrf=%s...)",
            len(page0_rows), row_count_total, max_page_index, csrf[:12],
        )

        all_rows: list[list[str]] = []
        all_rows.extend(page0_rows)

        for page_idx in range(1, max_page_index + 1):
            payload = {
                "CSRFToken": csrf,
                "hdnCurrentPageIndexbody_x_grid_grd": str(page_idx),
                "hdnRowCountbody_x_grid_grd": row_count_total,
                "maxpageindexbody_x_grid_grd": str(max_page_index),
                "ajaxrowsiscountedbody_x_grid_grd": "False",
                "hdnSortExpressionbody_x_grid_grd": "",
                "hdnSortDirectionbody_x_grid_grd": "",
                "REQUEST_METHOD": "GET",
            }
            rr = session.post(
                AJAX_URL,
                data=payload,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": BROWSE_URL,
                },
                timeout=60,
            )
            rr.raise_for_status()
            page_rows, page_meta = _parse_grid_rows(rr.text)
            all_rows.extend(page_rows)
            # refresh CSRFToken for the next POST
            csrf = page_meta.get("CSRFToken", csrf)
            logger.info(
                "page %d: %d rows (cumulative %d)",
                page_idx, len(page_rows), len(all_rows),
            )

        if not all_rows:
            raise RuntimeError("scraped 0 rows from AZ APP — aborting")

        df = pd.DataFrame(all_rows, columns=_COLUMNS)
        # All-VARCHAR per L9
        df = df.astype(str).where(df.notna(), None)
        # Replace literal "None" / empty strings with None for proper NULL on the Parquet side
        df = df.replace({"None": None, "": None})

        logger.info("scrape complete: %d rows × %d cols", len(df), len(df.columns))

        with tempfile.NamedTemporaryFile(suffix=".parquet", delete=False) as tmp:
            local_parquet = tmp.name

        table = pa.Table.from_pandas(df, preserve_index=False)
        pq.write_table(
            table, local_parquet,
            compression="ZSTD",
            compression_level=9,
        )

        r2_key = f"{R2_PREFIX}/snapshot={snapshot_date}/data.parquet"
        s3 = _r2_client()
        s3.upload_file(
            local_parquet, R2_BUCKET, r2_key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        logger.info("uploaded AZ APP → s3://%s/%s", R2_BUCKET, r2_key)

        _record_run_complete(conn, run_id, len(df))
    except Exception as exc:
        _record_run_failed(conn, run_id, str(exc))
        raise
    finally:
        conn.close()
        if local_parquet and Path(local_parquet).exists():
            Path(local_parquet).unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Arizona APP Public RFP Browse → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--snapshot-date",
        default=None,
        help="Snapshot date YYYY-MM-DD (default: today UTC)",
    )
    args = parser.parse_args()

    if args.snapshot_date:
        snapshot_date = datetime.date.fromisoformat(args.snapshot_date)
    else:
        snapshot_date = datetime.datetime.now(datetime.timezone.utc).date()

    logger.info("snapshot_date=%s", snapshot_date)
    ingest(snapshot_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
