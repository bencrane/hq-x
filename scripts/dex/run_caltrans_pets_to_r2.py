"""CalTrans PETS (Major Construction Payment & Information System) — R2 ingest.

Fetches all construction contract payment records from the CalTrans PETS system at
misc-external.dot.ca.gov/pets/ and writes a ZSTD Parquet snapshot to Cloudflare R2.

Source:
    https://misc-external.dot.ca.gov/pets/ctnums.php  — 14,185 contract-number anchors
    https://misc-external.dot.ca.gov/pets/ctors.php   — contractor-name → contracts map
    https://misc-external.dot.ca.gov/pets/results.php?ctrnm=<C>  — per-contract detail page

Heading regex (p2 / validator-corrected): H1 class="rpt77tx", NOT H3.
    <h1 class="rpt77tx">Contract: <CN><br />Contractor Name</h1>
    HTML-unescape the contractor name captured in group 2.

Voucher column disambiguation (p11 / validator-corrected):
    Payment table has columns: Released | Held | Disbursed.
    Held = retention withheld. Disbursed = net payment (last rpt77amt cell per row).
    total_disbursed_amount_dollars = sum of Disbursed (3rd/last amount cell).
    total_gross_amount_dollars = sum of Released (1st amount cell, optional).
    DO NOT sum "Held" alone — that would capture only the withheld fraction.

Date proxy (p10 / validator note):
    PETS does NOT publish award_date directly. earliest_voucher_date is used as a
    proxy with ~30-90 day lag from actual award date (warrants issued after work begins).
    Column is named earliest_voucher_date (NOT award_date) to preserve proxy semantics.

R2 output (L42):
    s3://dex-raw-landing-zone/caltrans/awards-pets/snapshot=YYYY-MM-DD/data.parquet
    ZSTD compression; ExtraArgs={"ContentType": "application/x-parquet"} ONLY.
    Plain .parquet extension (Hive-partitioned prefix).

Audit ledger:
    ops.caltrans_r2_ingest_runs — reused from PR #499 (NOT a new table).
    INSERT 'running' at start; UPDATE to terminal state (completed/failed/no_change)
    per PR #499 fix #1 to avoid PK UniqueViolation on re-run of same run_id.

Rate-limiting (p1 / validator prediction):
    ThreadPoolExecutor(max_workers=10) + 150ms sleep per worker between requests.
    Retry-with-exponential-backoff (1s, 2s, 4s, 8s) on 429/503/ConnectionError/timeout.
    Honors Retry-After header on 429/503. If bulk connection-reset occurs, fall back to
    max_workers=5 via restartable checkpoint (completed ctrnms tracked in /tmp/pets_checkpoint.json).

CLI:
    --apply   live mode (writes R2 + audit ledger row)
    no args   dry-run on first 50 contracts (no R2 write, no ledger row beyond initial)

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_caltrans_pets_to_r2.py --apply
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import psycopg
import pyarrow as pa
import pyarrow.parquet as pq
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.entity_name_normalize import (
    __version__ as NORMALIZER_VERSION,
    normalize_entity_name,
)

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ---- constants ---------------------------------------------------------------

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_TEMPLATE = "caltrans/awards-pets/snapshot={date}/data.parquet"
SOURCE_URL_BASE = "https://misc-external.dot.ca.gov/pets/"
CTNUMS_URL = "https://misc-external.dot.ca.gov/pets/ctnums.php"
CTORS_URL = "https://misc-external.dot.ca.gov/pets/ctors.php"
RESULTS_URL_TEMPLATE = "https://misc-external.dot.ca.gov/pets/results.php?ctrnm={ctrnm}"
USER_AGENT = "data-engine-x-caltrans-pets/1.0 (substrate-research)"  # p1 polite UA

MAX_WORKERS = 10  # p1: max_workers <= 10 (validator prediction p1)
PER_WORKER_SLEEP_S = 0.15   # p1: 150ms per-worker sleep for politeness
RETRY_AFTER_DEFAULT_S = 2
MAX_RETRIES = 4

TMP_PARQUET = "/tmp/caltrans-awards-pets.parquet"
CHECKPOINT_FILE = "/tmp/pets_checkpoint.json"  # p1: restartable checkpoint

# p2: H1 class="rpt77tx" heading regex (NOT H3)
# Captures group 1 = contract_number, group 2 = contractor_name (HTML-unescaped)
HEADING_REGEX = re.compile(
    r'<h1\s+class="rpt77tx"[^>]*>\s*Contract:\s*([A-Z0-9]+)\s*<br[^>]*/?\s*>\s*([^<]+?)\s*</h1>',
    re.IGNORECASE,
)

# p11: rpt77amt cells per voucher row — the Disbursed (net) amount is the 3rd/last
# amount cell (class rpt77amt). The table has: Released | Held | Disbursed.
# We extract all <td class="rpt77amt"> per row and take the last one for Disbursed.
ROW_REGEX = re.compile(r'<tr[^>]*>(.*?)</tr>', re.DOTALL | re.IGNORECASE)
CELL_REGEX = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
AMT_CELL_REGEX = re.compile(r'<td[^>]*class="rpt77amt"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)
DATE_CELL_REGEX = re.compile(r'<td[^>]*class="rpt77dat"[^>]*>(.*?)</td>', re.DOTALL | re.IGNORECASE)

# Anchor regex for ctrnm= parameter in ctnums.php
CTRNM_REGEX = re.compile(r'<a[^>]+href\s*=\s*["\'][^"\']*results\.php\?ctrnm=([a-zA-Z0-9]+)', re.IGNORECASE)

# Contractor block in ctors.php: <div class="rpt77b">Name</div> followed by anchors
CTORS_NAME_REGEX = re.compile(r'<div\s+class="rpt77b">([^<]+)</div>', re.IGNORECASE)
CTORS_LINK_REGEX = re.compile(r'<a[^>]+ctrnm=([a-zA-Z0-9]+)', re.IGNORECASE)

# Strip HTML tags helper
TAG_STRIP_REGEX = re.compile(r'<[^>]+>')


def _strip_html(s: str) -> str:
    return html.unescape(TAG_STRIP_REGEX.sub('', s).strip())


# ---- helpers -----------------------------------------------------------------

def _storage_options() -> dict:
    return {
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "endpoint_url": os.environ["R2_ENDPOINT"],
        "region_name": "us-east-1",
    }


def _db_conn():
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT not set")
    return psycopg.connect(url)


def _make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})  # p1 polite UA
    return session


def _fetch_with_retry(session: requests.Session, url: str, timeout: int = 30) -> requests.Response:
    """Fetch URL with exponential backoff + Retry-After honoring (p1)."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt < MAX_RETRIES:
                # exponential backoff: 1s, 2s, 4s, 8s
                wait = 2 ** attempt
                logger.warning("network error %s on %s; backoff %ds (attempt %d)", exc, url, wait, attempt)
                time.sleep(wait)
                continue
            raise

        if resp.status_code in (429, 503):
            # Honor Retry-After header (p1)
            retry_after = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT_S * (2 ** attempt)))
            logger.warning(
                "HTTP %d on %s; Retry-After=%ds (attempt %d)",
                resp.status_code, url, retry_after, attempt,
            )
            if attempt < MAX_RETRIES:
                time.sleep(retry_after)
                continue
            resp.raise_for_status()

        resp.raise_for_status()
        return resp

    raise RuntimeError(f"unreachable — failed after {MAX_RETRIES} retries on {url}")


