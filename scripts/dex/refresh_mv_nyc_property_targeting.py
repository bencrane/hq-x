#!/usr/bin/env python3
"""Refresh entities.mv_nyc_property_targeting via ops.refresh_mv_nyc_property_targeting().

Usage:
    doppler run -- python3 scripts/refresh_mv_nyc_property_targeting.py
    doppler run -- python3 scripts/refresh_mv_nyc_property_targeting.py --non-concurrent

REFRESH MATERIALIZED VIEW CONCURRENTLY does NOT work via the pgbouncer
transaction-mode pooler (DEX_DB_URL_POOLED). This script explicitly uses
DEX_DB_URL_DIRECT so the refresh runs against the raw connection.

Why a Python script and not a Trigger.dev task: per CLAUDE.md, the
Trigger.dev → DEX M2M auth boundary is currently broken (Phase 4 removed
require_m2m). This script avoids the boundary entirely by talking to
Postgres directly. Schedule via cron (or Railway scheduler) when you want
recurring runs.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

import psycopg

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("refresh-nyc-mv")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--non-concurrent",
        action="store_true",
        help="Run as REFRESH MATERIALIZED VIEW (non-concurrent). Default is concurrent.",
    )
    args = parser.parse_args()

    db_url = os.environ.get("DEX_DB_URL_DIRECT")
    if not db_url:
        log.error("DEX_DB_URL_DIRECT not set — wrap call with `doppler run --`.")
        return 2

    started = time.time()
    log.info(
        "calling ops.refresh_mv_nyc_property_targeting(p_concurrent=%s)",
        not args.non_concurrent,
    )

    # autocommit=True is required for REFRESH MATERIALIZED VIEW CONCURRENTLY,
    # which cannot run inside a transaction block. The function itself uses
    # PL/pgSQL EXCEPTION blocks for log-row updates.
    with psycopg.connect(db_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Belt + suspenders: extend statement_timeout for the session
            # in case Postgres role default is shorter than the MV refresh.
            cur.execute("SET statement_timeout = '60min'")
            cur.execute(
                "SELECT ops.refresh_mv_nyc_property_targeting(%s)",
                (not args.non_concurrent,),
            )
            row = cur.fetchone()
            result = row[0] if row else {}

    elapsed = time.time() - started
    log.info(
        "refresh complete: %s (wall=%.1fs)",
        json.dumps(result),
        elapsed,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
