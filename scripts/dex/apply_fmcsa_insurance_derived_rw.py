#!/usr/bin/env python3
"""Apply the 4 FMCSA insurance + carrier derived-Parquet RW sources.

Wires the 4 derivations produced by:
  - build_fmcsa_actpendinsur_essentials.py
  - build_fmcsa_inshist_essentials.py
  - build_fmcsa_rejected_essentials.py
  - build_fmcsa_carrier_registrations_essentials.py

Source DDL is hand-written here (lift from
`risingwave/source_wiring_fmcsa_insurance_derived.sql`). Per the predecessor
PR #293 pattern (`source_fmcsa_*_derived`), each source is wired explicitly
— RW 2.8.x rejects `(*)` for plain Parquet without a registry. Idempotent:
skips sources already in `pg_class`. Creates sources only — does NOT create
any MVs.

Usage:
  doppler run -p hq-all -c prd -- \\
      uv run --with psycopg[binary] python \\
      apps/data-engine-x/scripts/apply_fmcsa_insurance_derived_rw.py --apply

  doppler run -p hq-all -c prd -- \\
      uv run --with psycopg[binary] python \\
      apps/data-engine-x/scripts/apply_fmcsa_insurance_derived_rw.py --dry-run

See ~/Desktop/hq/directives/2026-05-10-fmcsa-insurance-feeds-derived-parquet-unblock.md.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_fmcsa_insurance_derived_rw")


# ──────────────────────────────────────────────────────────────────────────
# 4 source DDLs.
# ──────────────────────────────────────────────────────────────────────────

# Each entry: (name, columns_decl_block, match_pattern). columns_decl_block
# is the exact text inside the parens (column list, comma-separated, with
# trailing snapshot DATE for Hive partitioning). All cols VARCHAR per
# directive's L2 contract.

SOURCES: list[tuple[str, str, str]] = [
    (
        "source_fmcsa_actpendinsur_derived",
        """
            dot_number                  VARCHAR,
            docket_number               VARCHAR,
            form_code                   VARCHAR,
            insurance_type_description  VARCHAR,
            insurance_company_name      VARCHAR,
            policy_number               VARCHAR,
            posted_date                 VARCHAR,
            bipd_underlying_limit       VARCHAR,
            bipd_maximum_limit          VARCHAR,
            effective_date              VARCHAR,
            cancel_effective_date       VARCHAR,
            snapshot                    DATE
        """,
        "fmcsa-derived/actpendinsur_essentials/snapshot=*/data.parquet",
    ),
    (
        "source_fmcsa_inshist_derived",
        """
            dot_number                                VARCHAR,
            docket_number                             VARCHAR,
            form_code                                 VARCHAR,
            cancellation_method                       VARCHAR,
            cancel_replace_name_change_transfer_form  VARCHAR,
            insurance_type_indicator                  VARCHAR,
            insurance_type_description                VARCHAR,
            policy_number                             VARCHAR,
            minimum_coverage_amount                   VARCHAR,
            insurance_class_code                      VARCHAR,
            effective_date                            VARCHAR,
            bipd_underlying_limit_amount              VARCHAR,
            bipd_max_coverage_amount                  VARCHAR,
            cancel_effective_date                     VARCHAR,
            specific_cancellation_method              VARCHAR,
            insurance_company_branch                  VARCHAR,
            insurance_company_name                    VARCHAR,
            snapshot                                  DATE
        """,
        "fmcsa-derived/inshist_essentials/snapshot=*/data.parquet",
    ),
    (
        "source_fmcsa_rejected_derived",
        """
            dot_number                       VARCHAR,
            docket_number                    VARCHAR,
            form_code_insurance_or_cancel    VARCHAR,
            insurance_type_description       VARCHAR,
            policy_number                    VARCHAR,
            received_date                    VARCHAR,
            insurance_class_code             VARCHAR,
            insurance_type_code              VARCHAR,
            underlying_limit_amount          VARCHAR,
            maximum_coverage_amount          VARCHAR,
            rejected_date                    VARCHAR,
            insurance_branch                 VARCHAR,
            company_name                     VARCHAR,
            rejected_reason                  VARCHAR,
            minimum_coverage_amount          VARCHAR,
            snapshot                         DATE
        """,
        "fmcsa-derived/rejected_essentials/snapshot=*/data.parquet",
    ),
    (
        "source_fmcsa_carrier_registrations_derived",
        """
            dot_number                                VARCHAR,
            docket_number                             VARCHAR,
            mx_type                                   VARCHAR,
            rfc_number                                VARCHAR,
            common_authority                          VARCHAR,
            contract_authority                        VARCHAR,
            broker_authority                          VARCHAR,
            pending_common_authority                  VARCHAR,
            pending_contract_authority                VARCHAR,
            pending_broker_authority                  VARCHAR,
            common_authority_revocation               VARCHAR,
            contract_authority_revocation             VARCHAR,
            broker_authority_revocation               VARCHAR,
            property                                  VARCHAR,
            passenger                                 VARCHAR,
            household_goods                           VARCHAR,
            private_check                             VARCHAR,
            enterprise_check                          VARCHAR,
            bipd_required                             VARCHAR,
            cargo_required                            VARCHAR,
            bond_surety_required                      VARCHAR,
            bipd_on_file                              VARCHAR,
            cargo_on_file                             VARCHAR,
            bond_surety_on_file                       VARCHAR,
            address_status                            VARCHAR,
            dba_name                                  VARCHAR,
            legal_name                                VARCHAR,
            business_address_po_box_street            VARCHAR,
            business_address_colonia                  VARCHAR,
            business_address_city                     VARCHAR,
            business_address_state_code               VARCHAR,
            business_address_country_code             VARCHAR,
            business_address_zip_code                 VARCHAR,
            business_address_telephone_number         VARCHAR,
            business_address_fax_number               VARCHAR,
            mailing_address_po_box_street             VARCHAR,
            mailing_address_colonia                   VARCHAR,
            mailing_address_city                      VARCHAR,
            mailing_address_state_code                VARCHAR,
            mailing_address_country_code              VARCHAR,
            mailing_address_zip_code                  VARCHAR,
            mailing_address_telephone_number          VARCHAR,
            mailing_address_fax_number                VARCHAR,
            snapshot                                  DATE
        """,
        "fmcsa-derived/carrier_registrations_essentials/snapshot=*/data.parquet",
    ),
]


def _rw_psql(sql: str, *, fetch: bool = False) -> str:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "-v", "ON_ERROR_STOP=1",
    ]
    if fetch:
        cmd += ["-tAc", sql]
    else:
        cmd += ["-c", sql]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _build_ddl(name: str, columns_block: str, match_pattern: str) -> str:
    return (
        f"CREATE SOURCE public.{name} (\n"
        f"{columns_block.rstrip()}\n"
        f") WITH (\n"
        f"    connector = 's3',\n"
        f"    s3.bucket_name = 'dex-raw-landing-zone',\n"
        f"    s3.region_name = 'us-east-1',\n"
        f"    s3.endpoint_url = '{os.environ['R2_ENDPOINT']}',\n"
        f"    s3.credentials.access = '{os.environ['R2_ACCESS_KEY_ID']}',\n"
        f"    s3.credentials.secret = '{os.environ['R2_SECRET_ACCESS_KEY']}',\n"
        f"    match_pattern = '{match_pattern}'\n"
        f") FORMAT PLAIN ENCODE PARQUET;"
    )


def _source_exists(name: str) -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{name}';",
        fetch=True,
    ).strip()
    return name in out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--smoke-only",
        action="store_true",
        help="For each source, run SELECT count(*); print results.",
    )
    args = p.parse_args()
    if not (args.apply or args.dry_run or args.smoke_only):
        p.error("specify --apply, --dry-run, or --smoke-only")

    for var in (
        "R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        "RISINGWAVE_HOST", "RISINGWAVE_PORT", "RISINGWAVE_USER",
        "RISINGWAVE_PASSWORD", "RISINGWAVE_DATABASE",
    ):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if args.smoke_only:
        for name, _, _ in SOURCES:
            if not _source_exists(name):
                logger.warning("source not present: %s", name)
                continue
            try:
                cnt = _rw_psql(
                    f"SELECT count(*) FROM public.{name};", fetch=True
                ).strip()
                logger.info("smoke %s: %s rows", name, cnt)
            except SystemExit as exc:
                logger.error("smoke %s: ERROR %s", name, str(exc)[:200])
        return 0

    if args.dry_run:
        for name, columns_block, match_pattern in SOURCES:
            print(f"-- ====== {name} ======")
            print(_build_ddl(name, columns_block, match_pattern))
            print()
        return 0

    # --apply
    for name, columns_block, match_pattern in SOURCES:
        if _source_exists(name):
            logger.info("already exists — skipping: %s", name)
            continue
        ddl = _build_ddl(name, columns_block, match_pattern)
        logger.info("creating source: %s", name)
        _rw_psql(ddl)
        if not _source_exists(name):
            raise SystemExit(
                f"FAIL: source {name} not visible in pg_class after CREATE"
            )
        logger.info("OK — %s admitted", name)

    logger.info("done — %d sources processed", len(SOURCES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