def _parse_ctnums(html_content: str) -> list[str]:
    """Parse ctnums.php → flat list of contract numbers (uppercase, non-test)."""
    matches = CTRNM_REGEX.findall(html_content)
    # Dedup preserving order; filter out test* entries
    seen = set()
    result = []
    for m in matches:
        upper = m.upper()
        if upper in seen:
            continue
        seen.add(upper)
        # Filter test contract numbers (p1 directive: "Filter out test* entries")
        if upper.lower().startswith("te"):
            continue
        result.append(upper)
    return result


def _parse_ctors(html_content: str) -> dict[str, str]:
    """Parse ctors.php → contractor_name → contract_number mapping (fallback lookup)."""
    # We scan for <div class="rpt77b">NAME</div> then all subsequent <a href=...ctrnm=C>
    # until the next <div class="rpt77b">
    contractor_map: dict[str, str] = {}  # ctrnm -> contractor_name
    segments = re.split(r'<div\s+class="rpt77b">', html_content, flags=re.IGNORECASE)
    for seg in segments[1:]:
        # Extract name from beginning until first </div>
        name_end = seg.find('</div>')
        if name_end < 0:
            continue
        name_raw = seg[:name_end]
        name = _strip_html(name_raw).strip()
        if not name:
            continue
        # Extract all ctrnm links in this segment
        links = CTORS_LINK_REGEX.findall(seg)
        for ctrnm in links:
            contractor_map[ctrnm.upper()] = name
    return contractor_map


