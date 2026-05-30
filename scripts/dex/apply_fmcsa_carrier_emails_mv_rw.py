#!/usr/bin/env python3
"""Apply mv_fmcsa_carrier_emails (outreach-keyed surface) to RisingWave.

Stage 5+ projection MV: filters mv_fmcsa_carrier_essentials → emailable
rows. Pure SQL filter + reshape (no UNION, no joins). One row per
(dot_number, email_address) pair.

Surfaces:
  email_address, email_domain_normalized, is_free_mail_domain (leading dims),
  carrier identity (dot_number, legal_name, dba_name),
  BOTH officer-name slots + officer_slot_count,
  outreach context (phone/address),
  business signals (power_units, fleetsize, status_code, etc).

Idempotent: re-running --apply skips CREATE if MV exists, runs smoke only.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_emails_mv_rw.py --apply

    --dry-run       # emit DDL to stdout, no apply
    --verify-only   # smoke an existing MV (post-hydration verification)

Directive: ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-emails-mv.md
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_fmcsa_carrier_emails_mv_rw")

REPO_ROOT = Path(__file__).resolve().parents[3]
SQL_FILE = REPO_ROOT / "apps/data-engine-x/risingwave/fmcsa_carrier_emails.sql"

MV_NAME = "mv_fmcsa_carrier_emails"
UPSTREAM_MV = "mv_fmcsa_carrier_essentials"
UPSTREAM_MIN_ROWS = 4_000_000
HYDRATION_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30
ROW_COUNT_TOLERANCE = 0.02  # ±2%

DDL_TEMPLATE = """\
SET BACKGROUND_DDL = TRUE;

CREATE MATERIALIZED VIEW public.mv_fmcsa_carrier_emails AS
SELECT
  email_address,
  email_domain_normalized,
  is_free_mail_domain,
  dot_number,
  legal_name,
  dba_name,
  company_officer_1,
  company_officer_2,
  (
    CASE WHEN company_officer_1 IS NOT NULL AND TRIM(company_officer_1) <> '' THEN 1 ELSE 0 END
    +
    CASE WHEN company_officer_2 IS NOT NULL AND TRIM(company_officer_2) <> '' THEN 1 ELSE 0 END
  )::SMALLINT AS officer_slot_count,
  phone,
  cell_phone,
  phy_street,
  phy_city,
  phy_state,
  phy_zip,
  phy_country,
  carrier_mailing_street  AS mailing_street,
  carrier_mailing_city    AS mailing_city,
  carrier_mailing_state   AS mailing_state,
  carrier_mailing_zip     AS mailing_zip,
  power_units,
  fleetsize,
  total_drivers,
  status_code,
  mcs150_date,
  add_date,
  business_org_desc,
  carrier_operation,
  safety_rating,
  hm_ind,
  bus_units,
  dun_bradstreet_no
FROM public.mv_fmcsa_carrier_essentials
WHERE email_address IS NOT NULL
  AND TRIM(email_address) <> ''
  AND email_address LIKE '%@%';
