"""NY data.ny.gov construction-targeting substrate — Socrata API ingest.

Background:
  The original NY OpenBookNY OSC ColdFusion portal at wwe2.osc.state.ny.us
  rejects all automated CSV/TSV downloads with "An unexpected error occurred"
  regardless of session cookies / Referer / User-Agent / param-matrix. The
  prior Modal app `ny_openbook_ingest_app.py` was deleted as a result.

  Pivoted source: data.ny.gov Socrata API. Two construction-aligned datasets
  selected for tight alignment with the original "fills NY-side of
  construction-targeting roadmap" strategic role:

    * ehig-g5x3 — "Procurement Report for State Authorities"
                  (~275K rows; 140K NY-vendor)
                  → nystate.contracts_lance
    * rb9h-9fit — "Design & Construction Capital Projects Vendor Payments:
                   Beginning 2014" (~69K rows; NY state construction payments)
                  → nystate.vendor_payments_lance

R2 layout (latest-snapshot pattern):
    s3://dex-raw-landing-zone/ny-data-construction/contracts/snapshot=YYYY-MM-DD/data.parquet
    s3://dex-raw-landing-zone/ny-data-construction/vendor_payments/snapshot=YYYY-MM-DD/data.parquet

Per-row provenance:
    Each row carries source_dataset_id (Socrata 4x4) + retrieved_at (ISO8601).

Modal app:
    data-engine-x-ny-data-construction-ingest

Entrypoints:
    run_ingest_contracts()         — fetch ehig-g5x3, transform, upload
    run_ingest_vendor_payments()   — fetch rb9h-9fit, transform, upload
    run_backfill()                 — both feeds

Schedule:
    Weekly Mon 12:00 UTC (`0 12 * * 1`). Socrata datasets are refreshed by
    NY agencies on irregular cadence; weekly snapshot is the natural batch.

Secrets:
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
    dex-db   — DATABASE_URL (bulk_ingest.feed_ingest_runs ledger)

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/ny_data_construction_ingest_app.py

Manual run:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/ny_data_construction_ingest_app.py::run_backfill
"""
from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-ny-data-construction-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 60 * 60  # 1h ceiling

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "ny-data-construction"

CONTRACTS_DATASET_ID = "ehig-g5x3"
VENDOR_PAYMENTS_DATASET_ID = "rb9h-9fit"

SOURCE_ID_CONTRACTS = "ny_data_construction_contracts"
SOURCE_ID_VENDOR_PAYMENTS = "ny_data_construction_vendor_payments"

SOCRATA_PAGE_SIZE = 50_000  # Socrata's max with $limit
USER_AGENT = "data-engine-x-research/1.0"

DEFAULT_CRON = "0 12 * * 1"  # Mon 12:00 UTC weekly


# ---------------------------------------------------------------------------
# Ledger helpers (mirror modal/usaspending_api_daily_app.py:79-130 shape)
# ---------------------------------------------------------------------------