def _parse_amount(raw: str) -> float | None:
    """Parse a dollar amount string: strip $, commas, parens; return float or None."""
    if not raw:
        return None
    cleaned = _strip_html(raw).replace("$", "").replace(",", "").strip()
    # Handle parenthetical negatives like (1,234.56) -> -1234.56
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_date(raw: str) -> str | None:
    """Parse MM/DD/YY or MM/DD/YYYY date string to ISO YYYY-MM-DD or None."""
    s = _strip_html(raw).strip()
    if not s:
        return None
    # Try MM/DD/YY and MM/DD/YYYY
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


def _parse_contract_page(html_content: str, ctrnm: str, contractor_fallback: str) -> dict[str, Any]:
    """Parse a single results.php page into a contract record.

    p2: Use H1 class=rpt77tx heading (NOT H3).
    p11: Disbursed = last rpt77amt cell per row (3rd column = net payment).
         total_disbursed_amount_dollars is the correct net-payment aggregate.
    p10: earliest_voucher_date is a PROXY for award_date (~30-90 day lag).
         Column is NOT renamed to award_date — proxy semantics preserved in name.
    """
    # p2 heading parse
    heading_match = HEADING_REGEX.search(html_content)
    if heading_match:
        winning_contractor_name = html.unescape(heading_match.group(2)).strip()
    else:
        # Fallback to ctors.php cross-reference (p2 validator prediction)
        winning_contractor_name = contractor_fallback or ""

    # p11: voucher table parsing — find all <tr> rows containing rpt77amt cells
    # Table columns per validator probe: Released | Held | Disbursed
    # Released = gross, Held = retention, Disbursed = net (3rd/last rpt77amt cell)
    voucher_dates: list[str] = []
    disbursed_amounts: list[float] = []
    gross_amounts: list[float] = []

    # Find table rows
    rows = ROW_REGEX.findall(html_content)
    for row_html in rows:
        # p11: extract amount cells by class rpt77amt
        amt_cells = AMT_CELL_REGEX.findall(row_html)
        if len(amt_cells) < 1:
            continue

        # p11: Disbursed is the LAST rpt77amt cell per row (3rd column = net)
        disbursed_raw = amt_cells[-1] if amt_cells else ""
        gross_raw = amt_cells[0] if amt_cells else ""  # Released = first

        disbursed = _parse_amount(disbursed_raw)
        gross = _parse_amount(gross_raw)

        if disbursed is None:
            continue

        disbursed_amounts.append(disbursed)
        if gross is not None:
            gross_amounts.append(gross)

        # Date cells (class rpt77dat) — warrant/payment date
        date_cells = DATE_CELL_REGEX.findall(row_html)
        if date_cells:
            date_iso = _parse_date(date_cells[0])
            if date_iso:
                voucher_dates.append(date_iso)

    total_disbursed = sum(disbursed_amounts) if disbursed_amounts else 0.0
    total_gross = sum(gross_amounts) if gross_amounts else None
    voucher_count = len(disbursed_amounts)

    sorted_dates = sorted(d for d in voucher_dates if d)
    # p10: earliest_voucher_date — proxy for award_date (~30-90 day lag from actual award)
    earliest_voucher_date = sorted_dates[0] if sorted_dates else None
    most_recent_voucher_date = sorted_dates[-1] if sorted_dates else None

    caltrans_district = ctrnm[:2] if len(ctrnm) >= 2 else ""
    source_url = RESULTS_URL_TEMPLATE.format(ctrnm=ctrnm)

    raw_payload = {
        "contract_number": ctrnm,
        "winning_contractor_name": winning_contractor_name,
        "voucher_dates": sorted_dates,
        "disbursed_amounts": disbursed_amounts[:10],  # cap for raw_json size
        "voucher_count": voucher_count,
    }

    return {
        "contract_number": ctrnm,
        "winning_contractor_name": winning_contractor_name,
        "winning_contractor_name_normalized": (
            normalize_entity_name(winning_contractor_name)
            if winning_contractor_name
            else ""
        ),
        # p11: Disbursed = net payment (3rd/last rpt77amt cell)
        # Column named total_disbursed_amount_dollars (not total_warrant_amount_dollars)
        "total_disbursed_amount_dollars": str(total_disbursed),
        "total_gross_amount_dollars": str(total_gross) if total_gross is not None else "",
        "voucher_count": str(voucher_count),
        # p10: earliest_voucher_date is a PROXY for award_date with ~30-90 day lag.
        # Column is named earliest_voucher_date NOT award_date to preserve proxy semantics.
        "earliest_voucher_date": earliest_voucher_date or "",
        "most_recent_voucher_date": most_recent_voucher_date or "",
        "caltrans_district": caltrans_district,
        "source_url": source_url,
        "raw_json": json.dumps(raw_payload, ensure_ascii=False),
    }


