"""NYC + MTA + NY Local Authorities contractor-awards substrate — Socrata API ingest.

Mirror of apps/data-engine-x/modal/ny_data_construction_ingest_app.py shape.
Lands 3 NY contractor-award datasets in R2 as ZSTD Parquet:

  * qyyg-4tf5 — NYC "Recent Contract Awards" (~52K rows; mixed solicit + Award
                 type_of_notice_description; downstream bridges may filter to
                 type_of_notice_description='Award')
                 → nyc.contract_awards_lance
  * twsw-2mqa — NY State "MTA Procurements: Beginning 2018" (~107K rows; has
                 vendor_state for NY-vendor filter on bridges)
                 → nystate.mta_procurements_lance
  * 8w5p-k45m — NY State "Procurement Report for Local Authorities" (~65K rows;
                 has vendor_state)
                 → nystate.local_authority_procurements_lance

R2 layout (latest-snapshot pattern):
  s3://dex-raw-landing-zone/nyc-contract-awards/snapshot=YYYY-MM-DD/data.parquet
  s3://dex-raw-landing-zone/ny-mta-procurements/snapshot=YYYY-MM-DD/data.parquet
  s3://dex-raw-landing-zone/ny-local-authority-procurements/snapshot=YYYY-MM-DD/data.parquet

Per-row provenance:
  source_dataset_id (Socrata 4x4) + retrieved_at (ISO8601 UTC) + snapshot_date.

Modal app: data-engine-x-ny-nyc-local-awards-ingest

Entrypoints:
  run_ingest_nyc_contract_awards()
  run_ingest_ny_mta_procurements()
  run_ingest_ny_local_authority_procurements()
  run_backfill() — all three feeds in sequence; weekly cron `0 13 * * 1` UTC

Schedule: Mon 13:00 UTC (staggered from ny_data_construction_ingest_app.py's
0 12 * * 1).

Secrets:
  bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
  dex-db   — DATABASE_URL (bulk_ingest.feed_ingest_runs ledger)

Deploy:
  cd ~/hq-all/apps/data-engine-x && \\
    doppler run --project hq-all --config prd -- \\
    modal deploy modal/ny_nyc_local_awards_ingest_app.py

Manual run:
  cd ~/hq-all/apps/data-engine-x && \\
    doppler run --project hq-all --config prd -- \\
    modal run --detach modal/ny_nyc_local_awards_ingest_app.py::run_backfill
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-ny-nyc-local-awards-ingest")

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
INGEST_TIMEOUT_SECONDS = 60 * 60  # 1h ceiling per feed

R2_BUCKET = "dex-raw-landing-zone"

# Feed config — (dataset_id, socrata_domain, r2_prefix, feed_name, source_id, transform_fn_name)
FEED_NYC_CONTRACT_AWARDS = {
    "dataset_id": "qyyg-4tf5",
    "socrata_domain": "data.cityofnewyork.us",
    "r2_prefix": "nyc-contract-awards",
    "feed_name": "contract_awards",
    "source_id": "nyc_contract_awards",
    "transform_fn": "_transform_nyc_contract_awards_to_parquet",
}
FEED_MTA_PROCUREMENTS = {
    "dataset_id": "twsw-2mqa",
    "socrata_domain": "data.ny.gov",
    "r2_prefix": "ny-mta-procurements",
    "feed_name": "mta_procurements",
    "source_id": "ny_mta_procurements",
    "transform_fn": "_transform_ny_mta_procurements_to_parquet",
}
FEED_LOCAL_AUTHORITY_PROCUREMENTS = {
    "dataset_id": "8w5p-k45m",
    "socrata_domain": "data.ny.gov",
    "r2_prefix": "ny-local-authority-procurements",
    "feed_name": "local_authority_procurements",
    "source_id": "ny_local_authority_procurements",
    "transform_fn": "_transform_ny_local_authority_procurements_to_parquet",
}

SOCRATA_PAGE_SIZE = 50_000
USER_AGENT = "data-engine-x-research/1.0"
DEFAULT_CRON = "0 13 * * 1"  # Mon 13:00 UTC weekly (staggered from `0 12 * * 1`)


# ---------------------------------------------------------------------------
# Ledger helpers (mirror ny_data_construction_ingest_app.py + L21)
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
# Socrata pagination (mirror ny_data_construction_ingest_app.py:_socrata_fetch_all)
# ---------------------------------------------------------------------------

def _socrata_url(domain: str, dataset_id: str) -> str:
    return f"https://{domain}/resource/{dataset_id}.json"


def _socrata_count(domain: str, dataset_id: str, client) -> int:
    r = client.get(_socrata_url(domain, dataset_id), params={"$select": "count(*)"})
    r.raise_for_status()
    rows = r.json()
    return int(rows[0]["count"])


def _socrata_fetch_all(domain: str, dataset_id: str, client, logger) -> list[dict]:
    """Paginate through entire Socrata dataset via $limit + $offset."""
    url = _socrata_url(domain, dataset_id)
    total = _socrata_count(domain, dataset_id, client)
    logger(f"[{dataset_id}] total rows: {total:,}")

    all_rows: list[dict] = []
    offset = 0
    page = 1
    while offset < total:
        r = client.get(url, params={
            "$limit": SOCRATA_PAGE_SIZE,
            "$offset": offset,
            "$order": ":id",
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
# Helpers — name-normalization + derived columns
# ---------------------------------------------------------------------------

def _normalize_entity_name_py(s: str | None) -> str | None:
    """Lazy import of scripts._lib.entity_name_normalize inside Modal function."""
    if not s:
        return None
    sys.path.insert(0, "/root")
    from scripts._lib.entity_name_normalize import normalize_entity_name  # noqa: E402

    return normalize_entity_name(s)


def _sha1_id(parts: list[str]) -> str:
    import hashlib
    return hashlib.sha1("|".join(p or "" for p in parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Per-feed transforms — Socrata JSON → typed Parquet
# ---------------------------------------------------------------------------

def _transform_socrata_to_parquet(
    rows: list[dict],
    *,
    source_dataset_id: str,
    snapshot_date: str,
    out_basename: str,
    tmpdir: str,
    derive_fn,
) -> tuple[str, int]:
    """Generic Socrata-JSON → Parquet via JSONL→DuckDB COPY.

    derive_fn(row) MUTATES the row in-place to add derived columns (vendor_name_normalized,
    synthetic IDs, etc.). All rows then get source_dataset_id + retrieved_at + snapshot_date
    appended uniformly.
    """
    import duckdb
    import json

    if not rows:
        raise RuntimeError(f"{source_dataset_id}: no rows fetched from Socrata")

    retrieved_at = datetime.now(timezone.utc).isoformat()

    transformed: list[dict] = []
    for row in rows:
        out = {k: v for k, v in row.items() if not k.startswith(":@")}
        derive_fn(out)
        out["source_dataset_id"] = source_dataset_id
        out["retrieved_at"] = retrieved_at
        out["snapshot_date"] = snapshot_date
        transformed.append(out)

    jsonl_path = os.path.join(tmpdir, f"{out_basename}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for row in transformed:
            f.write(json.dumps(row) + "\n")

    parquet_path = os.path.join(tmpdir, f"{out_basename}.parquet")
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


def _transform_nyc_contract_awards_to_parquet(
    rows: list[dict], snapshot_date: str, tmpdir: str,
) -> tuple[str, int]:
    """qyyg-4tf5 NYC Recent Contract Awards.

    Derived cols:
      vendor_name_normalized — normalize_entity_name(vendor_name)
      contract_id            — sha1[:16] of (pin|agency_name|vendor_name|contract_amount|start_date)
                               Falls back to a request_id-based ID if pin is absent.
    Native PK candidates: pin (often present), request_id (always present).
    """
    def derive(row: dict) -> None:
        vendor = row.get("vendor_name") or ""
        row["vendor_name_normalized"] = _normalize_entity_name_py(vendor)
        row["contract_id"] = _sha1_id([
            row.get("pin", "") or "",
            row.get("agency_name", "") or "",
            vendor,
            str(row.get("contract_amount", "") or ""),
            row.get("start_date", "") or "",
            str(row.get("request_id", "") or ""),
        ])

    return _transform_socrata_to_parquet(
        rows,
        source_dataset_id="qyyg-4tf5",
        snapshot_date=snapshot_date,
        out_basename="nyc_contract_awards",
        tmpdir=tmpdir,
        derive_fn=derive,
    )


def _transform_ny_mta_procurements_to_parquet(
    rows: list[dict], snapshot_date: str, tmpdir: str,
) -> tuple[str, int]:
    """twsw-2mqa NY MTA Procurements.

    Derived cols:
      vendor_name_normalized — normalize_entity_name(vendor_name)
      contract_id            — sha1[:16] of (transaction_number|vendor_name|contract_amount|award_date)
                               transaction_number is a natural unique key on MTA side;
                               contract_id used as the cohort-aggregation surrogate to match
                               the harness's BTREE convention.
    """
    def derive(row: dict) -> None:
        vendor = row.get("vendor_name") or ""
        row["vendor_name_normalized"] = _normalize_entity_name_py(vendor)
        row["contract_id"] = _sha1_id([
            row.get("transaction_number", "") or "",
            vendor,
            str(row.get("contract_amount", "") or ""),
            row.get("award_date", "") or "",
        ])

    return _transform_socrata_to_parquet(
        rows,
        source_dataset_id="twsw-2mqa",
        snapshot_date=snapshot_date,
        out_basename="ny_mta_procurements",
        tmpdir=tmpdir,
        derive_fn=derive,
    )


def _transform_ny_local_authority_procurements_to_parquet(
    rows: list[dict], snapshot_date: str, tmpdir: str,
) -> tuple[str, int]:
    """8w5p-k45m NY Procurement Report for Local Authorities.

    Derived cols:
      vendor_name_normalized — normalize_entity_name(vendor_name)
      contract_id            — sha1[:16] of (authority_name|vendor_name|award_date|contract_amount|procurement_description)
                               No natural unique key in this dataset.
    """
    def derive(row: dict) -> None:
        vendor = row.get("vendor_name") or ""
        row["vendor_name_normalized"] = _normalize_entity_name_py(vendor)
        row["contract_id"] = _sha1_id([
            row.get("authority_name", "") or "",
            vendor,
            row.get("award_date", "") or "",
            str(row.get("contract_amount", "") or ""),
            row.get("procurement_description", "") or "",
        ])

    return _transform_socrata_to_parquet(
        rows,
        source_dataset_id="8w5p-k45m",
        snapshot_date=snapshot_date,
        out_basename="ny_local_authority_procurements",
        tmpdir=tmpdir,
        derive_fn=derive,
    )


# ---------------------------------------------------------------------------
# R2 upload (L42: ContentType only, no Content-Encoding)
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


# ---------------------------------------------------------------------------
# Per-feed Modal entrypoints
# ---------------------------------------------------------------------------

# Map transform_fn names to actual functions (Modal serialization friendly).
_TRANSFORM_FNS = {
    "_transform_nyc_contract_awards_to_parquet": _transform_nyc_contract_awards_to_parquet,
    "_transform_ny_mta_procurements_to_parquet": _transform_ny_mta_procurements_to_parquet,
    "_transform_ny_local_authority_procurements_to_parquet": _transform_ny_local_authority_procurements_to_parquet,
}


def _ingest_feed(feed_config: dict, snapshot_date: str) -> dict[str, Any]:
    import logging
    log = logging.getLogger(feed_config["source_id"])
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
        log.addHandler(h)

    _bridge_database_url()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    feed_date = date.fromisoformat(snapshot_date)
    r2_key = f"{feed_config['r2_prefix']}/snapshot={snapshot_date}/data.parquet"

    _record_run(
        run_id=run_id, source_id=feed_config["source_id"], feed_name=feed_config["feed_name"],
        feed_date=feed_date, status="running", outcome="never_ran",
        started_at=started_at, completed_at=None, duration_seconds=None,
        rows_loaded=None, landing_zone="r2",
        r2_bucket=R2_BUCKET, r2_object_key=r2_key,
        payload_format="parquet", payload_bytes=None,
        error_class=None, error_message=None,
        evidence={"dataset_id": feed_config["dataset_id"], "socrata_domain": feed_config["socrata_domain"]},
    )

    t0 = time.time()
    try:
        import httpx
        with httpx.Client(
            timeout=httpx.Timeout(60.0, read=300.0),
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            rows = _socrata_fetch_all(
                feed_config["socrata_domain"], feed_config["dataset_id"], client, log.info,
            )

        log.info("transforming %d rows to Parquet ...", len(rows))
        transform_fn = _TRANSFORM_FNS[feed_config["transform_fn"]]
        with tempfile.TemporaryDirectory() as tmpdir:
            parquet_path, row_count = transform_fn(rows, snapshot_date, tmpdir)
            log.info("Parquet ready: %s (%d rows)", parquet_path, row_count)
            size_bytes = _upload_to_r2(parquet_path, r2_key)
            log.info("uploaded to r2://%s/%s (%d bytes)", R2_BUCKET, r2_key, size_bytes)

        duration = time.time() - t0
        completed_at = datetime.now(timezone.utc).isoformat()
        _record_run(
            run_id=run_id, source_id=feed_config["source_id"], feed_name=feed_config["feed_name"],
            feed_date=feed_date,
            status="completed", outcome="succeeded_with_changes",
            started_at=started_at, completed_at=completed_at,
            duration_seconds=round(duration, 1),
            rows_loaded=row_count, landing_zone="r2",
            r2_bucket=R2_BUCKET, r2_object_key=r2_key,
            payload_format="parquet", payload_bytes=size_bytes,
            error_class=None, error_message=None,
            evidence={
                "dataset_id": feed_config["dataset_id"],
                "socrata_domain": feed_config["socrata_domain"],
                "fetched_rows": len(rows),
                "parquet_rows": row_count,
                "parquet_bytes": size_bytes,
                "snapshot_date": snapshot_date,
            },
        )

        return {
            "run_id": run_id,
            "source_id": feed_config["source_id"],
            "feed_name": feed_config["feed_name"],
            "snapshot_date": snapshot_date,
            "rows_loaded": row_count,
            "r2_key": r2_key,
            "duration_seconds": round(duration, 1),
        }

    except Exception as exc:
        duration = time.time() - t0
        completed_at = datetime.now(timezone.utc).isoformat()
        err_cls = _classify_error(exc)
        _record_run(
            run_id=run_id, source_id=feed_config["source_id"], feed_name=feed_config["feed_name"],
            feed_date=feed_date,
            status="failed", outcome="failed",
            started_at=started_at, completed_at=completed_at,
            duration_seconds=round(duration, 1),
            rows_loaded=None, landing_zone="r2",
            r2_bucket=R2_BUCKET, r2_object_key=r2_key,
            payload_format=None, payload_bytes=None,
            error_class=err_cls, error_message=repr(exc)[:2000],
            evidence={
                "dataset_id": feed_config["dataset_id"],
                "socrata_domain": feed_config["socrata_domain"],
            },
        )
        raise


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
)
def run_ingest_nyc_contract_awards(snapshot_date: str | None = None) -> dict[str, Any]:
    """Ingest NYC Recent Contract Awards (qyyg-4tf5) into R2."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    return _ingest_feed(FEED_NYC_CONTRACT_AWARDS, snapshot_date)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
)
def run_ingest_ny_mta_procurements(snapshot_date: str | None = None) -> dict[str, Any]:
    """Ingest NY MTA Procurements (twsw-2mqa) into R2."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    return _ingest_feed(FEED_MTA_PROCUREMENTS, snapshot_date)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
)
def run_ingest_ny_local_authority_procurements(snapshot_date: str | None = None) -> dict[str, Any]:
    """Ingest NY Local Authority Procurements (8w5p-k45m) into R2."""
    if snapshot_date is None:
        snapshot_date = datetime.now(timezone.utc).date().isoformat()
    return _ingest_feed(FEED_LOCAL_AUTHORITY_PROCUREMENTS, snapshot_date)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=INGEST_MEMORY_MB,
    timeout=INGEST_TIMEOUT_SECONDS,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron(DEFAULT_CRON),
)
def run_backfill(snapshot_date: str | None = None) -> dict[str, Any]:
    """Backfill all 3 feeds (also weekly cron entrypoint)."""
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
        hb.set_stage("nyc_contract_awards", {"snapshot_date": snapshot_date})
        nyc = _ingest_feed(FEED_NYC_CONTRACT_AWARDS, snapshot_date)
        hb.set_stage("mta_procurements")
        mta = _ingest_feed(FEED_MTA_PROCUREMENTS, snapshot_date)
        hb.set_stage("local_authority_procurements")
        la = _ingest_feed(FEED_LOCAL_AUTHORITY_PROCUREMENTS, snapshot_date)
    return {"run_id": run_id, "nyc_contract_awards": nyc, "mta_procurements": mta, "local_authority_procurements": la}
