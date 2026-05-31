"""Phase 0c — Modal cron app: every 15 min, calls DEX `/alerts/run-cycle`.

Triggers Telegram alerts on SLA breach + ingest_failed for every enabled
ops.alert_subscriptions row. Per-subscription dedup_window_seconds (default
4h) prevents repeat alerts.

Modal secret required (one-time setup):

    doppler run --project hq-all --config prd -- bash -c '
        modal secret create --force dex-alerter-telegram \\
            DEX_API_BASE_URL="$DEX_API_BASE_URL" \\
            DEX_SERVICE_TOKEN="$DEX_SERVICE_TOKEN"
    '

(TELEGRAM_BOT_TOKEN and TELEGRAM_ALERT_CHAT_ID live in DEX's env via
Doppler hq-all/prd; the Modal cron only needs to call DEX, not Telegram
directly.)

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/alerter_cron_app.py

Manual run (e.g. test):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/alerter_cron_app.py::run_cycle
"""

from __future__ import annotations

import os
from typing import Any

import modal

app = modal.App("data-engine-x-alerter-cron")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install("httpx>=0.27.0", "certifi>=2024.7.4", "psycopg[binary]>=3.2")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("dex-alerter-telegram"),
    modal.Secret.from_name("hqx-db"),  # for ops.cron_heartbeats staleness check (P1-1)
]

CYCLE_TIMEOUT_SECONDS = 60  # alerter cycle should be fast; cron-safe upper bound
CRON_EXPRESSION = "*/15 * * * *"  # every 15 min UTC

# Heartbeat staleness threshold. Any cron whose latest heartbeat is older
# than this is flagged as `failed_orchestrator_crashed`-suspect. Set to 30 min
# so long-tail crons (FMCSA factory ~1.5h, SEC IAPD parse ~60min) with the
# default 60s heartbeat interval get plenty of slack before alerting.
HEARTBEAT_STALENESS_THRESHOLD_SECONDS = 30 * 60


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=CYCLE_TIMEOUT_SECONDS,
    # [NEUTRALIZED 2026-05-30: DEX FastAPI deprecation — run_cycle POSTs to
    # DEX_API_BASE_URL/api/v1/internal/observability/alerts/run-cycle, which is
    # being removed with apps/data-engine-x/app. Cron disabled so the app still
    # deploys but never auto-fires. Re-point to the new alert endpoint (or
    # re-home to Trigger.dev) before re-enabling.]
    # schedule=modal.Cron(CRON_EXPRESSION),
)
def run_cycle() -> dict[str, Any]:
    """Call DEX /alerts/run-cycle and return the summary dict."""
    import httpx

    api_base = os.environ.get("DEX_API_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("DEX_SERVICE_TOKEN")
    if not api_base:
        raise RuntimeError("DEX_API_BASE_URL not set in Modal secret dex-alerter-telegram")
    if not api_key:
        raise RuntimeError("DEX_SERVICE_TOKEN not set in Modal secret dex-alerter-telegram")

    url = f"{api_base}/api/v1/internal/observability/alerts/run-cycle"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    with httpx.Client(timeout=CYCLE_TIMEOUT_SECONDS) as client:
        resp = client.post(url, headers=headers, json={})

    if resp.status_code != 200:
        raise RuntimeError(
            f"alerter run-cycle failed: HTTP {resp.status_code} — {resp.text[:300]}"
        )

    summary: dict[str, Any] = resp.json()
    print(
        "[alerter_cron] sent={alerts_sent} failed={alerts_failed} "
        "skipped_dedup={alerts_skipped_dedup} lock_skipped={lock_skipped} ts={ts}".format(
            **summary
        )
    )

    # P1-1: heartbeat staleness check. Surfaces orchestrators that have a
    # `running` status row in bulk_ingest.feed_ingest_runs but no recent
    # heartbeat in ops.cron_heartbeats — the canonical
    # `failed_orchestrator_crashed` shape.
    stale = _check_stale_heartbeats()
    summary["stale_heartbeats"] = stale
    summary["stale_heartbeats_flipped"] = 0
    if stale:
        print(f"[alerter_cron] STALE HEARTBEATS detected ({len(stale)}):")
        for row in stale:
            print(
                "  - cron_app={cron_app} run_id={run_id} "
                "last_heartbeat_at={last_heartbeat_at} "
                "seconds_since={seconds_since:.0f} stage={stage}".format(**row)
            )
        # P0-C fix (2026-05-25 adversarial audit): the staleness check used to
        # only log. Now it also flips the matching ledger row from
        # status='running' to status='failed' / outcome='failed_orchestrator_crashed'
        # so downstream queries that filter on outcome correctly bucket these
        # runs as failed rather than treating them as in-flight forever.
        flipped = _flip_stale_runs_to_crashed(stale)
        summary["stale_heartbeats_flipped"] = flipped
        if flipped:
            print(f"[alerter_cron] flipped {flipped} stale rows to failed_orchestrator_crashed")

    return summary


def _flip_stale_runs_to_crashed(stale: list[dict[str, Any]]) -> int:
    """UPDATE bulk_ingest.feed_ingest_runs status='failed' /
    outcome='failed_orchestrator_crashed' for every stale heartbeat row.

    Returns the number of rows flipped. Idempotent (the WHERE clause
    restricts to status='running' so a re-flip is a no-op)."""
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        return 0

    run_ids = [row["run_id"] for row in stale if row.get("run_id")]
    if not run_ids:
        return 0

    sql = """
        UPDATE bulk_ingest.feed_ingest_runs
           SET status = 'failed',
               outcome = 'failed_orchestrator_crashed',
               completed_at = NOW(),
               error_message = COALESCE(error_message,
                   'heartbeat stale > %s seconds; orchestrator presumed crashed by alerter'),
               updated_at = NOW()
         WHERE run_id::text = ANY(%s)
           AND status = 'running'
    """
    flipped = 0
    try:
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (HEARTBEAT_STALENESS_THRESHOLD_SECONDS, run_ids))
                flipped = cur.rowcount or 0
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        print(f"[alerter_cron] flip-stale-runs UPDATE failed: {type(exc).__name__}: {exc}")
        return 0
    return flipped


