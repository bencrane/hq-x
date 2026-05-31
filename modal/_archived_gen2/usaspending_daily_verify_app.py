"""USAspending daily verify — Modal cron monitoring the daily ingest pipeline.

Cycle: usaspending-pipeline-remediation (2026-05-13). Parallel to
modal/fmcsa_daily_verify_app.py — FMCSA's daily verify pattern.

Runs every day at 08:00 UTC (2h after USAspending API-daily-delta @ 06:00
UTC; gives the cron time to land + the ledger unify reconciliation a chance
to run if it's wired in-cron).

Each invocation:
  1. Runs scripts/_lib/ingest_ledger_unify.reconcile_all_usaspending() first
     so the ops ledger reflects bulk_ingest's most recent rows.
  2. subprocess `scripts/usaspending/verify_daily_ingest.py`.
  3. INSERT one row into ops.data_source_ingest_runs for source
     'usaspending_contracts_lance':
       - on success: status='succeeded', run_metadata captures verify stdout tail
       - on failure: status='failed', error_message + run_metadata captures stderr
  4. On failure, also INSERT a row into ops.alert_emissions referencing the
     pre-seeded ingest_failed Telegram alert subscription.

Idempotency: each invocation produces a NEW row (observability rows are
append-only by design). Same-day re-runs are allowed.

Secrets required (Modal):
    dex-db   — DEX_DB_URL_DIRECT for ops.data_source_ingest_runs writes
                        (the secret is shared across DEX ingests; same DB).
    bulk-ingest-r2    — R2 endpoint + keys for verify's R2 poison-guard check.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_daily_verify_app.py

Manual invocation:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/usaspending_daily_verify_app.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-usaspending-daily-verify")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]", "boto3")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

VERIFY_TIMEOUT_SECONDS = 5 * 60

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the verify script reads DEX_DB_URL_POOLED
    OR DEX_DB_URL_DIRECT. Bridge across so subprocess works."""
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _reconcile_ledger() -> int:
    """Mirror bulk_ingest USAspending rows into ops.data_source_ingest_runs."""
    sys.path.insert(0, "/root/scripts")
    try:
        from _lib.ingest_ledger_unify import reconcile_all_usaspending  # type: ignore
    except Exception as exc:  # noqa: BLE001
        logger.warning("ingest_ledger_unify import failed: %s", exc)
        return 0
    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        return 0
    try:
        return reconcile_all_usaspending(db_url=db_url, since_interval="2 days")
    except Exception as exc:  # noqa: BLE001
        logger.warning("reconcile_all_usaspending raised: %s", exc)
        return 0


def _record_run(
    status: str,
    stdout_tail: str,
    stderr_tail: str,
    returncode: int,
    started_at: str,
    mirrored_rows: int,
) -> str:
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("no DB URL available (need DEX_DB_URL_DIRECT or DATABASE_URL)")

    completed_at = datetime.now(timezone.utc).isoformat()
    run_metadata = json.dumps(
        {
            "writer": "usaspending-daily-verify",
            "verify_returncode": returncode,
            "stdout_tail": stdout_tail[-2000:],
            "stderr_tail": stderr_tail[-2000:],
            "started_at": started_at,
            "ledger_mirrored_rows": mirrored_rows,
        }
    )
    error_message = stderr_tail[-1000:] if status == "failed" else None

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.data_source_ingest_runs
                    (source_id, started_at, completed_at, status, run_metadata, error_message)
                SELECT s.source_id, %s::timestamptz, %s::timestamptz, %s::data_source_run_status, %s::jsonb, %s
                  FROM ops.data_sources s
                 WHERE s.display_name = 'usaspending_contracts_lance'
                RETURNING run_id
                """,
                (started_at, completed_at, status, run_metadata, error_message),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("usaspending_contracts_lance not found in ops.data_sources")
            conn.commit()
            return str(row[0])


def _emit_alert(reason_payload: dict) -> None:
    """Find the ingest_failed Telegram alert subscription for USAspending and emit one row."""
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        return

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.alert_id
                  FROM ops.alert_subscriptions a
                  JOIN ops.data_sources s ON s.source_id = a.source_id
                 WHERE s.display_name = 'usaspending_contracts_lance'
                   AND a.alert_kind = 'ingest_failed'
                   AND a.channel = 'telegram'
                   AND a.enabled = true
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("no ingest_failed alert subscription for USAspending; skipping alert emission")
                return
            alert_id = row[0]

            cur.execute(
                """
                INSERT INTO ops.alert_emissions (alert_id, alert_payload, delivery_status)
                VALUES (%s, %s::jsonb, 'sent'::alert_delivery_status)
                """,
                (alert_id, json.dumps(reason_payload)),
            )
            conn.commit()


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=VERIFY_TIMEOUT_SECONDS,
    # [migrated 2026-05-29 -> Trigger.dev shared dispatcher] schedule=modal.Cron("0 8 * * *"),  # 08:00 UTC daily (2h after API-daily-delta)
)
def run_daily_verify() -> dict:
    """Run reconcile + scripts/usaspending/verify_daily_ingest.py, record outcome."""
    _bridge_database_url()
    started_at = datetime.now(timezone.utc).isoformat()

    mirrored = _reconcile_ledger()
    logger.info("ledger reconciliation mirrored %d bulk_ingest rows", mirrored)

    result = subprocess.run(
        [sys.executable, "/root/scripts/usaspending/verify_daily_ingest.py"],
        capture_output=True,
        text=True,
        env=os.environ,
        check=False,
    )

    status = "succeeded" if result.returncode == 0 else "failed"
    run_id = _record_run(
        status=status,
        stdout_tail=result.stdout,
        stderr_tail=result.stderr,
        returncode=result.returncode,
        started_at=started_at,
        mirrored_rows=mirrored,
    )

    if status == "failed":
        _emit_alert(
            {
                "writer": "usaspending-daily-verify",
                "run_id": run_id,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-1500:],
                "summary": "USAspending daily verify check FAILED",
            }
        )

    summary = {
        "started_at": started_at,
        "run_id": run_id,
        "status": status,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
        "mirrored_rows": mirrored,
    }
    if status == "failed":
        raise RuntimeError(
            f"USAspending daily verify FAILED (run_id={run_id}); see ops.data_source_ingest_runs"
        )
    return summary


@app.local_entrypoint()
def main() -> None:
    """Manual entrypoint for one-off testing (`modal run`)."""
    out = run_daily_verify.remote()
    print(json.dumps(out, indent=2, default=str))
