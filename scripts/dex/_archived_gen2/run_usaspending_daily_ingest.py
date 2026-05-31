"""USAspending Daily Drip — download + parquet conversion + R2 upload.

Wrapped by modal/usaspending_daily_app.py at runtime; keeps the data-flow
logic separate from the Modal scheduling decoration so it stays unit-
testable and re-runnable from a local shell.

Flow:
    1. POST USAspending bulk-download API for {feed_date} action_date range.
    2. Poll the returned status URL until file_url appears.
    3. Stream-download the resulting ZIP/CSV.
    4. Stream-parse the CSV in batches; project to the GTM-essentials column
       set (see GTM_COLUMNS); coerce strict types (UEI/EIN/NAICS = str,
       action_date = date, federal_action_obligation = decimal).
    5. Hand RecordBatches to R2Landing.write_streaming_to_key — the harness
       writes ZSTD-compressed Parquet to dex-raw-landing-zone via boto3
       multipart upload.
    6. Return rows_loaded + payload_bytes for the ledger.

Operator-confirmable TODOs:
    - GTM_COLUMNS list: scaffolded with the canonical identity-spine columns,
      operator should expand to the full "Top 50 GTM Columns" set when
      ready.
    - Bulk-download API filters: scaffolded for "all federal contract
      awards on action_date == {feed_date}". Adjust filters block if the
      operator wants subaward inclusion / agency carve-outs / etc.
    - File format: directive said CSV → Parquet conversion. USAspending's
      API also serves Parquet directly (file_format='parquet'). If they
      do, we can skip the CSV→Parquet step. TODO confirm.
"""

from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from datetime import date
from typing import Any, Iterator

import httpx

# ──────────────────────────────────────────────────────────────────────────
# GTM-essentials column projection
# ──────────────────────────────────────────────────────────────────────────
# Reduces the 308-column USAspending CSV down to the identity spine + the
# financials + set-aside flags that drive contract targeting. Cuts R2 storage
# by ~60% per directive. Operator should expand to "Top 50" — this is
# ~20 to start.
#
# Column names match USAspending's CSV header convention (snake_case lower).
# Strict type mapping enforced on parquet write below.

GTM_COLUMNS: list[tuple[str, str]] = [
    # Identity spine
    ("contract_award_unique_key", "string"),
    ("recipient_uei", "string"),
    ("recipient_name", "string"),
    ("recipient_duns", "string"),  # legacy; keep until DUNS fully retired
    ("recipient_parent_uei", "string"),
    ("recipient_parent_name", "string"),
    # Action / temporal
    ("action_date", "date"),
    ("period_of_performance_start_date", "date"),
    ("period_of_performance_current_end_date", "date"),
    # Financials
    ("federal_action_obligation", "decimal"),
    ("total_dollars_obligated", "decimal"),
    ("base_and_exercised_options_value", "decimal"),
    ("base_and_all_options_value", "decimal"),
    # Awarding agency
    ("awarding_agency_code", "string"),
    ("awarding_agency_name", "string"),
    ("awarding_sub_agency_code", "string"),
    ("awarding_sub_agency_name", "string"),
    # Set-asides (the GTM gold)
    ("type_of_set_aside_code", "string"),
    ("type_of_set_aside", "string"),
    ("small_business_competitiveness_demonstration_program", "string"),
    # Industry
    ("naics_code", "string"),
    ("naics_description", "string"),
    ("product_or_service_code", "string"),
    ("product_or_service_code_description", "string"),
    # Location (recipient)
    ("recipient_state_code", "string"),
    ("recipient_city_name", "string"),
    ("recipient_zip_4_code", "string"),
    # Award metadata
    ("award_type_code", "string"),
    ("award_type", "string"),
    ("contract_award_type", "string"),
]

# ──────────────────────────────────────────────────────────────────────────
# Bulk-download API
# ──────────────────────────────────────────────────────────────────────────