def _fetch_contract(
    ctrnm: str,
    session: requests.Session,
    contractor_fallback: str,
    sleep_s: float = PER_WORKER_SLEEP_S,
) -> dict[str, Any] | None:
    """Fetch + parse one contract page. Returns None on unrecoverable error."""
    url = RESULTS_URL_TEMPLATE.format(ctrnm=ctrnm)
    try:
        time.sleep(sleep_s)  # p1: per-worker sleep for politeness
        resp = _fetch_with_retry(session, url)
        return _parse_contract_page(resp.text, ctrnm, contractor_fallback)
    except Exception as exc:
        logger.warning("failed to fetch %s: %s", ctrnm, exc)
        return None


def _load_checkpoint() -> set[str]:
    """Load previously-completed ctrnms from checkpoint file (p1: restartable)."""
    if Path(CHECKPOINT_FILE).exists():
        try:
            with open(CHECKPOINT_FILE) as f:
                data = json.load(f)
            completed = set(data.get("completed", []))
            logger.info("checkpoint: %d previously completed contracts", len(completed))
            return completed
        except Exception as exc:
            logger.warning("checkpoint load failed (ignoring): %s", exc)
    return set()


def _save_checkpoint(completed: set[str]) -> None:
    """Save completed ctrnms to checkpoint file."""
    try:
        with open(CHECKPOINT_FILE, "w") as f:
            json.dump({"completed": list(completed)}, f)
    except Exception as exc:
        logger.warning("checkpoint save failed (non-fatal): %s", exc)


def _crawl_all_contracts(
    ctrnms: list[str],
    contractor_map: dict[str, str],
    max_workers: int = MAX_WORKERS,
) -> list[dict[str, Any]]:
    """Crawl all contracts with ThreadPoolExecutor (p1). Returns list of records."""
    checkpoint = _load_checkpoint()
    pending = [c for c in ctrnms if c not in checkpoint]
    logger.info("crawling %d contracts (%d from checkpoint)", len(pending), len(checkpoint))

    results: list[dict[str, Any]] = []
    completed_this_run: set[str] = set()

    def _worker(ctrnm: str) -> tuple[str, dict | None]:
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        fallback = contractor_map.get(ctrnm, "")
        record = _fetch_contract(ctrnm, session, fallback)
        return ctrnm, record

    batch_size = 500  # Save checkpoint every 500 contracts
    batches = [pending[i:i+batch_size] for i in range(0, len(pending), batch_size)]

    for batch_idx, batch in enumerate(batches):
        logger.info("batch %d/%d: %d contracts", batch_idx+1, len(batches), len(batch))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:  # p1: max_workers <= 10
            futures = {executor.submit(_worker, c): c for c in batch}
            for future in as_completed(futures):
                ctrnm = futures[future]
                try:
                    ctrnm_result, record = future.result()
                    if record is not None:
                        results.append(record)
                    completed_this_run.add(ctrnm_result)
                except Exception as exc:
                    logger.warning("unexpected error for %s: %s", ctrnm, exc)

        # Save checkpoint after each batch
        all_completed = checkpoint | completed_this_run
        _save_checkpoint(all_completed)
        logger.info(
            "checkpoint saved: %d total completed (batch %d done)",
            len(all_completed), batch_idx+1,
        )

    return results