"""


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


def _rw_psql_script(sql_script: str, *, timeout_s: int | None = None) -> str:
    cmd = [
        "psql",
        "-h", os.environ["RISINGWAVE_HOST"],
        "-p", os.environ["RISINGWAVE_PORT"],
        "-U", os.environ["RISINGWAVE_USER"],
        "-d", os.environ["RISINGWAVE_DATABASE"],
        "--no-psqlrc",
        "-v", "ON_ERROR_STOP=1",
    ]
    env = {**os.environ, "PGPASSWORD": os.environ["RISINGWAVE_PASSWORD"]}
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            input=sql_script,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "RW psql timed out after %ss — DDL likely admitted (verify via SHOW JOBS)",
            timeout_s,
        )
        return "<psql_timeout>"
    if proc.returncode != 0:
        raise SystemExit(
            f"RW psql script failed (exit {proc.returncode}):\n"
            f"  STDERR:\n{proc.stderr}\n"
            f"  STDOUT:\n{proc.stdout}"
        )
    return proc.stdout


def _preflight() -> tuple[int, int, int]:
    """Verify upstream MV is healthy and capture email-population stats.

    Returns (email_populated, email_with_at, free_mail_count).
    email_with_at is the smoke target.
    """
    logger.info("preflight: checking upstream %s", UPSTREAM_MV)
    out = _rw_psql(
        f"SELECT count(*) FROM public.{UPSTREAM_MV};",
        fetch=True,
    ).strip()
    upstream_total = int(out)
    if upstream_total < UPSTREAM_MIN_ROWS:
        raise SystemExit(
            f"FAIL: {UPSTREAM_MV} has only {upstream_total:,} rows; "
            f"expected ≥ {UPSTREAM_MIN_ROWS:,}. Upstream may be re-hydrating."
        )
    logger.info("preflight: %s = %s rows", UPSTREAM_MV, f"{upstream_total:,}")

    out = _rw_psql(
        f"""
        SELECT
          count(*) FILTER (WHERE email_address IS NOT NULL AND TRIM(email_address) <> '')
            AS email_populated,
          count(*) FILTER (WHERE email_address LIKE '%@%')
            AS email_with_at,
          count(*) FILTER (WHERE is_free_mail_domain = TRUE)
            AS free_mail_count
        FROM public.{UPSTREAM_MV};
        """,
        fetch=True,
    ).strip()
    parts = out.split("|")
    email_populated = int(parts[0])
    email_with_at = int(parts[1])
    free_mail_count = int(parts[2])
    logger.info(
        "preflight: email_populated=%s email_with_at=%s free_mail_count=%s",
        f"{email_populated:,}", f"{email_with_at:,}", f"{free_mail_count:,}",
    )
    if email_with_at == 0:
        raise SystemExit(
            f"FAIL: {UPSTREAM_MV}.email_with_at = 0; cannot proceed."
        )
    return (email_populated, email_with_at, free_mail_count)


def _mv_exists() -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{MV_NAME}';",
        fetch=True,
    ).strip()
    return MV_NAME in out


def _mv_in_progress() -> bool:
    out = _rw_psql(
        "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress;",
        fetch=True,
    ).strip()
    return f"public.{MV_NAME}" in out or MV_NAME in out


def _wait_for_hydration(timeout_s: int) -> str:
    """Poll rw_ddl_progress until MV no longer in progress, or timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            f"SELECT progress FROM rw_catalog.rw_ddl_progress "
            f"WHERE ddl_statement ILIKE '%{MV_NAME}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — progress: %s", MV_NAME, out)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


