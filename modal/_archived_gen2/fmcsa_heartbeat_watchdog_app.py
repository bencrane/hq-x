"""FMCSA heartbeat watchdog — Modal-native absence alarm.

Closes the never-started blind spot that left FMCSA ingest silently dark for
6 days (2026-05-23 → 05-29): the existing alerter (alerter_cron_app.py) only
flags rows already written as running/failed, so a heartbeat that never fires
writes nothing to bulk_ingest.feed_ingest_runs and goes completely unnoticed.

This watchdog asserts the FMCSA heartbeat actually RAN by checking
bulk_ingest.feed_schedule_config.last_heartbeat_evaluated_at — which
schedule_heartbeat stamps unconditionally every tick, BEFORE any probe
(fmcsa_ingest_app.py:_record_heartbeat_evaluated), so it advances even on a
zero-due-feed day. If it goes stale, the heartbeat (Modal cron OR the future
Trigger.dev schedule) is not firing → no feeds are being ingested → alert.

Modal-native ON PURPOSE: its entire job is to catch a dead heartbeat / dead
Trigger schedule, so it must NOT depend on Trigger or on the heartbeat itself.
It also alerts via DIRECT Telegram (TELEGRAM_BOT_TOKEN/CHAT_ID), not via the DEX
API, so it doesn't depend on DEX being up either — the alerter's run-cycle path
could not detect "absent" anyway (no ledger row exists to flag).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fmcsa_heartbeat_watchdog_app.py

Secrets:
    dex-db                   → DEX_DB_URL_POOLED (read the heartbeat stamp)
    fmcsa-watchdog-telegram  → TELEGRAM_BOT_TOKEN, TELEGRAM_ALERT_CHAT_ID
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

import modal

app = modal.App("data-engine-x-fmcsa-heartbeat-watchdog")
image = modal.Image.debian_slim(python_version="3.11").pip_install(
    "psycopg[binary]>=3.1",
    "httpx>=0.27",
)

SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("fmcsa-watchdog-telegram"),
]

# The heartbeat fires every 15 min. Alert after ~1.6 missed ticks so a single
# skipped tick (a deploy, a transient blip) does not page, but a real stall does.
STALE_THRESHOLD_MINUTES = 25
# At most one Telegram alert per hour while stale (dedup via a Modal Dict —
# Modal-native state, no extra DB schema).
ALERT_COOLDOWN_SECONDS = 3600

_dedup = modal.Dict.from_name("fmcsa-watchdog-dedup", create_if_missing=True)


def _db_url() -> str:
    url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")
    if not url:
        raise RuntimeError("DEX_DB_URL_POOLED / DEX_DB_URL_DIRECT not set")
    return url


def _send_telegram(text: str) -> None:
    import httpx

    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_ALERT_CHAT_ID"]
    resp = httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=30,
    )
    resp.raise_for_status()


@app.function(
    image=image,
    secrets=SECRETS,
    timeout=120,
    schedule=modal.Cron("*/10 * * * *"),
)
def check_heartbeat() -> dict:
    """Every 10 min: assert the FMCSA heartbeat stamp is fresh; alert if stale."""
    import psycopg

    with psycopg.connect(_db_url(), connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT max(last_heartbeat_evaluated_at)
                FROM bulk_ingest.feed_schedule_config
                WHERE source_id = 'fmcsa'
                """
            )
            row = cur.fetchone()

    last_eval = row[0] if row else None
    now = datetime.now(timezone.utc)
    age_min = None if last_eval is None else (now - last_eval).total_seconds() / 60.0

    if age_min is not None and age_min <= STALE_THRESHOLD_MINUTES:
        print(f"[watchdog] OK — fmcsa heartbeat age {age_min:.1f} min")
        return {"healthy": True, "age_min": age_min}

    # Stale or never-evaluated — alert, deduped to once per cooldown window.
    last_alert = float(_dedup.get("last_alert_epoch", 0.0))
    now_epoch = now.timestamp()
    should_alert = (now_epoch - last_alert) >= ALERT_COOLDOWN_SECONDS
    age_str = "never" if age_min is None else f"{age_min:.0f} min"
    msg = (
        "\U0001F6A8 FMCSA ingest heartbeat STALE\n"
        f"last_heartbeat_evaluated_at: {last_eval} ({age_str} ago)\n"
        f"threshold: {STALE_THRESHOLD_MINUTES} min\n"
        "The */15 heartbeat is not firing — no FMCSA feeds are being ingested. "
        "Check data-engine-x-fmcsa-ingest::schedule_heartbeat (Modal cron or the "
        "Trigger.dev schedule) and the dex-db secret."
    )
    if should_alert:
        try:
            _send_telegram(msg)
            _dedup["last_alert_epoch"] = now_epoch
            print(f"[watchdog] ALERTED — fmcsa heartbeat stale ({age_str})")
        except Exception as exc:  # noqa: BLE001
            print(f"[watchdog] alert send FAILED: {exc}")
    else:
        print(f"[watchdog] stale ({age_str}) but within cooldown — not re-alerting")
    return {"healthy": False, "age_min": age_min, "alerted": should_alert}