def _bridge_database_url() -> None:
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _classify_error(exc: Exception) -> str:
    msg = str(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in msg or "timeout" in name:
        return "timeout"
    if any(k in msg for k in ("http", "download", "connection", "ssl", "tls")):
        return "download_failure"
    if "parse" in msg or "json" in msg or "duckdb" in msg or "parquet" in msg:
        return "parse_failure"
    if "boto" in name or "s3" in msg or "r2" in msg:
        return "r2_failure"
    if "psycopg" in name or "db" in msg:
        return "db_failure"
    return "unknown"


def _record_run(
    *,
    run_id: str,
    source_id: str,
    feed_name: str,
    feed_date: date,
    status: str,
    outcome: str,
    started_at: str,
    completed_at: str | None,
    duration_seconds: float | None,
    rows_loaded: int | None,
    landing_zone: str,
    r2_bucket: str | None,
    r2_object_key: str | None,
    payload_format: str | None,
    payload_bytes: int | None,
    error_class: str | None,
    error_message: str | None,
    evidence: dict[str, Any],
) -> None:
    import json
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL/DEX_DB_URL_POOLED not set")

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bulk_ingest.feed_ingest_runs (
                    run_id, source_id, feed_name, feed_date, attempt,
                    status, outcome, started_at, completed_at, duration_seconds,
                    rows_loaded, landing_zone, r2_bucket, r2_object_key,
                    payload_format, payload_bytes,
                    error_class, error_message, evidence
                ) VALUES (
                    %s, %s, %s, %s, 1,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s
                )
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE SET
                    status = EXCLUDED.status,
                    outcome = EXCLUDED.outcome,
                    completed_at = EXCLUDED.completed_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    rows_loaded = EXCLUDED.rows_loaded,
                    r2_object_key = EXCLUDED.r2_object_key,
                    payload_bytes = EXCLUDED.payload_bytes,
                    error_class = EXCLUDED.error_class,
                    error_message = EXCLUDED.error_message,
                    evidence = EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    run_id, source_id, feed_name, feed_date,
                    status, outcome, started_at, completed_at, duration_seconds,
                    rows_loaded, landing_zone, r2_bucket, r2_object_key,
                    payload_format, payload_bytes,
                    error_class, error_message, json.dumps(evidence),
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Socrata downloader
# ---------------------------------------------------------------------------

def _socrata_url(dataset_id: str) -> str:
    return f"https://data.ny.gov/resource/{dataset_id}.json"


def _socrata_count(dataset_id: str, client) -> int:
    url = _socrata_url(dataset_id)
    r = client.get(url, params={"$select": "count(*)"})
    r.raise_for_status()
    rows = r.json()
    return int(rows[0]["count"])


def _socrata_fetch_all(dataset_id: str, client, logger) -> list[dict]:
    """Paginate through entire Socrata dataset via $limit + $offset."""
    url = _socrata_url(dataset_id)
    total = _socrata_count(dataset_id, client)
    logger(f"[{dataset_id}] total rows: {total:,}")

    all_rows: list[dict] = []
    offset = 0
    page = 1
    while offset < total:
        r = client.get(url, params={
            "$limit": SOCRATA_PAGE_SIZE,
            "$offset": offset,
            "$order": ":id",  # stable pagination order
        })
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        all_rows.extend(rows)
        logger(
            f"[{dataset_id}] page {page}: fetched {len(rows):,} "
            f"rows (cumulative {len(all_rows):,}/{total:,})"
        )
        offset += SOCRATA_PAGE_SIZE
        page += 1
    return all_rows


# ---------------------------------------------------------------------------
# Column normalization (snake_case for nicer downstream JOINs)
# ---------------------------------------------------------------------------

def _snake_case(name: str) -> str:
    n = re.sub(r"[^A-Za-z0-9]+", "_", name)
    n = re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), n)
    n = re.sub(r"_+", "_", n).strip("_").lower()
    return n


def _normalize_entity_name_py(s: str | None) -> str | None:
    """Importable wrapper to scripts._lib.entity_name_normalize.

    Imported lazily inside the Modal function so it works against the
    image's add_local_dir("scripts/dex", remote_path="/root/scripts").
    """
    if not s:
        return None
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

    return normalize_entity_name(s)


# ---------------------------------------------------------------------------
# Per-feed transform → Parquet
# ---------------------------------------------------------------------------