def _build_parquet(records: list[dict]) -> pa.Table:
    """Project records to the target Arrow schema (all pa.string() per L9 spirit)."""
    # All columns as pa.string() so DuckDB read_parquet can read without schema inference issues
    # (per predecessor pattern: write pa.string() for all cols so Parquet is already all-varchar)
    def col(key: str) -> list:
        return [r.get(key, "") or "" for r in records]

    schema = pa.schema([
        ("contract_number", pa.string()),
        ("winning_contractor_name", pa.string()),
        ("winning_contractor_name_normalized", pa.string()),
        # p11: Disbursed = net payment (column name pins total_disbursed_amount_dollars)
        ("total_disbursed_amount_dollars", pa.string()),
        ("total_gross_amount_dollars", pa.string()),
        ("voucher_count", pa.string()),
        # p10: earliest_voucher_date (proxy for award_date — NOT renamed to award_date)
        ("earliest_voucher_date", pa.string()),
        ("most_recent_voucher_date", pa.string()),
        ("caltrans_district", pa.string()),
        ("source_url", pa.string()),
        ("raw_json", pa.string()),
    ])

    arrays = [pa.array(col(k), type=pa.string()) for k in schema.names]
    return pa.table(dict(zip(schema.names, arrays)))


def _upload_to_r2(local_path: str, r2_key: str) -> int:
    """Upload Parquet to R2. Returns file size in bytes.

    CRITICAL (L42 / p7): ExtraArgs uses ONLY ContentType — plain .parquet extension.
    Serving raw ZSTD bytes correctly without triggering transparent decompression.
    """
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )
    size = Path(local_path).stat().st_size
    client.upload_file(
        local_path,
        R2_BUCKET,
        r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # L42: ContentType only (no extra headers)
    )
    return size