def _smoke(email_with_at_target: int) -> dict:
    """Run smoke gates. Return dict of results; raises on hard failure."""
    results: dict[str, object] = {}

    # Gate 1: pg_class existence.
    if not _mv_exists():
        raise SystemExit(f"SMOKE FAIL: {MV_NAME} not in pg_class")
    results["pg_class_exists"] = True
    logger.info("smoke gate 1 PASS: pg_class shows %s exists", MV_NAME)

    # Gate 2: row count within ±tolerance of pre-flight target.
    out = _rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME};",
        fetch=True,
    ).strip()
    total = int(out)
    results["row_count"] = total
    delta = abs(total - email_with_at_target) / email_with_at_target
    if delta > ROW_COUNT_TOLERANCE:
        raise SystemExit(
            f"SMOKE FAIL: row count {total:,} deviates {delta*100:.2f}% from "
            f"target {email_with_at_target:,} (tolerance ±{ROW_COUNT_TOLERANCE*100:.0f}%)"
        )
    logger.info(
        "smoke gate 2 PASS: row count %s vs target %s (Δ %.3f%%)",
        f"{total:,}", f"{email_with_at_target:,}", delta * 100,
    )

    # Gate 3: filter enforcement — zero rows with NULL/empty/no-@ email.
    out = _rw_psql(
        f"""
        SELECT count(*) FROM public.{MV_NAME}
         WHERE email_address IS NULL
            OR TRIM(email_address) = ''
            OR email_address NOT LIKE '%@%';
        """,
        fetch=True,
    ).strip()
    invalid = int(out)
    results["invalid_email_rows"] = invalid
    if invalid != 0:
        raise SystemExit(
            f"SMOKE FAIL: {invalid} rows have NULL/empty/no-@ email — filter not enforced"
        )
    logger.info("smoke gate 3 PASS: zero invalid email rows")

    # Gate 4: uniqueness on (dot_number, email_address).
    out = _rw_psql(
        f"""
        SELECT count(*) - count(DISTINCT (dot_number, email_address))
          FROM public.{MV_NAME};
        """,
        fetch=True,
    ).strip()
    dupes = int(out)
    results["dupe_dot_email_pairs"] = dupes
    if dupes != 0:
        raise SystemExit(
            f"SMOKE FAIL: {dupes} duplicate (dot_number, email_address) pairs"
        )
    logger.info("smoke gate 4 PASS: zero duplicate (dot_number, email) pairs")

    # Gate 5: officer_slot_count distribution histogram.
    out = _rw_psql(
        f"""
        SELECT officer_slot_count, count(*)
          FROM public.{MV_NAME}
         GROUP BY officer_slot_count
         ORDER BY officer_slot_count;
        """,
        fetch=True,
    ).strip()
    histogram: dict[int, int] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        slot, cnt = line.split("|")
        histogram[int(slot)] = int(cnt)
    results["officer_slot_histogram"] = histogram
    logger.info(
        "smoke gate 5: officer_slot_count histogram = %s",
        {k: f"{v:,}" for k, v in sorted(histogram.items())},
    )

    return results


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true", help="CREATE MV + wait + smoke")
    p.add_argument("--dry-run", action="store_true", help="emit DDL to stdout")
    p.add_argument(
        "--verify-only",
        action="store_true",
        help="smoke an existing MV (skip CREATE)",
    )
    args = p.parse_args()

    if not (args.apply or args.dry_run or args.verify_only):
        p.error("must pass --apply, --dry-run, or --verify-only")
    if sum([args.apply, args.dry_run, args.verify_only]) > 1:
        p.error("--apply, --dry-run, --verify-only are mutually exclusive")

    if args.dry_run:
        print(DDL_TEMPLATE)
        logger.info("DRY RUN — no DDL applied")
        return 0

    required = (
        "RISINGWAVE_HOST",
        "RISINGWAVE_PORT",
        "RISINGWAVE_USER",
        "RISINGWAVE_PASSWORD",
        "RISINGWAVE_DATABASE",
    )
    for var in required:
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if not SQL_FILE.is_file():
        raise SystemExit(f"FAIL: SQL doc missing: {SQL_FILE}")

    t0 = time.time()
    email_populated, email_with_at, free_mail_count = _preflight()

    if args.verify_only:
        if not _mv_exists():
            raise SystemExit(
                f"FAIL: --verify-only requires {MV_NAME} to exist; not found in pg_class"
            )
        logger.info("verify-only: skipping CREATE, running smoke …")
        results = _smoke(email_with_at)
        logger.info("verify OK — duration=%.1fs results=%s", time.time() - t0, results)
        return 0

    # --apply path
    if _mv_exists():
        logger.info("idempotency: %s already in pg_class — skipping CREATE", MV_NAME)
    elif _mv_in_progress():
        logger.info(
            "idempotency: %s already in rw_ddl_progress — skipping CREATE, will wait",
            MV_NAME,
        )
    else:
        logger.info("applying DDL for %s …", MV_NAME)
        _rw_psql_script(DDL_TEMPLATE, timeout_s=120)
        logger.info("DDL admitted; entering hydration wait")

    # Wait for hydration (BACKGROUND_DDL is async).
    wait_status = _wait_for_hydration(HYDRATION_TIMEOUT_S)
    if wait_status == "timeout":
        raise SystemExit(
            f"HYDRATION TIMEOUT after {HYDRATION_TIMEOUT_S}s — surface to operator. "
            "Check `SHOW JOBS` and rw_ddl_progress for state."
        )
    logger.info("hydration complete")

    results = _smoke(email_with_at)
    logger.info(
        "OK — applied + smoked. duration=%.1fs results=%s",
        time.time() - t0, results,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