USASPENDING_BULK_DOWNLOAD_URL = "https://api.usaspending.gov/api/v2/bulk_download/awards/"
USASPENDING_STATUS_URL = "https://api.usaspending.gov/api/v2/bulk_download/status/"

POLL_INTERVAL_SECONDS = 15
POLL_TIMEOUT_SECONDS = 60 * 60  # 1 hour ceiling on download readiness


def _request_bulk_download(*, feed_date: date, client: httpx.Client) -> str:
    """POST a bulk-download request for federal contract awards modified on
    feed_date. Returns the status_url to poll.

    Uses date_type=last_modified_date (NOT action_date) because USAspending
    has a 7+ day lag between when a contract action happens and when it
    lands in their warehouse. A daily drip on action_date returns 0 rows
    every day; last_modified_date captures the full daily delta of records
    USAspending actually received that day, regardless of the underlying
    action's age.

    Filter is PRIME contract types (BPA Call / Purchase Order / Delivery
    Order / Definitive Contract); subawards excluded. All agencies.
    """
    payload = {
        "filters": {
            "prime_award_types": [
                "A",  # BPA Call
                "B",  # Purchase Order
                "C",  # Delivery Order
                "D",  # Definitive Contract
            ],
            "date_type": "last_modified_date",
            "date_range": {
                "start_date": feed_date.isoformat(),
                "end_date": feed_date.isoformat(),
            },
        },
        "file_format": "csv",
    }
    response = client.post(USASPENDING_BULK_DOWNLOAD_URL, json=payload, timeout=60.0)
    response.raise_for_status()
    body = response.json()
    status_url = body.get("status_url") or body.get("url")
    if not status_url:
        raise RuntimeError(
            f"USAspending bulk_download response missing status_url: {body}"
        )
    return status_url


def _poll_until_ready(*, status_url: str, client: httpx.Client) -> str:
    """Poll the bulk-download status endpoint until file_url is populated.
    Returns the file_url to download.
    """
    deadline = time.time() + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        response = client.get(status_url, timeout=30.0)
        response.raise_for_status()
        body = response.json()
        status = body.get("status")
        if status == "finished":
            file_url = body.get("file_url") or body.get("url")
            if not file_url:
                raise RuntimeError(
                    f"bulk_download finished but file_url missing: {body}"
                )
            return file_url
        if status == "failed":
            raise RuntimeError(
                f"bulk_download failed: {body.get('message') or body}"
            )
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"bulk_download still pending after {POLL_TIMEOUT_SECONDS}s — "
        f"USAspending queue may be backed up."
    )


# ──────────────────────────────────────────────────────────────────────────
# CSV → RecordBatch streaming
# ──────────────────────────────────────────────────────────────────────────


def _coerce_value(raw: str, target_type: str) -> Any:
    """Apply strict typing per the directive (UEI/EIN/NAICS = str,
    action_date = date, federal_action_obligation = decimal)."""
    import decimal as _decimal

    if raw is None or raw == "":
        return None
    if target_type == "string":
        return raw
    if target_type == "date":
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if target_type == "decimal":
        try:
            return _decimal.Decimal(raw)
        except (_decimal.InvalidOperation, ValueError):
            return None
    return raw