def _check_stale_heartbeats() -> list[dict[str, Any]]:
    """Query ops.cron_heartbeats for runs whose latest heartbeat is older
    than HEARTBEAT_STALENESS_THRESHOLD_SECONDS. Returns list of dicts.

    Cross-joined with bulk_ingest.feed_ingest_runs so we only flag stale
    heartbeats for runs still in 'running' status — completed/failed runs
    legitimately stop heartbeating.
    """
    import psycopg

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        print("[alerter_cron] WARN: DEX_DB_URL_POOLED/DATABASE_URL not set; "
              "skipping heartbeat check")
        return []

    sql = f"""
        WITH latest_heartbeat AS (
            SELECT cron_app, cron_function, run_id,
                   MAX(heartbeat_at) AS last_heartbeat_at,
                   MAX(stage) AS stage
            FROM ops.cron_heartbeats
            WHERE heartbeat_at > NOW() - INTERVAL '24 hours'
            GROUP BY cron_app, cron_function, run_id
        )
        SELECT
            lh.cron_app,
            lh.cron_function,
            lh.run_id::text,
            lh.last_heartbeat_at,
            EXTRACT(EPOCH FROM (NOW() - lh.last_heartbeat_at)) AS seconds_since,
            lh.stage
        FROM latest_heartbeat lh
        JOIN bulk_ingest.feed_ingest_runs r
            ON r.run_id::text = lh.run_id::text
        WHERE r.status = 'running'
          AND lh.last_heartbeat_at < NOW()
              - (INTERVAL '1 second' * {HEARTBEAT_STALENESS_THRESHOLD_SECONDS})
        ORDER BY lh.last_heartbeat_at ASC
        LIMIT 50
    """

    out: list[dict[str, Any]] = []
    try:
        with psycopg.connect(db_url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                for row in cur.fetchall():
                    out.append({
                        "cron_app": row[0],
                        "cron_function": row[1],
                        "run_id": row[2],
                        "last_heartbeat_at": str(row[3]),
                        "seconds_since": float(row[4]),
                        "stage": row[5],
                    })
    except Exception as exc:  # noqa: BLE001
        print(f"[alerter_cron] heartbeat staleness query failed: {type(exc).__name__}: {exc}")
    return out
