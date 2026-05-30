#!/usr/bin/env python3
"""One-shot backfill: USAspending API-daily-delta for action_date 2026-05-10.

Cycle: usaspending-pipeline-remediation (2026-05-13).

Background:
    The cron at modal/usaspending_api_daily_app.py landed a 0-byte parquet at
        s3://dex-raw-landing-zone/usaspending/contracts/api-delta/date=2026-05-11/data.parquet
    on the run that should have picked up action_date=2026-05-10 activity. The
    audit (predecessor cycle scope-usaspending-data-integrity-audit) flagged
    ~8,183 missing contracts.

What this script does:
    1. HEAD the canonical R2 key. If size == 0, DELETE it (the poison guard).
       If size > 0, ABORT (we are NOT going to overwrite real data).
    2. Invoke run_ingest from scripts.run_usaspending_api_daily_ingest with
       feed_date=2026-05-10, writing to the canonical key.
    3. Log to ops.data_source_ingest_runs (success or failure) with
       idempotency_key='usaspending_backfill_2026_05_10' so re-runs are no-ops
       after first success.

Usage (operator runs once post-merge):
    cd apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        bash -c 'uv run python -m scripts.usaspending.backfill_missing_2026_05_10'

Exit codes:
    0 = backfill landed (or was already complete; idempotency hit).
    2 = aborted because R2 key already holds non-empty data (operator must
        inspect and decide whether to delete-and-rerun).
    other = upstream failure (R2, USAspending API, or DB) — see stderr.

Forward-only; rollback = delete the parquet (back to poison-deleted state).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import date, datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

FEED_DATE = date(2026, 5, 10)
R2_BUCKET = "dex-raw-landing-zone"
R2_OBJECT_KEY = "usaspending/contracts/api-delta/date=2026-05-11/data.parquet"
IDEMPOTENCY_KEY = "usaspending_backfill_2026_05_10"
SOURCE_DISPLAY_NAME = "usaspending_api_daily"


def _r2_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _head_size_or_none(client, bucket: str, key: str) -> int | None:
    from botocore.exceptions import ClientError

    try:
        meta = client.head_object(Bucket=bucket, Key=key)
        return int(meta.get("ContentLength", 0))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise


def _resolve_source_id(conn, display_name: str) -> str:
    row = conn.execute(
        "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
        (display_name,),
    ).fetchone()
    if row is None:
        log.error("source display_name=%r not registered in ops.data_sources", display_name)
        sys.exit(64)
    return str(row[0])


def _already_backfilled(conn) -> bool:
    """Check whether this backfill already landed (idempotency_key in run_metadata)."""
    row = conn.execute(
        """
        SELECT 1
          FROM ops.data_source_ingest_runs
         WHERE run_metadata->>'idempotency_key' = %s
           AND status = 'succeeded'
         LIMIT 1
        """,
        (IDEMPOTENCY_KEY,),
    ).fetchone()
    return row is not None


def _record_run(conn, source_id: str, status: str, run_metadata: dict, error_message: str | None) -> None:
    conn.execute(
        """
        INSERT INTO ops.data_source_ingest_runs
            (source_id, started_at, completed_at, status, run_metadata, error_message)
        VALUES (%s, NOW(), NOW(), %s::data_source_run_status, %s::jsonb, %s)
        """,
        (source_id, status, json.dumps(run_metadata), error_message),
    )


def main() -> int:
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not db_url:
        log.error("DEX_DB_URL_DIRECT (or DEX_DB_URL_POOLED) must be set (Doppler hq-all/prd)")
        return 64

    import psycopg

    with psycopg.connect(db_url, autocommit=True) as conn:
        source_id = _resolve_source_id(conn, SOURCE_DISPLAY_NAME)

        if _already_backfilled(conn):
            log.info("idempotency hit: %s already succeeded; nothing to do", IDEMPOTENCY_KEY)
            return 0

        # 1. R2 poison-or-empty guard.
        s3 = _r2_client()
        size = _head_size_or_none(s3, R2_BUCKET, R2_OBJECT_KEY)
        if size is None:
            log.info("R2 key absent: %s — will write fresh", R2_OBJECT_KEY)
        elif size == 0:
            log.info("R2 key is 0-byte poison: deleting %s", R2_OBJECT_KEY)
            s3.delete_object(Bucket=R2_BUCKET, Key=R2_OBJECT_KEY)
        else:
            log.error(
                "R2 key already holds %d bytes (non-empty): %s. "
                "Aborting to avoid overwriting real data. Operator must "
                "manually delete-and-rerun if backfill is intentional.",
                size,
                R2_OBJECT_KEY,
            )
            return 2

        # 2. Run the canonical API-daily-delta ingest for feed_date=2026-05-10.
        # Path bridges identical to modal/usaspending_api_daily_app.py.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from run_usaspending_api_daily_ingest import run_ingest  # type: ignore

        run_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = run_ingest(
                feed_date=FEED_DATE,
                run_id=run_id,
                r2_object_key=R2_OBJECT_KEY,
                max_api_calls=500,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("backfill run_ingest raised")
            _record_run(
                conn,
                source_id,
                "failed",
                {
                    "idempotency_key": IDEMPOTENCY_KEY,
                    "writer": "usaspending-backfill-2026-05-10",
                    "feed_date": FEED_DATE.isoformat(),
                    "r2_object_key": R2_OBJECT_KEY,
                    "started_at": started_at,
                },
                str(exc)[:4000],
            )
            return 1

        rows_loaded = int(result.get("rows_loaded") or 0)
        log.info(
            "backfill complete: rows_loaded=%d r2_object_key=%s",
            rows_loaded,
            R2_OBJECT_KEY,
        )
        _record_run(
            conn,
            source_id,
            "succeeded",
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "writer": "usaspending-backfill-2026-05-10",
                "feed_date": FEED_DATE.isoformat(),
                "r2_object_key": R2_OBJECT_KEY,
                "rows_loaded": rows_loaded,
                "api_calls": result.get("api_calls"),
                "payload_bytes": result.get("payload_bytes"),
                "started_at": started_at,
            },
            None,
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
