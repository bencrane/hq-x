#!/usr/bin/env python3
"""Register a Lance dataset in ops.data_sources + ops.data_source_slas.

Wave 1 sweep helper. Parameterized generalization of
``seed_lance_canary_observability_source.py`` (the canary's hardcoded seed
for fmcsa_carrier_essentials_lance). New sources call this script with
CLI args; the canary's seed stays in place for back-compat.

Usage:
    doppler run --project hq-all --config prd -- python3 \\
        apps/data-engine-x/scripts/seed_lance_observability_source.py \\
        --display-name fmcsa_crash_essentials_lance \\
        --storage-uri s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/crash_essentials_lance \\
        --sla-freshness-seconds 86400 \\
        --notes "Wave 1 Lance sweep — daily refresh via Modal cron at 06:45 UTC"

Required env (from Doppler):
    DEX_DB_URL_DIRECT
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
LOG = logging.getLogger("seed-lance-observability-source")

FORMAT = "lance"
OWNER_APP = "data-engine-x"
# ops.data_sources.status enum allows {active, needs_triage, retired}.
# Wave 1 sweep canaries register as 'active' (the constraint blocks
# 'canary'); canary semantics live in the SLA notes.
STATUS = "active"
SLA_BASIS = "last_ingested"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--display-name", required=True,
        help="e.g. fmcsa_crash_essentials_lance",
    )
    ap.add_argument(
        "--storage-uri", required=True,
        help="s3://dex-raw-landing-zone/polaris-warehouse/<namespace>/<table>_lance",
    )
    ap.add_argument(
        "--sla-freshness-seconds", type=int, required=True,
        help="e.g. 86400 (24h) for daily refresh; 2700000 (~31d) for monthly",
    )
    ap.add_argument(
        "--notes", required=True,
        help="SLA notes; document the cron, source path, refresh cadence",
    )
    args = ap.parse_args()

    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not db_url:
        LOG.error("required env DEX_DB_URL_DIRECT not set")
        return 64

    LOG.info("registering %s in ops.data_sources", args.display_name)
    LOG.info("  storage_uri: %s", args.storage_uri)
    LOG.info("  format:      %s", FORMAT)
    LOG.info("  status:      %s", STATUS)
    LOG.info("  sla:         %ds", args.sla_freshness_seconds)

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
            (args.display_name, args.storage_uri, FORMAT, OWNER_APP, STATUS),
        ).fetchone()

        if result is None:
            row = conn.execute(
                "SELECT source_id FROM ops.data_sources WHERE display_name = %s",
                (args.display_name,),
            ).fetchone()
            assert row is not None, f"upsert failed for {args.display_name}"
            source_id = row[0]
            LOG.info("data_sources: no-op (already current)")
        else:
            source_id, was_inserted = result
            LOG.info("data_sources: %s", "INSERTED" if was_inserted else "UPDATED")

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
            (source_id, args.sla_freshness_seconds, SLA_BASIS, args.notes),
        ).fetchone()

        if sla_result is None:
            LOG.info("data_source_slas: no-op (already current)")
        else:
            LOG.info("data_source_slas: %s", "INSERTED" if sla_result[0] else "UPDATED")

    LOG.info("OK: %s registered (source_id=%s)", args.display_name, source_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
