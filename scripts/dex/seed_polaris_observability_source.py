#!/usr/bin/env python3
"""Register the Polaris catalog as a single row in ops.data_sources, with a
24h freshness SLA enforced via the daily Modal health-check cron
(modal/polaris_health_check_app.py).

Idempotent: re-runs are safe and produce no new rows when the entry is
unchanged (INSERT ... ON CONFLICT DO UPDATE WHERE IS DISTINCT FROM).

Usage:
  doppler run --project hq-all --config prd -- \\
    python3 apps/data-engine-x/scripts/seed_polaris_observability_source.py

Required env (from Doppler):
  DEX_DB_URL_DIRECT       — direct (non-pooled) Postgres connection to the
                            data-engine-x database that holds ops.data_sources
  POLARIS_PUBLIC_URL      — Polaris service URL (becomes storage_uri)

Run order: AFTER Phase 0a's ops.data_sources table exists in prod
(predecessor `2026-05-11-hq-all-phase-0a-observability-foundation`).
"""
from __future__ import annotations

import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DISPLAY_NAME = "polaris_catalog"
FORMAT = "polaris-rest"
OWNER_APP = "polaris"
STATUS = "active"
SLA_FRESHNESS_SECONDS = 86400  # 24h — enforced by polaris_health_check_app.py
SLA_BASIS = "last_ingested"
SLA_NOTES = (
    "Apache Polaris REST catalog freshness measured by the daily Modal "
    "health-check cron (apps/data-engine-x/modal/polaris_health_check_app.py). "
    "Each successful ping writes a row to ops.data_source_ingest_runs."
)


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("required env var %s not set", name)
        sys.exit(64)
    return val


def main() -> int:
    db_url = _req("DEX_DB_URL_DIRECT")
    polaris_url = _req("POLARIS_PUBLIC_URL").rstrip("/")

    log.info("registering polaris_catalog in ops.data_sources")
    log.info("  storage_uri: %s", polaris_url)
    log.info("  sla:         %ds (24h)", SLA_FRESHNESS_SECONDS)

    with psycopg.connect(db_url, autocommit=True) as conn:
        # Upsert ops.data_sources.
        result = conn.execute(
            """
            INSERT INTO ops.data_sources
                (display_name, storage_uri, format, owner_app, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (display_name) DO UPDATE
                SET storage_uri = EXCLUDED.storage_uri,
                    format      = EXCLUDED.format,
                    owner_app   = EXCLUDED.owner_app,
                    status      = EXCLUDED.status
                WHERE ops.data_sources.storage_uri IS DISTINCT FROM EXCLUDED.storage_uri
                   OR ops.data_sources.format      IS DISTINCT FROM EXCLUDED.format
                   OR ops.data_sources.owner_app   IS DISTINCT FROM EXCLUDED.owner_app
                   OR ops.data_sources.status      IS DISTINCT FROM EXCLUDED.status
            RETURNING source_id, (xmax = 0) AS was_inserted
            """,
            (DISPLAY_NAME, polaris_url, FORMAT, OWNER_APP, STATUS),
        ).fetchone()

        if result is None:
            row = conn.execute(
                "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
                (DISPLAY_NAME,),
            ).fetchone()
            assert row is not None, f"upsert failed for {DISPLAY_NAME}"
            source_id = row[0]
            log.info("data_sources: no-op (already current)")
        else:
            source_id, was_inserted = result
            log.info("data_sources: %s", "INSERTED" if was_inserted else "UPDATED")

        # Upsert ops.data_source_slas.
        sla_result = conn.execute(
            """
            INSERT INTO ops.data_source_slas
                (source_id, sla_freshness_seconds, sla_basis, notes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (source_id) DO UPDATE
                SET sla_freshness_seconds = EXCLUDED.sla_freshness_seconds,
                    sla_basis             = EXCLUDED.sla_basis,
                    notes                 = EXCLUDED.notes,
                    updated_at            = NOW()
                WHERE ops.data_source_slas.sla_freshness_seconds IS DISTINCT FROM EXCLUDED.sla_freshness_seconds
                   OR ops.data_source_slas.sla_basis             IS DISTINCT FROM EXCLUDED.sla_basis
                   OR ops.data_source_slas.notes                 IS DISTINCT FROM EXCLUDED.notes
            RETURNING (xmax = 0) AS was_inserted
            """,
            (source_id, SLA_FRESHNESS_SECONDS, SLA_BASIS, SLA_NOTES),
        ).fetchone()

        if sla_result is None:
            log.info("data_source_slas: no-op (already current)")
        else:
            log.info("data_source_slas: %s", "INSERTED" if sla_result[0] else "UPDATED")

    log.info("OK: polaris_catalog registered (source_id=%s)", source_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