def _insert_run_row(
    conn,
    run_id_sql: str,
    status: str,
    source_url: str,
    r2_prefix: str | None = None,
    r2_object_count: int | None = None,
    r2_total_bytes: int | None = None,
    parquet_row_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Insert or UPDATE audit ledger row (PR #499 fix #1: UPDATE for terminal states).

    The initial INSERT creates the run_id row with status='running'.
    All subsequent calls for this run_id UPDATE the existing row (avoids PK UniqueViolation).
    Reuses ops.caltrans_r2_ingest_runs table from PR #499 — does NOT create a new table.
    """
    is_terminal = status in ("completed", "failed", "no_change")
    with conn.cursor() as cur:
        if not is_terminal:
            cur.execute(
                """
                INSERT INTO ops.caltrans_r2_ingest_runs
                  (run_id, status, source_url, source_observed_at)
                VALUES
                  (%s::uuid, %s, %s, now())
                ON CONFLICT (run_id) DO NOTHING
                """,
                (run_id_sql, status, source_url),
            )
        else:
            cur.execute(
                """
                UPDATE ops.caltrans_r2_ingest_runs
                SET status            = %s,
                    completed_at      = now(),
                    source_url        = %s,
                    source_observed_at= now(),
                    r2_prefix         = %s,
                    r2_object_count   = %s,
                    r2_total_bytes    = %s,
                    parquet_row_count = %s,
                    error_message     = %s
                WHERE run_id = %s::uuid
                """,
                (
                    status, source_url,
                    r2_prefix, r2_object_count, r2_total_bytes,
                    parquet_row_count, error_message,
                    run_id_sql,
                ),
            )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CalTrans PETS → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to R2 + audit ledger. Without this flag: dry-run on first 50 contracts.",
    )
    args = parser.parse_args()

    today_utc = datetime.now(timezone.utc).date().isoformat()
    r2_key = R2_PREFIX_TEMPLATE.replace("{date}", today_utc)
    r2_prefix_full = f"s3://{R2_BUCKET}/{r2_key}"

    logger.info(
        "CalTrans PETS ingest  normalizer=v%s  apply=%s  target=%s",
        NORMALIZER_VERSION, args.apply, r2_prefix_full,
    )

    # Step 1: GET ctnums.php → parse contract numbers
    logger.info("Step 1: fetching ctnums.php (%s) ...", CTNUMS_URL)
    session = _make_session()
    ctnums_resp = _fetch_with_retry(session, CTNUMS_URL, timeout=60)
    ctrnms = _parse_ctnums(ctnums_resp.text)
    logger.info("  parsed %d contract numbers (non-test)", len(ctrnms))

    if len(ctrnms) < 14000:
        logger.warning("expected >= 14,000 ctrnms; got %d — proceeding anyway", len(ctrnms))

    # Step 2: GET ctors.php → parse contractor-name × contract map (fallback)
    logger.info("Step 2: fetching ctors.php (%s) ...", CTORS_URL)
    ctors_resp = _fetch_with_retry(session, CTORS_URL, timeout=60)
    contractor_map = _parse_ctors(ctors_resp.text)
    logger.info("  parsed contractor map: %d ctrnm → name entries", len(contractor_map))

    if not args.apply:
        # Dry-run: first 50 contracts
        logger.info("DRY-RUN: fetching first 50 contracts to validate parse ...")
        sample = ctrnms[:50]
        records = _crawl_all_contracts(sample, contractor_map)
        logger.info(
            "DRY-RUN: %d/%d contracts parsed OK; Parquet not written (pass --apply)",
            len(records), len(sample),
        )
        # Show a sample record
        if records:
            r = records[0]
            logger.info(
                "  sample: %s | contractor=%s | disbursed=%s | voucher_count=%s | earliest=%s",
                r["contract_number"], r["winning_contractor_name"],
                r["total_disbursed_amount_dollars"], r["voucher_count"],
                r["earliest_voucher_date"],
            )
        return 0

    # Apply mode: check idempotency
    conn = _db_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM ops.caltrans_r2_ingest_runs
            WHERE status = 'completed'
              AND r2_prefix LIKE %s
            """,
            (f"s3://dex-raw-landing-zone/caltrans/awards-pets/snapshot={today_utc}%",),
        )
        existing = cur.fetchone()[0]

    if existing:
        logger.info(
            "idempotency skip: PETS snapshot for %s already has a completed run", today_utc
        )
        return 0

    run_id = str(uuid.uuid4())

    # Insert 'running' row
    _insert_run_row(conn, run_id, "running", SOURCE_URL_BASE)

    try:
        # Step 3: Crawl all contracts
        logger.info("Step 3: crawling %d contracts (max_workers=%d) ...", len(ctrnms), MAX_WORKERS)
        records = _crawl_all_contracts(ctrnms, contractor_map)
        logger.info("  fetched %d records", len(records))

        if not records:
            _insert_run_row(conn, run_id, "no_change", SOURCE_URL_BASE)
            logger.info("no records returned — status=no_change")
            return 0

        # Step 4: Build Arrow table + write Parquet
        tbl = _build_parquet(records)
        row_count = len(tbl)
        logger.info("writing ZSTD Parquet (%d rows) to %s ...", row_count, TMP_PARQUET)
        pq.write_table(
            tbl,
            TMP_PARQUET,
            compression="zstd",
            compression_level=9,
            row_group_size=100_000,
        )

        # Step 5: Upload to R2 (L42: ContentType only)
        logger.info("uploading to R2 at %s ...", r2_key)
        size_bytes = _upload_to_r2(TMP_PARQUET, r2_key)
        logger.info("upload complete: %d bytes", size_bytes)

        # Step 6: Update audit ledger to 'completed'
        _insert_run_row(
            conn, run_id, "completed",
            SOURCE_URL_BASE,
            r2_prefix=r2_prefix_full,
            r2_object_count=1,
            r2_total_bytes=size_bytes,
            parquet_row_count=row_count,
        )
        logger.info(
            "OK — run_id=%s  rows=%d  r2_prefix=%s",
            run_id, row_count, r2_prefix_full,
        )
        return 0

    except Exception as exc:
        logger.exception("PETS ingest failed")
        try:
            _insert_run_row(conn, run_id, "failed", SOURCE_URL_BASE, error_message=repr(exc))
        except Exception:
            logger.exception("also failed to write failed ledger row")
        return 1

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
