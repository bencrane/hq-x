"""USAspending Daily Drip — Modal-hosted daily delta ingest.

Pulls yesterday's contract awards from USAspending's bulk-download API,
streams the result into ZSTD-compressed Parquet under R2, and records the
run in bulk_ingest.feed_ingest_runs (source_id='usaspending_daily').

Object key layout (Hive-partitioned for RisingWave match_pattern globbing):

    s3://dex-raw-landing-zone/usaspending/contracts/year=YYYY/month=MM/day=DD/{run_id}.parquet.zst

The matching RisingWave SOURCE in risingwave/usaspending_daily.sql uses
match_pattern = 'usaspending/contracts/year=*/month=*/day=*/*.parquet' to
pick up new partitions as they land — no source rebuild needed per day.

Schedule: daily at 05:00 UTC (≈01:00 ET). Late enough that USAspending's
nightly ETL has settled, early enough to land before business hours.

Secrets required (Modal):
    dex-db   — DATABASE_URL pooled to data-engine-x (the bulk_ingest
                        ledger lives here). Reused from the FMCSA pipeline
                        because the ledger is shared.
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_daily_app.py

Manual run (e.g. backfill yesterday after a Modal cron drop):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_daily_app.py::run_daily_drip \\
        --feed-date=2026-05-06
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-daily-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    # USAspending's TLS chain trips Modal's stale Debian CA bundle on first
    # request ("self-signed certificate in certificate chain"). Refresh both
    # the system bundle and certifi so httpx + boto3 trust the chain.
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

# 8GB / 4h sized for a single day's national contract delta — well below
# FMCSA's xl bucket but above the small-feed default. USAspending volume is
# bursty (Q4 fiscal-year-end can quintuple); 4h leaves room without
# forcing the xl bucket.
INGEST_MEMORY_MB = 8192
INGEST_TIMEOUT_SECONDS = 4 * 60 * 60

SOURCE_ID = "usaspending_daily"
FEED_NAME = "contracts"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the bulk_ingest writers expect
    DEX_DB_URL_POOLED. Mirror it across so both readers work."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _record_run(
    *,
    run_id: str,
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
    """UPSERT a single row into bulk_ingest.feed_ingest_runs.

    USAspending only ever runs once per (run_id, feed_date) so the conflict
    resolution updates everything — last write wins.
    """
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
                    %s, %s, %s::jsonb
                )
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE
                SET
                    status = EXCLUDED.status,
                    outcome = EXCLUDED.outcome,
                    completed_at = EXCLUDED.completed_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    rows_loaded = EXCLUDED.rows_loaded,
                    landing_zone = EXCLUDED.landing_zone,
                    r2_bucket = EXCLUDED.r2_bucket,
                    r2_object_key = EXCLUDED.r2_object_key,
                    payload_format = EXCLUDED.payload_format,
                    payload_bytes = EXCLUDED.payload_bytes,
                    error_class = EXCLUDED.error_class,
                    error_message = EXCLUDED.error_message,
                    evidence = COALESCE(bulk_ingest.feed_ingest_runs.evidence, '{}'::jsonb)
                               || EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    SOURCE_ID,
                    FEED_NAME,
                    feed_date.isoformat(),
                    status,
                    outcome,
                    started_at,
                    completed_at,
                    duration_seconds,
                    rows_loaded,
                    landing_zone,
                    r2_bucket,
                    r2_object_key,
                    payload_format,
                    payload_bytes,
                    error_class,
                    error_message,
                    json.dumps(evidence, default=str),
                ),
            )
        conn.commit()


def _r2_object_key(*, feed_date: date, run_id: str) -> str:
    """Hive-partitioned R2 object key for the daily delta.

    Mirrors the RisingWave match_pattern. Run_id in the filename keeps
    re-runs of the same day idempotent at the object level — re-running
    creates a new object rather than overwriting yesterday's.
    """
    return (
        f"usaspending/contracts/"
        f"year={feed_date.year:04d}/month={feed_date.month:02d}/day={feed_date.day:02d}/"
        f"{run_id}.parquet"
    )


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# Trigger task apps/hq-x/src/trigger/usaspending-bulk-drip-daily.ts POSTs this
# daily; we spawn run_daily_drip async and return the call_id immediately
# (fire-and-forget — ingest runs in Modal).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_daily_drip_via_http(feed_date: str | None = None) -> dict:
    call = run_daily_drip.spawn(feed_date=feed_date)
    return {"call_id": call.object_id, "feed_date": feed_date}


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
)
def run_daily_drip(feed_date: str | None = None) -> dict[str, Any]:
    """Daily entry point. Defaults to (yesterday UTC) when called by cron;
    accepts an explicit feed_date for backfill / smoke tests."""
    _bridge_database_url()

    target_date: date
    if feed_date:
        target_date = date.fromisoformat(feed_date)
    else:
        target_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    object_key = _r2_object_key(feed_date=target_date, run_id=run_id)

    # Mark running before the work starts so the dispatch view shows
    # IN_FLIGHT (RUNNING in operator's 8-state spec) immediately.
    _record_run(
        run_id=run_id,
        feed_date=target_date,
        status="running",
        outcome="never_ran",
        started_at=started_at,
        completed_at=None,
        duration_seconds=None,
        rows_loaded=None,
        landing_zone="r2",
        r2_bucket="dex-raw-landing-zone",
        r2_object_key=object_key,
        payload_format="parquet_zstd",
        payload_bytes=None,
        error_class=None,
        error_message=None,
        evidence={
            "feed_date": target_date.isoformat(),
            "trigger": "schedule" if feed_date is None else "manual",
        },
    )

    sys.path.insert(0, "/root/scripts")
    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402
    from run_usaspending_daily_ingest import run_ingest  # noqa: E402

    try:
        with HeartbeatLoop(
            cron_app=app.name,
            cron_function="run_daily_drip",
            run_id=run_id,
        ) as hb:
            hb.set_stage("daily_ingest_to_r2", {"feed_date": target_date.isoformat()})
            result = run_ingest(
                feed_date=target_date,
                run_id=run_id,
                r2_object_key=object_key,
            )
    except Exception as exc:  # noqa: BLE001
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_seconds = (
            datetime.fromisoformat(completed_at)
            - datetime.fromisoformat(started_at)
        ).total_seconds()
        error_class = _classify_exception(exc)
        _record_run(
            run_id=run_id,
            feed_date=target_date,
            status="failed",
            outcome="failed",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            rows_loaded=None,
            landing_zone="r2",
            r2_bucket="dex-raw-landing-zone",
            r2_object_key=object_key,
            payload_format="parquet_zstd",
            payload_bytes=None,
            error_class=error_class,
            error_message=str(exc)[:4000],
            evidence={"feed_date": target_date.isoformat()},
        )
        raise

    completed_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = (
        datetime.fromisoformat(completed_at)
        - datetime.fromisoformat(started_at)
    ).total_seconds()

    rows_loaded = int(result.get("rows_loaded", 0))
    payload_bytes = int(result.get("payload_bytes", 0))
    outcome = (
        "succeeded_with_changes" if rows_loaded > 0 else "succeeded_with_zero_new_rows"
    )

    _record_run(
        run_id=run_id,
        feed_date=target_date,
        status="completed",
        outcome=outcome,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        rows_loaded=rows_loaded,
        landing_zone="r2",
        r2_bucket="dex-raw-landing-zone",
        r2_object_key=object_key,
        payload_format="parquet_zstd",
        payload_bytes=payload_bytes,
        error_class=None,
        error_message=None,
        evidence={
            "feed_date": target_date.isoformat(),
            "trigger": "schedule" if feed_date is None else "manual",
            **{k: v for k, v in result.items() if k not in ("rows_loaded", "payload_bytes")},
        },
    )

    return {
        "run_id": run_id,
        "feed_date": target_date.isoformat(),
        "rows_loaded": rows_loaded,
        "payload_bytes": payload_bytes,
        "r2_object_key": object_key,
        "duration_seconds": duration_seconds,
        "outcome": outcome,
    }


def _classify_exception(exc: BaseException) -> str:
    """Heuristic mapping to bulk_ingest.feed_ingest_runs.error_class taxonomy."""
    message = (str(exc) or "").lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    type_name = type(exc).__name__.lower()
    module_name = (type(exc).__module__ or "").lower()
    if "boto" in module_name or "s3" in module_name or "r2" in message:
        return "r2_failure"
    if "psycopg" in module_name or "operationalerror" in type_name:
        return "db_failure"
    if "httpx" in module_name or "requests" in module_name or "connection" in type_name:
        return "download_failure"
    if type_name in {"valueerror", "keyerror", "typeerror"}:
        return "parse_failure"
    return "unknown"