def _iter_record_batches(
    *,
    csv_path: str,
    chunk_size: int = 25_000,
) -> Iterator[Any]:
    """Stream-parse the USAspending CSV; project to GTM_COLUMNS; yield
    pyarrow.RecordBatch chunks for the parquet writer."""
    import pyarrow as pa

    column_names = [name for name, _ in GTM_COLUMNS]
    column_types = dict(GTM_COLUMNS)

    with open(csv_path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        chunk_rows: list[dict[str, Any]] = []
        for row in reader:
            projected = {
                name: _coerce_value(row.get(name, ""), column_types[name])
                for name in column_names
            }
            chunk_rows.append(projected)
            if len(chunk_rows) >= chunk_size:
                yield pa.RecordBatch.from_pylist(chunk_rows)
                chunk_rows = []
        if chunk_rows:
            yield pa.RecordBatch.from_pylist(chunk_rows)


def _download_zip_and_extract_csv(
    *, file_url: str, client: httpx.Client, work_dir: str
) -> str:
    """Stream the ZIP, extract the (single) CSV, return the local path."""
    zip_path = os.path.join(work_dir, "usaspending.zip")
    with client.stream("GET", file_url, timeout=300.0) as response:
        response.raise_for_status()
        with open(zip_path, "wb") as out:
            for chunk in response.iter_bytes(chunk_size=1 << 20):
                out.write(chunk)
    with zipfile.ZipFile(zip_path) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise RuntimeError(
                f"USAspending zip contained no CSV: {zf.namelist()}"
            )
        # USAspending packages "Contracts_PrimeAwardSummaries_*.csv". Pick
        # the first one for now; operator can refine if multi-CSV ever
        # ships.
        csv_path = zf.extract(csv_names[0], work_dir)
    return csv_path


# ──────────────────────────────────────────────────────────────────────────
# Top-level entry — called by modal/usaspending_daily_app.py::run_daily_drip
# ──────────────────────────────────────────────────────────────────────────


def run_ingest(
    *,
    feed_date: date,
    run_id: str,
    r2_object_key: str,
    work_dir: str | None = None,
) -> dict[str, Any]:
    """Pull yesterday's USAspending contract delta → R2 parquet.

    Returns a dict the Modal app stores in evidence: rows_loaded,
    payload_bytes, file_url, csv_size_bytes, http_status, etc.
    """
    import tempfile

    from landing.r2 import R2Landing

    work_dir = work_dir or tempfile.mkdtemp(prefix=f"usaspending-{feed_date}-")
    print(f"[usaspending] work_dir={work_dir} feed_date={feed_date} run_id={run_id}")

    with httpx.Client(headers={"User-Agent": "data-engine-x/1.0"}) as client:
        status_url = _request_bulk_download(feed_date=feed_date, client=client)
        print(f"[usaspending] status_url={status_url}")
        file_url = _poll_until_ready(status_url=status_url, client=client)
        print(f"[usaspending] file_url={file_url}")
        csv_path = _download_zip_and_extract_csv(
            file_url=file_url, client=client, work_dir=work_dir
        )
        csv_size = os.path.getsize(csv_path)
        print(f"[usaspending] csv_path={csv_path} bytes={csv_size}")

    r2 = R2Landing()
    landing_result = r2.write_streaming_to_key(
        key=r2_object_key,
        batches=_iter_record_batches(csv_path=csv_path),
    )
    print(
        f"[usaspending] r2 wrote rows={landing_result.rows_loaded} "
        f"bytes={landing_result.payload_bytes} key={landing_result.r2_object_key}"
    )

    return {
        "rows_loaded": landing_result.rows_loaded,
        "payload_bytes": landing_result.payload_bytes,
        "csv_size_bytes": csv_size,
        "file_url": file_url,
        "status_url": status_url,
        "r2_object_key": landing_result.r2_object_key,
        "gtm_columns_count": len(GTM_COLUMNS),
    }


if __name__ == "__main__":
    # Local smoke test:
    #   doppler run --project hq-all --config prd -- \\
    #     uv run --project apps/data-engine-x \\
    #     python apps/data-engine-x/scripts/run_usaspending_daily_ingest.py 2026-05-07
    import sys as _sys
    import uuid as _uuid

    raw_date = _sys.argv[1] if len(_sys.argv) > 1 else None
    if not raw_date:
        from datetime import datetime, timedelta, timezone
        raw_date = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    feed_date = date.fromisoformat(raw_date)
    rid = str(_uuid.uuid4())
    key = (
        f"usaspending/contracts/year={feed_date.year:04d}/"
        f"month={feed_date.month:02d}/day={feed_date.day:02d}/"
        f"{rid}.parquet.zst"
    )
    result = run_ingest(feed_date=feed_date, run_id=rid, r2_object_key=key)
    print(result)
