"""CalTrans Highway Contract Awards (HCAD) — R2 ingest script.

Fetches awarded construction contracts from the public CalTrans Contractor's
Corner ServiceNow Service Portal JSON API and writes a ZSTD Parquet snapshot
to Cloudflare R2.

Source:
    https://ppmoe.dot.ca.gov/api/now/sp/page?id=cc_awarded
        &portal_id=2086b814c3221200f3897bfaa2d3aea8
    Table: x_cado2_contractor_project (backed by ServiceNow)
    Grain: one row per awarded construction contract
    Corpus: ~5,000 records spanning 2017-2026; 12 CA districts

Extraction:
    12 district sweeps (&contract=01..12). When count==maxCount (500 cap),
    drill by 2-char prefix (e.g. &contract=04-0 .. &contract=04-F) to capture
    all records. Dedup by sys_id (globally unique ServiceNow GUID). Districts
    04 and 07 are KNOWN to exceed the 500-record cap at probe time.

R2 output:
    s3://dex-raw-landing-zone/caltrans/awards/snapshot=YYYY-MM-DD/data.parquet
    ZSTD compression; ExtraArgs={"ContentType": "application/x-parquet"} ONLY
    — ExtraArgs ContentType only; no Content-Encoding header (L42).

Audit ledger:
    ops.caltrans_r2_ingest_runs — one row per run with status enum
    ('pending', 'running', 'completed', 'failed', 'no_change') per L4.

CLI:
    --apply   live mode (writes R2 + audit ledger row)
    no args   dry-run (no R2 write, no ledger row beyond status='failed')

Invocation:
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/run_caltrans_hcad_to_r2.py --apply
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
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
R2_PREFIX_TEMPLATE = "caltrans/awards/snapshot={date}/data.parquet"  # p9 exact shape
SOURCE_URL_BASE = (
    "https://ppmoe.dot.ca.gov/api/now/sp/page"
    "?id=cc_awarded&portal_id=2086b814c3221200f3897bfaa2d3aea8"
)
USER_AGENT = "data-engine-x-caltrans-hcad/1.0 (substrate-research)"  # p1 polite UA

DISTRICTS = [f"{i:02d}" for i in range(1, 13)]  # 01..12
RETRY_AFTER_DEFAULT_S = 2
MAX_RETRIES = 4

TMP_PARQUET = "/tmp/caltrans-awards.parquet"


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


def _fetch_district(
    district: str,
    contract_param: str,
    session: requests.Session,
) -> list[dict]:
    """Fetch one district (or sub-prefix) page from the ServiceNow API.

    Extracts the list from:
        result.containers[0].rows[0].columns[0].widgets[2].widget.data
    Returns the list[] items and also the count + maxCount for cap detection.
    """
    url = f"{SOURCE_URL_BASE}&contract={contract_param}"
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30)
        except requests.RequestException as exc:
            if attempt < MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning("network error (%s); retry in %ds", exc, wait)
                time.sleep(wait)
                continue
            raise

        if resp.status_code == 429 or resp.status_code == 503:
            # Honor Retry-After (p1)
            retry_after = int(resp.headers.get("Retry-After", RETRY_AFTER_DEFAULT_S * (2 ** attempt)))
            logger.warning(
                "HTTP %d on district=%s contract=%s; backing off %ds",
                resp.status_code, district, contract_param, retry_after,
            )
            if attempt < MAX_RETRIES:
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
        resp.raise_for_status()

        body = resp.json()
        try:
            widget_data = (
                body["result"]["containers"][0]["rows"][0]
                ["columns"][0]["widgets"][2]["widget"]["data"]
            )
        except (KeyError, IndexError, TypeError) as exc:
            logger.warning("unexpected widget structure for contract=%s: %s", contract_param, exc)
            return []

        rows = widget_data.get("list", [])
        count = widget_data.get("count", 0)
        max_count = widget_data.get("maxCount", 0)
        return rows, count, max_count

    return [], 0, 0


def _extract_secondary(fields: list[dict], label: str) -> tuple[str, str]:
    """Return (value, display_value) for a given label in secondary_fields."""
    for f in fields:
        if f.get("label", "") == label:
            return f.get("value", ""), f.get("display_value", "")
    return "", ""


def _parse_amount(raw: str) -> float | None:
    """Strip $, commas; return float or None."""
    if not raw:
        return None
    cleaned = raw.replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _record_to_row(record: dict) -> dict:
    """Transcode one ServiceNow record to a flat dict with the target column shape."""
    sys_id = record.get("sys_id", "")
    contract_number = (record.get("display_field") or {}).get("value", "")
    caltrans_district = contract_number[:2] if len(contract_number) >= 2 else ""
    bid_amount_raw = record.get("bidAmount", "")
    bid_amount_dollars = _parse_amount(bid_amount_raw)

    sec = record.get("secondary_fields", [])

    def sv(label):
        v, dv = _extract_secondary(sec, label)
        return v, dv

    _, contractor_name = sv("Name")
    awarded_amount_str, _ = sv("Awarded Amount")
    awarded_amount_dollars = _parse_amount(awarded_amount_str)
    award_date, _ = sv("Award Date/Rejected Date")
    bid_open_date, _ = sv("Bid Open Date")
    advertisement_date, _ = sv("Advertisement Date")
    approval_date, _ = sv("Approval Date")
    _, work_description = sv("Work Description")
    _, county_route_pm = sv("County-Route-PM")
    _, type_of_goal = sv("Type of Goal")
    _, call_out_number = sv("Call Out Number")
    _, phone_number = sv("Phone Number")

    winning_contractor_name = contractor_name.strip()
    winning_contractor_name_normalized = (
        normalize_entity_name(winning_contractor_name)
        if winning_contractor_name
        else ""
    )

    return {
        "sys_id": sys_id,
        "contract_number": contract_number,
        "caltrans_district": caltrans_district,
        "winning_contractor_name": winning_contractor_name,
        "winning_contractor_name_normalized": winning_contractor_name_normalized,
        "awarded_amount_dollars": awarded_amount_dollars,
        "bid_amount_dollars": bid_amount_dollars,
        "award_date": award_date,
        "bid_open_date": bid_open_date,
        "advertisement_date": advertisement_date,
        "approval_date": approval_date,
        "work_description": work_description,
        "county_route_pm": county_route_pm,
        "type_of_goal": type_of_goal,
        "call_out_number": call_out_number,
        "phone_number": phone_number,
        "raw_json": json.dumps(record, ensure_ascii=False),
    }


def _fetch_all_records() -> list[dict]:
    """Sweep all 12 districts; drill capped districts by 2-char prefix."""
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})  # p1 polite UA

    all_records: dict[str, dict] = {}  # sys_id -> record (dedup by GUID p9)
    drill_prefixes = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    for district in DISTRICTS:
        logger.info("sweeping district %s ...", district)
        rows, count, max_count = _fetch_district(district, district, session)
        time.sleep(0.4)  # p1: 300-500ms between sweep calls

        if count == max_count and max_count > 0:  # p6: cap detected
            logger.info(
                "  district %s hit cap (count=%d == maxCount=%d); drilling by sub-prefix",
                district, count, max_count,
            )
            sub_rows_by_id: dict[str, dict] = {}
            for prefix in drill_prefixes:
                sub_param = f"{district}-{prefix}"
                sub_rows, sub_count, sub_max = _fetch_district(
                    district, sub_param, session
                )
                for r in sub_rows:
                    sid = r.get("sys_id", "")
                    if sid:
                        sub_rows_by_id[sid] = r
                time.sleep(0.35)  # p1 polite sleep in sub-prefix drill too
                # Stop prefix drill early if sub-result is clearly empty
                if sub_count == 0:
                    continue
            logger.info(
                "  district %s sub-prefix drill: %d distinct sys_ids",
                district, len(sub_rows_by_id),
            )
            for sid, rec in sub_rows_by_id.items():
                all_records[sid] = rec
        else:
            logger.info("  district %s: %d rows", district, len(rows))
            for r in rows:
                sid = r.get("sys_id", "")
                if sid:
                    all_records[sid] = r

    logger.info("total distinct sys_ids after dedup: %d", len(all_records))
    return list(all_records.values())


def _build_parquet(records: list[dict]) -> pa.Table:
    """Project raw records to the target Arrow schema."""
    rows = [_record_to_row(r) for r in records]

    def col(key):
        return [r[key] for r in rows]

    schema = pa.schema([
        ("sys_id", pa.string()),
        ("contract_number", pa.string()),
        ("caltrans_district", pa.string()),
        ("winning_contractor_name", pa.string()),
        ("winning_contractor_name_normalized", pa.string()),
        ("awarded_amount_dollars", pa.float64()),
        ("bid_amount_dollars", pa.float64()),
        ("award_date", pa.string()),
        ("bid_open_date", pa.string()),
        ("advertisement_date", pa.string()),
        ("approval_date", pa.string()),
        ("work_description", pa.string()),
        ("county_route_pm", pa.string()),
        ("type_of_goal", pa.string()),
        ("call_out_number", pa.string()),
        ("phone_number", pa.string()),
        ("raw_json", pa.string()),
    ])

    arrays = [
        pa.array(col("sys_id"), type=pa.string()),
        pa.array(col("contract_number"), type=pa.string()),
        pa.array(col("caltrans_district"), type=pa.string()),
        pa.array(col("winning_contractor_name"), type=pa.string()),
        pa.array(col("winning_contractor_name_normalized"), type=pa.string()),
        pa.array(col("awarded_amount_dollars"), type=pa.float64()),
        pa.array(col("bid_amount_dollars"), type=pa.float64()),
        pa.array(col("award_date"), type=pa.string()),
        pa.array(col("bid_open_date"), type=pa.string()),
        pa.array(col("advertisement_date"), type=pa.string()),
        pa.array(col("approval_date"), type=pa.string()),
        pa.array(col("work_description"), type=pa.string()),
        pa.array(col("county_route_pm"), type=pa.string()),
        pa.array(col("type_of_goal"), type=pa.string()),
        pa.array(col("call_out_number"), type=pa.string()),
        pa.array(col("phone_number"), type=pa.string()),
        pa.array(col("raw_json"), type=pa.string()),
    ]

    return pa.table(dict(zip(schema.names, arrays)))


def _upload_to_r2(local_path: str, r2_key: str) -> int:
    """Upload Parquet to R2. Returns file size in bytes.

    CRITICAL (L42 / p2): ExtraArgs uses ONLY ContentType — omit the
    Content-Encoding header entirely. Plain .parquet extension. Cloudflare R2
    transparent decompression is triggered by the Content-Encoding header;
    omitting it serves raw bytes correctly.
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
        ExtraArgs={"ContentType": "application/x-parquet"},  # L42: Content-Encoding header omitted
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
    """Insert the initial 'running' row, or UPDATE it for terminal states.

    The initial INSERT creates the run_id row with status='running'.
    All subsequent calls for this run_id UPDATE the existing row rather than
    inserting a new one (avoids unique constraint violation on run_id PK).
    """
    is_terminal = status in ("completed", "failed", "no_change")
    with conn.cursor() as cur:
        if not is_terminal:
            # Initial INSERT for the 'running' sentinel.
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
            # UPDATE the existing row to the terminal state.
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
        description="CalTrans HCAD → R2 ZSTD Parquet ingest"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write to R2 + audit ledger. Without this flag: dry-run.",
    )
    args = parser.parse_args()

    today_utc = datetime.now(timezone.utc).date().isoformat()
    r2_key = R2_PREFIX_TEMPLATE.replace("{date}", today_utc)
    r2_prefix_full = f"s3://{R2_BUCKET}/{r2_key}"

    logger.info(
        "CalTrans HCAD ingest  normalizer=v%s  apply=%s  target=%s",
        NORMALIZER_VERSION, args.apply, r2_prefix_full,
    )

    if not args.apply:
        logger.info("DRY-RUN: fetching records (no R2 write, no ledger row)")
        records = _fetch_all_records()
        tbl = _build_parquet(records)
        logger.info("DRY-RUN: %d rows built; Parquet not written", len(tbl))
        return 0

    # Check idempotency: skip if today's snapshot already exists in ledger.
    conn = _db_conn()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT count(*)
            FROM ops.caltrans_r2_ingest_runs
            WHERE status = 'completed'
              AND r2_prefix LIKE %s
            """,
            (f"s3://dex-raw-landing-zone/caltrans/awards/snapshot={today_utc}%",),
        )
        existing = cur.fetchone()[0]

    if existing:
        logger.info(
            "idempotency skip: snapshot for %s already has a completed run", today_utc
        )
        return 0

    import uuid

    run_id = str(uuid.uuid4())

    # Insert 'running' row.
    _insert_run_row(
        conn, run_id, "running",
        SOURCE_URL_BASE,
    )

    try:
        records = _fetch_all_records()

        if not records:
            _insert_run_row(
                conn, run_id, "no_change",
                SOURCE_URL_BASE,
            )
            logger.info("no records returned — status=no_change")
            return 0

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

        logger.info("uploading to R2 at %s ...", r2_key)
        size_bytes = _upload_to_r2(TMP_PARQUET, r2_key)
        logger.info("upload complete: %d bytes", size_bytes)

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
        logger.exception("ingest failed")
        try:
            _insert_run_row(
                conn, run_id, "failed",
                SOURCE_URL_BASE,
                error_message=repr(exc),
            )
        except Exception:
            logger.exception("also failed to write failed ledger row")
        return 1

    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
