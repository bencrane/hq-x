#!/usr/bin/env python3
"""Register fmcsa_carrier_essentials_embeddings_lance in ops.data_sources.

Phase 4 of the multi-phase hq-all rebuild — observability registration for
the FMCSA carrier embeddings Lance dataset. Mirrors the canary +
sweep-Wave-1 pattern: one row in ``ops.data_sources``, one row in
``ops.data_source_slas``, both idempotent.

The 24h SLA mirrors the source Lance dataset SLA — the embedding emit
cron at ``modal/fmcsa_carrier_essentials_embedding_emit_app.py`` runs at
07:45 UTC and writes a row to ``ops.data_source_ingest_runs`` per run.

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/seed_carrier_essentials_embeddings_observability_source.py
"""
from __future__ import annotations

import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

DISPLAY_NAME = "fmcsa_carrier_essentials_embeddings_lance"
STORAGE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/"
    "carrier_essentials_embeddings_lance"
)
FORMAT = "lance"
OWNER_APP = "data-engine-x"
STATUS = "active"
SLA_FRESHNESS_SECONDS = 86400  # 24h, mirrors the source Lance dataset
SLA_BASIS = "last_ingested"
SLA_NOTES = (
    "Phase 4 vector layer — daily embedding refresh of "
    "fmcsa.carrier_essentials_lance via "
    "modal/fmcsa_carrier_essentials_embedding_emit_app.py at 07:45 UTC. "
    "Only carriers whose profile_text content_hash changed get re-embedded, "
    "so daily marginal cost is tiny. See "
    "apps/data-engine-x/docs/embeddings-pipeline.md."
)


def _req(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        log.error("required env var %s not set", name)
        sys.exit(64)
    return val


def main() -> int:
    db_url = _req("DEX_DB_URL_DIRECT")

    log.info("registering %s in ops.data_sources", DISPLAY_NAME)
    log.info("  storage_uri: %s", STORAGE_URI)
    log.info("  format:      %s", FORMAT)
    log.info("  status:      %s", STATUS)
    log.info("  sla:         %ds (24h)", SLA_FRESHNESS_SECONDS)

    with psycopg.connect(db_url, autocommit=True) as conn:
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
            (DISPLAY_NAME, STORAGE_URI, FORMAT, OWNER_APP, STATUS),
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

    log.info("OK: %s registered (source_id=%s)", DISPLAY_NAME, source_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
