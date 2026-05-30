"""USAspending REST API daily-delta — Modal-hosted synchronous paginated ingest.

Closes the ~30-day staleness gap between monthly bulk-archive drops by
querying USAspending's synchronous `/api/v2/search/spending_by_transaction/`
endpoint for transactions modified in the prior 24h UTC window. Lands a
single Parquet per day at:

    s3://dex-raw-landing-zone/usaspending/contracts/api-delta/date={YYYY-MM-DD}/data.parquet

This sits alongside the existing `data-engine-x-usaspending-daily-ingest`
app (which uses the async bulk_download API and writes to a disjoint R2
prefix); downstream DuckDB consumers UNION ALL across the two.

Schedule: daily at 06:00 UTC. Selected to fire after USAspending's
nightly ETL has settled (~05:00 UTC reported) but before business hours.
Configurable via env override `MODAL_USA_API_CRON`.

Secrets required (Modal):
    dex-db   — DATABASE_URL pooled to data-engine-x (the
                        bulk_ingest ledger lives here).
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_api_daily_app.py

Manual backfill (single day):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_api_daily_app.py::run_api_daily_delta \\
        --target-date=2026-05-09
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-usaspending-api-daily-delta")

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

# Daily delta is small (~10-50K rows expected) — well within 2GB/1h.
# Generous timeout absorbs 429 backoff bursts and USAspending slow periods.
INGEST_MEMORY_MB = 2048
INGEST_TIMEOUT_SECONDS = 60 * 60  # 1h ceiling

SOURCE_ID = "usaspending_api_daily"
FEED_NAME = "contracts_api_delta"
DEFAULT_CRON = "0 6 * * *"  # 06:00 UTC daily


# Ledger writer + exception classifier live in `modal/landing/ledger.py`
# (canonical helper per P0-2). The `_bridge_database_url` helper was deleted
# because `dex-db` injects DEX_DB_URL_POOLED + DEX_DB_URL_DIRECT directly
# (empirically verified by `modal/db_secret_probe_app.py`); the bridge was a
# no-op on the new secret.


def _r2_object_key(*, feed_date: date) -> str:
    """Per-date Hive-style key. New key per day → no L45 RW collision risk."""
    return (
        f"usaspending/contracts/api-delta/"
        f"date={feed_date.isoformat()}/data.parquet"
    )


# Trigger.dev dispatch endpoint (Modal-cron -> Trigger.dev scheduling migration).
# Trigger task apps/hq-x/src/trigger/usaspending-contracts-delta-daily.ts POSTs
# this daily; we spawn run_api_daily_delta async and return the call_id
# immediately (fire-and-forget — the ingest runs in Modal, never in Trigger.dev).
@app.function(image=image, timeout=60)
@modal.fastapi_endpoint(method="POST")
def trigger_contracts_delta_via_http(target_date: str | None = None) -> dict:
    call = run_api_daily_delta.spawn(target_date=target_date)
    return {"call_id": call.object_id, "target_date": target_date}


# retry-policy: modal-retries-ip-throttle
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-29] schedule moved to Trigger.dev (see dispatch endpoint above).
    # Fresh container per attempt = fresh egress IP — the only thing that
    # bypasses USAspending's F5 BotDefense persistent disconnect (2026-05-25
    # post-mortem: 2026-05-21 deep pagination died after ~30-40 pages from
    # the same Modal egress IP; per-IP throttle held > 15 min; script-level
    # `_post_with_backoff` was neutered the same day so failures bubble up
    # to this decorator). 30s initial → 60s → 2m → 4m → 8m (~16m envelope).
    retries=modal.Retries(
        max_retries=5,
        backoff_coefficient=2.0,
        initial_delay=30.0,
    ),
)
def run_api_daily_delta(
    target_date: str | None = None,
    max_api_calls: int = 500,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Daily entry point. Defaults to (yesterday UTC) when called by cron;
    accepts an explicit target_date for backfill / smoke tests."""

    feed_date: date
    if target_date:
        feed_date = date.fromisoformat(target_date)
    else:
        feed_date = (datetime.now(timezone.utc) - timedelta(days=1)).date()

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()
    object_key = _r2_object_key(feed_date=feed_date)

    # Mount the landing/ + scripts/ libs (added to the image above).
    sys.path.insert(0, "/root")
    sys.path.insert(0, "/root/scripts")
    from landing.ledger import (  # noqa: E402
        HeartbeatLoop,
        RunResult,
        classify_exception,
        compute_outcome,
        record_run,
    )
    from run_usaspending_api_daily_ingest import run_ingest  # noqa: E402

    record_run(
        source_id=SOURCE_ID,
        feed_name=FEED_NAME,
        run_id=run_id,
        feed_date=feed_date,
        started_at=started_at,
        completed_at=None,
        result=None,                # pre-record → status=running, outcome=never_ran
        landing_zone="r2",
        r2_bucket="dex-raw-landing-zone",
        r2_object_key=object_key,
        payload_format="parquet_zstd",
        evidence={
            "feed_date": feed_date.isoformat(),
            "trigger": "schedule" if target_date is None else "manual",
            "max_api_calls": max_api_calls,
            "dry_run": dry_run,
        },
    )

    try:
        with HeartbeatLoop(
            cron_app=app.name,
            cron_function="run_api_daily_delta",
            run_id=run_id,
        ) as hb:
            hb.set_stage("api_ingest_to_r2", {"feed_date": feed_date.isoformat(), "max_api_calls": max_api_calls})
            result = run_ingest(
                feed_date=feed_date,
                run_id=run_id,
                r2_object_key=object_key,
                max_api_calls=max_api_calls,
                dry_run=dry_run,
            )
    except Exception as exc:  # noqa: BLE001
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_seconds = (
            datetime.fromisoformat(completed_at)
            - datetime.fromisoformat(started_at)
        ).total_seconds()
        record_run(
            source_id=SOURCE_ID,
            feed_name=FEED_NAME,
            run_id=run_id,
            feed_date=feed_date,
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            result=None,
            error_class=classify_exception(exc),
            error_message=str(exc)[:4000],
            landing_zone="r2",
            r2_bucket="dex-raw-landing-zone",
            r2_object_key=object_key,
            payload_format="parquet_zstd",
            evidence={"feed_date": feed_date.isoformat()},
        )
        raise

    completed_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = (
        datetime.fromisoformat(completed_at)
        - datetime.fromisoformat(started_at)
    ).total_seconds()

    rows_loaded = int(result.get("rows_loaded", 0))
    payload_bytes = int(result.get("payload_bytes", 0))
    skipped_existing = bool(result.get("skipped_existing"))

    run_result = RunResult(
        rows_loaded=rows_loaded,
        is_dry_run=bool(dry_run),
        skipped_idempotent=skipped_existing,
    )
    record_run(
        source_id=SOURCE_ID,
        feed_name=FEED_NAME,
        run_id=run_id,
        feed_date=feed_date,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        result=run_result,
        landing_zone="r2",
        r2_bucket="dex-raw-landing-zone",
        r2_object_key=object_key,
        payload_format="parquet_zstd",
        payload_bytes=payload_bytes,
        evidence={
            "feed_date": feed_date.isoformat(),
            "trigger": "schedule" if target_date is None else "manual",
            **{k: v for k, v in result.items() if k not in ("rows_loaded", "payload_bytes")},
        },
    )

    _status, outcome = compute_outcome(run_result)
    return {
        "run_id": run_id,
        "feed_date": feed_date.isoformat(),
        "rows_loaded": rows_loaded,
        "payload_bytes": payload_bytes,
        "r2_object_key": object_key,
        "duration_seconds": duration_seconds,
        "outcome": outcome,
    }