def _transform_contracts_to_parquet(
    rows: list[dict], snapshot_date: str, tmpdir: str
) -> tuple[str, int]:
    """ehig-g5x3 contracts → typed Parquet at /tmp/.../data.parquet.

    Adds derived columns:
      vendor_name_normalized — normalize_entity_name(vendor_name)
      contract_id            — sha1 of (authority_name|vendor_name|award_date|contract_amount)
      source_dataset_id      — 'ehig-g5x3'
      retrieved_at           — ISO8601 UTC at ingest
      snapshot_date          — YYYY-MM-DD
    """
    import hashlib

    import duckdb

    if not rows:
        raise RuntimeError("contracts: no rows fetched from Socrata")

    retrieved_at = datetime.now(timezone.utc).isoformat()

    # Compute derived columns row-by-row
    transformed: list[dict] = []
    for row in rows:
        # Build the synthetic contract_id from stable identifying fields.
        salt_parts = [
            row.get("authority_name", "") or "",
            row.get("vendor_name", "") or "",
            row.get("award_date", "") or "",
            row.get("contract_amount", "") or "",
            row.get("procurement_description", "") or "",
        ]
        contract_id = hashlib.sha1(
            "|".join(salt_parts).encode("utf-8")
        ).hexdigest()[:16]
        vendor_name = row.get("vendor_name", "") or ""
        vendor_name_normalized = _normalize_entity_name_py(vendor_name)

        out = {k: v for k, v in row.items() if not k.startswith(":@")}
        out["contract_id"] = contract_id
        out["vendor_name_normalized"] = vendor_name_normalized
        out["source_dataset_id"] = CONTRACTS_DATASET_ID
        out["retrieved_at"] = retrieved_at
        out["snapshot_date"] = snapshot_date
        transformed.append(out)

    # Write JSON Lines then DuckDB COPY to Parquet (avoids in-memory pyarrow
    # cast issues for sparse/mixed-type Socrata responses).
    jsonl_path = os.path.join(tmpdir, "contracts.jsonl")
    import json
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in transformed:
            f.write(json.dumps(row) + "\n")

    parquet_path = os.path.join(tmpdir, "contracts.parquet")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT * FROM read_json_auto(
                '{jsonl_path}',
                format='newline_delimited',
                maximum_object_size=104857600,
                sample_size=-1,
                union_by_name=true
            )
        ) TO '{parquet_path}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
        )
    """)
    row_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
    con.close()
    return parquet_path, int(row_count)


def _transform_vendor_payments_to_parquet(
    rows: list[dict], snapshot_date: str, tmpdir: str
) -> tuple[str, int]:
    """rb9h-9fit payments → typed Parquet at /tmp/.../data.parquet.

    Adds derived columns:
      vendor_name_normalized — normalize_entity_name(vendor)
      payment_id             — sha1 of (contractnumber|vendor|paymentamount|fiscalyear|quarter)
      source_dataset_id      — 'rb9h-9fit'
      retrieved_at           — ISO8601 UTC at ingest
      snapshot_date          — YYYY-MM-DD
      vendor_name            — alias of 'vendor' for downstream consistency
    """
    import hashlib

    import duckdb

    if not rows:
        raise RuntimeError("vendor_payments: no rows fetched from Socrata")

    retrieved_at = datetime.now(timezone.utc).isoformat()

    transformed: list[dict] = []
    for row in rows:
        salt_parts = [
            row.get("contractnumber", "") or "",
            row.get("vendor", "") or "",
            row.get("paymentamount", "") or "",
            row.get("fiscalyear", "") or "",
            row.get("quarter", "") or "",
            row.get("typeofservice", "") or "",
            row.get("county", "") or "",
        ]
        payment_id = hashlib.sha1(
            "|".join(salt_parts).encode("utf-8")
        ).hexdigest()[:16]
        vendor = row.get("vendor", "") or ""
        vendor_name_normalized = _normalize_entity_name_py(vendor)

        out = {k: v for k, v in row.items() if not k.startswith(":@")}
        out["vendor_name"] = vendor
        out["payment_id"] = payment_id
        out["vendor_name_normalized"] = vendor_name_normalized
        out["source_dataset_id"] = VENDOR_PAYMENTS_DATASET_ID
        out["retrieved_at"] = retrieved_at
        out["snapshot_date"] = snapshot_date
        transformed.append(out)

    jsonl_path = os.path.join(tmpdir, "vendor_payments.jsonl")
    import json
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in transformed:
            f.write(json.dumps(row) + "\n")

    parquet_path = os.path.join(tmpdir, "vendor_payments.parquet")
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT * FROM read_json_auto(
                '{jsonl_path}',
                format='newline_delimited',
                maximum_object_size=104857600,
                sample_size=-1,
                union_by_name=true
            )
        ) TO '{parquet_path}' (
            FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000
        )
    """)
    row_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')"
    ).fetchone()[0]
    con.close()
    return parquet_path, int(row_count)


