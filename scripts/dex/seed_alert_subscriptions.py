#!/usr/bin/env python3
"""Phase 0c — seed ops.alert_subscriptions for every source.

For every row in ops.data_sources, ensure (one breach + one ingest_failed)
Telegram subscription exists for the operator's chat_id (read from
TELEGRAM_ALERT_CHAT_ID env var).

Idempotent: ON CONFLICT DO NOTHING per the UNIQUE (source_id, alert_kind,
channel, recipient) constraint.

Run:
    cd apps/data-engine-x
    doppler run --project hq-all --config prd -- \\
        python3 scripts/seed_alert_subscriptions.py
"""
from __future__ import annotations

import logging
import os
import sys

import psycopg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _conn() -> psycopg.Connection:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError("DEX_DB_URL_DIRECT must be set (Doppler hq-all/prd)")
    return psycopg.connect(url)


def main() -> int:
    chat_id = os.environ.get("TELEGRAM_ALERT_CHAT_ID")
    if not chat_id:
        log.error("TELEGRAM_ALERT_CHAT_ID not set in env")
        return 1

    log.info("Seeding alert subscriptions for chat_id=%s", chat_id)

    with _conn() as conn:
        with conn.cursor() as cur:
            # Insert breach + ingest_failed Telegram subscriptions for every
            # data source. ON CONFLICT DO NOTHING per UNIQUE constraint =
            # full idempotency on re-run.
            cur.execute(
                """
                INSERT INTO ops.alert_subscriptions
                    (source_id, alert_kind, channel, recipient)
                SELECT ds.source_id, kind::alert_kind, 'telegram'::alert_channel, %s
                  FROM ops.data_sources ds
                  CROSS JOIN (VALUES ('breach'), ('ingest_failed')) k(kind)
                ON CONFLICT (source_id, alert_kind, channel, recipient) DO NOTHING
                """,
                (chat_id,),
            )
            inserted = cur.rowcount
            conn.commit()

            cur.execute("SELECT count(*) FROM ops.alert_subscriptions")
            row = cur.fetchone()
            total = row[0] if row else 0

            cur.execute("SELECT count(*) FROM ops.data_sources")
            row = cur.fetchone()
            sources = row[0] if row else 0

    log.info("Inserted %d new subscriptions; total=%d, sources=%d (expected total >= 2*sources = %d)",
             inserted, total, sources, 2 * sources)

    if total < 2 * sources:
        log.warning("subscriptions count (%d) < 2*sources (%d) — some sources missing subscriptions",
                    total, 2 * sources)

    return 0


if __name__ == "__main__":
    sys.exit(main())
