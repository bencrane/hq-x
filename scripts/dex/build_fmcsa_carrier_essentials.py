#!/usr/bin/env python3
"""Project FMCSA Company Census Parquet → derived 'essentials' Parquet on R2.

Reads the raw FMCSA Company Census Parquet (R2 path:
`fmcsa/Company Census File/<snapshot>/*.parquet.zst`) via DuckDB-on-R2, projects
the ~25 essentials columns + 2 derived (email_domain_normalized,
is_free_mail_domain), and writes a single uncompressed-format Parquet (internal
column ZSTD is fine) to:

    s3://dex-raw-landing-zone/fmcsa-carrier-essentials/snapshot=<YYYY-MM-DD>/data.parquet

Why a derived Parquet instead of an MV-over-raw-source: RisingWave's PARQUET
encoder cannot read whole-file `.parquet.zst` — it expects raw `.parquet` (with
internal column zstd OK, file-level zstd NOT OK). PR #249 wired
`source_fmcsa_company_census` over the .parquet.zst files; querying it returns
"invalid seek to a negative position". This script writes a sibling Parquet
that RW *can* read, and a thin RW MV (`mv_fmcsa_carrier_essentials`) is built
on top of a source over this output. See directive
~/Desktop/hq/directives/2026-05-08-fmcsa-pdl-domain-bridge.md.

Identity-key contract — every output row carries:
    dot_number (varchar), legal_name, dba_name, email_address, phy_state,
    status_code, fleetsize, power_units, total_drivers, … 25 source cols total
    + email_domain_normalized (varchar; lowercase, protocol/www/path-stripped)
    + is_free_mail_domain (boolean; from scripts._lib.free_mail_domains)

Rows are NOT filtered by email presence or domain type. Owner-operators with
gmail.com are kept — that's the outbound payload. is_free_mail_domain is a
downstream filter (see L14 in the directive), not a row-level exclusion.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb python \\
        apps/data-engine-x/scripts/build_fmcsa_carrier_essentials.py --apply

    # dry-run: row counts only, no R2 writes
    doppler run -p hq-all -c prd -- \\
        uv run --with duckdb python \\
        apps/data-engine-x/scripts/build_fmcsa_carrier_essentials.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Make scripts._lib importable when this script runs as __main__.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.free_mail_domains import (  # noqa: E402
    BLACKLIST_VERSION,
    FREE_MAIL_DOMAINS,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_fmcsa_carrier_essentials")

R2_BUCKET = "dex-raw-landing-zone"
INPUT_PREFIX = "fmcsa/Company Census File"
OUTPUT_PREFIX = "fmcsa-carrier-essentials"

ESSENTIALS_COLS = [
    # Identity (preserved as VARCHAR — leading zeros matter for DOT/DUNS)
    "DOT_NUMBER",
    "DUN_BRADSTREET_NO",
    # Names
    "LEGAL_NAME",
    "DBA_NAME",
    # Contact
    "EMAIL_ADDRESS",
    "PHONE",
    "CELL_PHONE",
    # Geographic — physical
    "PHY_STREET",
    "PHY_CITY",
    "PHY_STATE",
    "PHY_ZIP",
    "PHY_COUNTRY",
    # Geographic — mailing
    "CARRIER_MAILING_STREET",
    "CARRIER_MAILING_CITY",
    "CARRIER_MAILING_STATE",
    "CARRIER_MAILING_ZIP",
    # Org
    "BUSINESS_ORG_DESC",
    "COMPANY_OFFICER_1",
    "COMPANY_OFFICER_2",
    "MCS150_DATE",
    "ADD_DATE",
    # Operations
    "STATUS_CODE",
    "CARRIER_OPERATION",
    "FLEETSIZE",
    "POWER_UNITS",
    "TOTAL_DRIVERS",
    "MCS150_MILEAGE",
    "MCS150_MILEAGE_YEAR",
    # Specialty / safety (added 2026-05-10 to unblock 5 deferred FMCSA audience MVs)
    "HM_Ind",
    "CRGO_PASSENGERS",
    "CRGO_LIQGAS",
    "CRGO_COLDFOOD",
    "CRGO_MOTOVEH",
    "CRGO_DRIVETOW",
    "BUS_UNITS",
    "SAFETY_RATING",
]


def _r2_account_id_from_endpoint(endpoint: str) -> str:
    # R2_ENDPOINT looks like https://<account_id>.r2.cloudflarestorage.com
    return endpoint.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id_from_endpoint(os.environ["R2_ENDPOINT"])}'
        );
        """
    )
    return con