# ---------------------------------------------------------------------------
# R2 upload (L42: ContentType only, no Content-Encoding header)
# ---------------------------------------------------------------------------

def _r2_client():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _upload_to_r2(local_path: str, r2_key: str) -> int:
    s3 = _r2_client()
    s3.upload_file(
        local_path, R2_BUCKET, r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},  # L42: NO ContentEncoding
    )
    head = s3.head_object(Bucket=R2_BUCKET, Key=r2_key)
    return int(head["ContentLength"])


def _upload_notice_md() -> None:
    """Write a NOTICE.md to R2 prefix root recording source provenance."""
    s3 = _r2_client()
    body = f"""# NY data.ny.gov construction-targeting substrate

Source: NY State Open Data Portal (data.ny.gov)
Access: Socrata Open Data API (SODA) — no authentication required
Datasets:
  * ehig-g5x3 — "Procurement Report for State Authorities"
    URL: https://data.ny.gov/resource/ehig-g5x3.json
    Output Lance: polaris-warehouse/nystate/contracts_lance
  * rb9h-9fit — "Design & Construction Capital Projects Vendor Payments:
                 Beginning 2014"
    URL: https://data.ny.gov/resource/rb9h-9fit.json
    Output Lance: polaris-warehouse/nystate/vendor_payments_lance

License: NY government records are public records published for
transparency on data.ny.gov. Datasets are made available under the standard
NY Open Data terms permitting unrestricted reuse including commercial use,
redistribution, and derivative works. See https://data.ny.gov/about for the
portal's terms.

Retrieved: {datetime.now(timezone.utc).isoformat()}
Ingest pipeline: apps/data-engine-x/modal/ny_data_construction_ingest_app.py
"""
    s3.put_object(
        Bucket=R2_BUCKET,
        Key=f"{R2_PREFIX}/NOTICE.md",
        Body=body.encode("utf-8"),
        ContentType="text/markdown; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# Per-feed Modal entrypoints
# ---------------------------------------------------------------------------

def _ingest_feed(
    *,
    dataset_id: str,
    source_id: str,
    feed_name: str,
    transform_fn,
    snapshot_date: str,
) -> dict[str, Any]:
    import logging
    log = logging.getLogger(source_id)
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
        log.addHandler(h)

    _bridge_database_url()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    feed_date = date.fromisoformat(snapshot_date)

    r2_key = f"{R2_PREFIX}/{feed_name}/snapshot={snapshot_date}/data.parquet"

    _record_run(
        run_id=run_id, source_id=source_id, feed_name=feed_name,
        feed_date=feed_date, status="running", outcome="never_ran",
        started_at=started_at, completed_at=None, duration_seconds=None,
        rows_loaded=None, landing_zone="r2",
        r2_bucket=R2_BUCKET, r2_object_key=r2_key,
        payload_format="parquet", payload_bytes=None,
        error_class=None, error_message=None,
        evidence={"dataset_id": dataset_id},
    )

    t0 = time.time()
    try:
        import httpx
        with httpx.Client(
            timeout=httpx.Timeout(60.0, read=300.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            rows = _socrata_fetch_all(dataset_id, client, log.info)

        log.info("transforming %d rows to Parquet ...", len(rows))
        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, row_count = transform_fn(rows, snapshot_date, tmpdir)
            log.info("Parquet ready: %s (%d rows)", parquet_path, row_count)
            size_bytes = _upload_to_r2(parquet_path, r2_key)
            log.info("uploaded to r2://%s/%s (%d bytes)", R2_BUCKET, r2_key, size_bytes)

        duration = time.time() - t0
        completed_at = datetime.now(timezone.utc).isoformat()
        _record_run(
            run_id=run_id, source_id=source_id, feed_name=feed_name,
            feed_date=feed_date,
            status="completed", outcome="succeeded_with_changes",
            started_at=started_at, completed_at=completed_at,
            duration_seconds=round(duration, 1),
            rows_loaded=row_count, landing_zone="r2",
            r2_bucket=R2_BUCKET, r2_object_key=r2_key,
            payload_format="parquet", payload_bytes=size_bytes,
            error_class=None, error_message=None,
            evidence={
                "dataset_id": dataset_id,
                "fetched_rows": len(rows),
                "parquet_rows": row_count,
                "parquet_bytes": size_bytes,
                "snapshot_date": snapshot_date,
            },
        )

        return {
            "run_id": run_id,
            "source_id": source_id,
            "feed_name": feed_name,
            "snapshot_date": snapshot_date,
            "rows_loaded": row_count,
            "r2_key": r2_key,
            "duration_seconds": round(duration, 1),
        }

    except Exception as exc:
        duration = time.time() - t0
        completed_at = datetime.now(timezone.utc).isoformat()
        err_msg = repr(exc)[:2000]
        err_cls = _classify_error(exc)
        _record_run(
            run_id=run_id, source_id=source_id, feed_name=feed_name,
            feed_date=feed_date,
            status="failed", outcome="failed",
            started_at=started_at, completed_at=completed_at,
            duration_seconds=round(duration, 1),
            rows_loaded=None, landing_zone="r2",
            r2_bucket=R2_BUCKET, r2_object_key=r2_key,
            payload_format=None, payload_bytes=None,
            error_class=err_cls, error_message=err_msg,
            evidence={"dataset_id": dataset_id},
        )
        raise


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
)
def run_ingest_contracts(snapshot_date: str | None = None) -> dict[str, Any]:
    """Ingest ehig-g5x3 Procurement Report State Authorities into R2."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    return _ingest_feed(
        dataset_id=CONTRACTS_DATASET_ID,
        source_id=SOURCE_ID_CONTRACTS,
        feed_name="contracts",
        transform_fn=_transform_contracts_to_parquet,
        snapshot_date=snapshot_date,
    )


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
)
def run_ingest_vendor_payments(snapshot_date: str | None = None) -> dict[str, Any]:
    """Ingest rb9h-9fit Design+Construction Vendor Payments into R2."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    return _ingest_feed(
        dataset_id=VENDOR_PAYMENTS_DATASET_ID,
        source_id=SOURCE_ID_VENDOR_PAYMENTS,
        feed_name="vendor_payments",
        transform_fn=_transform_vendor_payments_to_parquet,
        snapshot_date=snapshot_date,
    )


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron(DEFAULT_CRON),
)
def run_backfill(snapshot_date: str | None = None) -> dict[str, Any]:
    """Backfill both feeds (also serves as the weekly cron entrypoint)."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    run_id = str(uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_backfill",
        run_id=run_id,
    ) as hb:
        hb.set_stage("contracts", {"snapshot_date": snapshot_date})
        contracts = _ingest_feed(
            dataset_id=CONTRACTS_DATASET_ID,
            source_id=SOURCE_ID_CONTRACTS,
            feed_name="contracts",
            transform_fn=_transform_contracts_to_parquet,
            snapshot_date=snapshot_date,
        )
        hb.set_stage("vendor_payments")
        vendor_payments = _ingest_feed(
            dataset_id=VENDOR_PAYMENTS_DATASET_ID,
            source_id=SOURCE_ID_VENDOR_PAYMENTS,
            feed_name="vendor_payments",
            transform_fn=_transform_vendor_payments_to_parquet,
            snapshot_date=snapshot_date,
        )
    try:
        _upload_notice_md()
    except Exception:
        pass
    return {"contracts": contracts, "vendor_payments": vendor_payments}
