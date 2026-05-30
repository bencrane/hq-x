"""FMCSA daily verify — Modal cron monitoring the daily ingest pipeline.

Runs every day at 07:30 UTC (90min after factory at 06:00 UTC, 90min after
material_change_detection at 06:00 cron tick — gives both upstream cycles
time to finish + write).

Each invocation:
  1. subprocess `scripts/fmcsa/verify_daily_ingest.py`
  2. INSERT one row into ops.data_source_ingest_runs for source 'fmcsa_carrier_essentials':
       - on success: status='succeeded', run_metadata captures verify stdout tail
       - on failure: status='failed', error_message + run_metadata captures stderr
  3. On failure, also INSERT a row into ops.alert_emissions referencing the
     pre-seeded ingest_failed Telegram alert subscription.

Idempotency: each invocation produces a NEW row (observability rows are
append-only by design). Same-day re-runs are allowed.

Secrets required (Modal):
    dex-db — DEX_DB_URL_DIRECT for ops.data_source_ingest_runs writes.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_daily_verify_app.py

Manual invocation:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/fmcsa_daily_verify_app.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-fmcsa-daily-verify")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
]

VERIFY_TIMEOUT_SECONDS = 5 * 60  # plenty for a verify script that talks to DB only

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; the verify script reads DEX_DB_URL_POOLED
    OR DEX_DB_URL_DIRECT. Bridge across so subprocess works."""
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _record_run(
    status: str,
    stdout_tail: str,
    stderr_tail: str,
    returncode: int,
    started_at: str,
) -> str:
    """INSERT one row into ops.data_source_ingest_runs; return run_id (uuid str)."""
    import psycopg  # imported at function-time (Modal image installs psycopg)

    db_url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("no DB URL available (need DEX_DB_URL_DIRECT or DATABASE_URL)")

    completed_at = datetime.now(timezone.utc).isoformat()
    run_metadata = json.dumps(
        {
            "writer": "fmcsa-daily-verify",
            "verify_returncode": returncode,
            "stdout_tail": stdout_tail[-2000:],
            "stderr_tail": stderr_tail[-2000:],
            "started_at": started_at,
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
                 WHERE s.display_name = 'fmcsa_carrier_essentials'
                RETURNING run_id
                """,
                (started_at, completed_at, status, run_metadata, error_message),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError("fmcsa_carrier_essentials not found in ops.data_sources")
            conn.commit()
            return str(row[0])


def _emit_alert(reason_payload: dict) -> None:
    """Find the ingest_failed Telegram alert subscription for FMCSA and emit one row."""
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
                 WHERE s.display_name = 'fmcsa_carrier_essentials'
                   AND a.alert_kind = 'ingest_failed'
                   AND a.channel = 'telegram'
                   AND a.enabled = true
                 LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                logger.warning("no ingest_failed alert subscription for FMCSA; skipping alert emission")
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
    schedule=modal.Cron("30 7 * * *"),  # 07:30 UTC daily
)
def run_daily_verify() -> dict:
    """Run scripts/fmcsa/verify_daily_ingest.py and record outcome."""
    _bridge_database_url()
    started_at = datetime.now(timezone.utc).isoformat()

    result = subprocess.run(
        [sys.executable, "/root/scripts/fmcsa/verify_daily_ingest.py"],
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
    )

    if status == "failed":
        _emit_alert(
            {
                "writer": "fmcsa-daily-verify",
                "run_id": run_id,
                "returncode": result.returncode,
                "stderr_tail": result.stderr[-1500:],
                "summary": "FMCSA daily verify check FAILED",
            }
        )

    summary = {
        "started_at": started_at,
        "run_id": run_id,
        "status": status,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-500:],
    }
    if status == "failed":
        # Raise so Modal dashboard marks the cron run red.
        raise RuntimeError(f"FMCSA daily verify FAILED (run_id={run_id}); see ops.data_source_ingest_runs")
    return summary


@app.local_entrypoint()
def main() -> None:
    """Manual entrypoint for one-off testing (`modal run`)."""
    out = run_daily_verify.remote()
    print(json.dumps(out, indent=2, default=str))