def _detect_latest_snapshot(con) -> str:
    rows = con.execute(
        f"SELECT file FROM glob('r2://{R2_BUCKET}/{INPUT_PREFIX}/**/*.parquet*')"
    ).fetchall()
    if not rows:
        raise SystemExit(
            f"FAIL: no FMCSA Census Parquet files found under "
            f"r2://{R2_BUCKET}/{INPUT_PREFIX}/"
        )
    snapshots: set[str] = set()
    for (path,) in rows:
        # path like r2://dex-raw-landing-zone/fmcsa/Company Census File/2026-05-08/<uuid>.parquet.zst
        parts = path.split("/")
        for p in parts:
            # YYYY-MM-DD shape
            if len(p) == 10 and p[4] == "-" and p[7] == "-":
                snapshots.add(p)
    if not snapshots:
        raise SystemExit("FAIL: no YYYY-MM-DD snapshot directory found in input paths")
    return max(snapshots)


def _build_select_sql(input_glob: str) -> str:
    quoted = ", ".join(f'"{c}" AS {c.lower()}' for c in ESSENTIALS_COLS)
    blacklist_literal = ", ".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))
    return f"""
        WITH base AS (
            SELECT
                {quoted},
                CASE
                    WHEN "EMAIL_ADDRESS" LIKE '%@%' THEN
                        regexp_replace(
                            regexp_replace(
                                regexp_replace(
                                    lower(trim(split_part("EMAIL_ADDRESS", '@', 2))),
                                    '^https?://', ''
                                ),
                                '^www\\.', ''
                            ),
                            '/.*$', ''
                        )
                    ELSE NULL
                END AS email_domain_normalized
            FROM read_parquet('{input_glob}')
        )
        SELECT
            *,
            CASE
                WHEN email_domain_normalized IN ({blacklist_literal}) THEN TRUE
                WHEN email_domain_normalized IS NULL THEN FALSE
                ELSE FALSE
            END AS is_free_mail_domain
        FROM base
    """


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Parquet to R2")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="row counts + sample only, no R2 writes",
    )
    parser.add_argument(
        "--snapshot",
        default=None,
        help="YYYY-MM-DD snapshot dir under input prefix (default: latest)",
    )
    args = parser.parse_args()

    if not args.apply and not args.dry_run:
        parser.error("must pass --apply or --dry-run")
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set in environment")

    t0 = time.time()
    con = _connect_duckdb_to_r2()

    snapshot = args.snapshot or _detect_latest_snapshot(con)
    logger.info(f"snapshot: {snapshot}")
    logger.info(f"blacklist version: {BLACKLIST_VERSION} ({len(FREE_MAIL_DOMAINS)} domains)")

    input_glob = f"r2://{R2_BUCKET}/{INPUT_PREFIX}/{snapshot}/*.parquet*"
    select_sql = _build_select_sql(input_glob)

    # Count rows + tier sanity (always — cheap, useful for both dry-run and apply).
    cnt_sql = f"SELECT count(*) FROM ({select_sql})"
    total = con.execute(cnt_sql).fetchone()[0]
    logger.info(f"essentials row count: {total:,}")

    domain_stats_sql = f"""
        SELECT
            count(*) FILTER (WHERE email_domain_normalized IS NOT NULL) AS rows_with_domain,
            count(*) FILTER (WHERE is_free_mail_domain) AS rows_free_mail,
            count(*) FILTER (WHERE email_domain_normalized IS NOT NULL AND NOT is_free_mail_domain) AS rows_branded_domain
        FROM ({select_sql})
    """
    rows_with_domain, rows_free_mail, rows_branded = con.execute(domain_stats_sql).fetchone()
    logger.info(f"  rows with email domain: {rows_with_domain:,}")
    logger.info(f"  rows w/ free-mail domain: {rows_free_mail:,}")
    logger.info(f"  rows w/ branded domain:   {rows_branded:,}")

    if args.dry_run:
        logger.info(f"DRY RUN — no R2 writes. duration={time.time()-t0:.1f}s")
        return 0

    # Apply: COPY ... TO 'r2://...' FORMAT PARQUET (no .zst on output filename)
    output_key = f"{OUTPUT_PREFIX}/snapshot={snapshot}/data.parquet"
    output_url = f"r2://{R2_BUCKET}/{output_key}"
    logger.info(f"writing → {output_url}")
    copy_sql = f"""
        COPY ({select_sql})
        TO '{output_url}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
    """
    con.execute(copy_sql)
    logger.info(f"OK — wrote {total:,} rows to {output_url}")
    logger.info(f"duration={time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
