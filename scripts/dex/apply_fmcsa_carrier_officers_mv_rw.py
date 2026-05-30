#!/usr/bin/env python3
"""Apply mv_fmcsa_carrier_officers — officer-grain projection over the carrier-grain
mv_fmcsa_carrier_essentials MV.

Pure RW DDL. UNION ALL slot-fanout: each row in mv_fmcsa_carrier_essentials with
a populated company_officer_1 yields one (dot_number, officer_slot=1, officer_name_raw)
row; same for company_officer_2 → slot=2. Carrier context (legal_name, dba_name,
contact, address, fleet, status, safety) passes through verbatim so downstream
bridges and audience MVs can filter without re-joining to essentials.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-officers-mv.md.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_mv_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_mv_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_mv_rw.py --verify-only

Lifted from apply_connector_model_v1_audience_mvs_rw.py (PR #289) — same shape,
single MV, no YAML config (SQL is hardcoded here since this is a foundation
projection, not a configurable audience cohort).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("apply_fmcsa_carrier_officers_mv_rw")

MV_NAME = "mv_fmcsa_carrier_officers"
UPSTREAM_MV = "mv_fmcsa_carrier_essentials"
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30
UPSTREAM_MIN_ROWS = 4_000_000
SMOKE_TOLERANCE_FRAC = 0.05  # row count within ±5% of pre-flight s1+s2

# Slot-fanout SELECT. Each branch carries the carrier-context pass-through.
# 28 carrier-context cols + dot_number + officer_slot + officer_name_raw = 31 cols.
# All cols are VARCHAR pass-through except is_free_mail_domain (BOOLEAN, native
# type from the upstream MV). Per L29: no casting at producer; consumers cast.
_OFFICER_SELECT = """
SELECT
  dot_number,
  1::SMALLINT             AS officer_slot,
  company_officer_1       AS officer_name_raw,
  legal_name,
  dba_name,
  email_address,
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
  email_domain_normalized,
  is_free_mail_domain,
  safety_rating,
  hm_ind,
  bus_units,
  dun_bradstreet_no
FROM public.mv_fmcsa_carrier_essentials
WHERE company_officer_1 IS NOT NULL AND TRIM(company_officer_1) <> ''
UNION ALL
SELECT
  dot_number,
  2::SMALLINT             AS officer_slot,
  company_officer_2       AS officer_name_raw,
  legal_name,
  dba_name,
  email_address,
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
  email_domain_normalized,
  is_free_mail_domain,
  safety_rating,
  hm_ind,
  bus_units,
  dun_bradstreet_no
FROM public.mv_fmcsa_carrier_essentials
WHERE company_officer_2 IS NOT NULL AND TRIM(company_officer_2) <> ''
""".strip()


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
            cmd, env=env, input=sql_script, capture_output=True, text=True,
            check=False, timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "RW psql timed out after %ss — DDL likely admitted (verify via rw_ddl_progress)",
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
    """Return (upstream_total, s1_populated, s2_populated)."""
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{UPSTREAM_MV}';",
        fetch=True,
    ).strip()
    if UPSTREAM_MV not in out:
        raise SystemExit(
            f"FAIL: upstream MV {UPSTREAM_MV} not found in pg_class. "
            "Ingest pipeline broken upstream of this directive."
        )
    logger.info("preflight: %s exists", UPSTREAM_MV)

    counts_sql = (
        "SELECT count(*), "
        "count(*) FILTER (WHERE company_officer_1 IS NOT NULL "
        "                 AND TRIM(company_officer_1) <> ''), "
        "count(*) FILTER (WHERE company_officer_2 IS NOT NULL "
        "                 AND TRIM(company_officer_2) <> '') "
        f"FROM public.{UPSTREAM_MV};"
    )
    out = _rw_psql(counts_sql, fetch=True).strip()
    total_str, s1_str, s2_str = [p.strip() for p in out.split("|")]
    total, s1, s2 = int(total_str), int(s1_str), int(s2_str)

    if total < UPSTREAM_MIN_ROWS:
        raise SystemExit(
            f"FAIL: upstream {UPSTREAM_MV} has only {total:,} rows; "
            f"expected ≥ {UPSTREAM_MIN_ROWS:,}. Re-hydration in progress?"
        )
    if s1 == 0 or s2 == 0:
        raise SystemExit(
            f"FAIL: upstream officer slots empty (s1={s1}, s2={s2}). "
            "Officer columns are not populated; cannot fanout."
        )
    logger.info(
        "preflight: upstream=%s  s1=%s  s2=%s  expected=%s",
        f"{total:,}", f"{s1:,}", f"{s2:,}", f"{s1+s2:,}",
    )
    return total, s1, s2


def _ddl_for(*, drop_first: bool = False) -> str:
    parts = ["SET BACKGROUND_DDL = TRUE;"]
    if drop_first:
        parts.append(f"DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};")
    parts.append(
        f"CREATE MATERIALIZED VIEW public.{MV_NAME} AS\n{_OFFICER_SELECT};"
    )
    return "\n".join(parts) + "\n"


def _mv_already_present() -> bool:
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{MV_NAME}';",
        fetch=True,
    ).strip()
    if MV_NAME in out:
        return True
    out = _rw_psql(
        "SELECT ddl_statement FROM rw_catalog.rw_ddl_progress;",
        fetch=True,
    ).strip()
    return f"public.{MV_NAME}" in out


def _wait_for_hydration(*, timeout_s: int) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        out = _rw_psql(
            f"SELECT ddl_statement FROM rw_catalog.rw_ddl_progress "
            f"WHERE ddl_statement ILIKE '%{MV_NAME}%';",
            fetch=True,
        ).strip()
        if not out:
            return "hydrated"
        logger.info("waiting on %s — still in rw_ddl_progress", MV_NAME)
        time.sleep(POLL_INTERVAL_S)
    return "timeout"


def _smoke_gates(*, expected_s1: int, expected_s2: int) -> dict:
    """Run all 5 smoke gates from the directive. Returns dict with results."""
    expected_total = expected_s1 + expected_s2
    tolerance = int(expected_total * SMOKE_TOLERANCE_FRAC)
    lower = expected_total - tolerance
    upper = expected_total + tolerance

    # Gate 1: pg_class existence (already verified by caller; cheap re-confirm)
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{MV_NAME}';",
        fetch=True,
    ).strip()
    gate1 = MV_NAME in out

    # Gate 2: total row count within ±5%
    total = int(_rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME};", fetch=True,
    ).strip())
    gate2 = lower <= total <= upper

    # Gate 3: slot distribution exact match
    out = _rw_psql(
        f"SELECT count(*) FILTER (WHERE officer_slot = 1), "
        f"count(*) FILTER (WHERE officer_slot = 2) "
        f"FROM public.{MV_NAME};",
        fetch=True,
    ).strip()
    s1_str, s2_str = [p.strip() for p in out.split("|")]
    actual_s1, actual_s2 = int(s1_str), int(s2_str)
    gate3 = (actual_s1 == expected_s1) and (actual_s2 == expected_s2)

    # Gate 4: zero NULL/empty officer names
    nulls = int(_rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME} "
        f"WHERE officer_name_raw IS NULL OR TRIM(officer_name_raw) = '';",
        fetch=True,
    ).strip())
    gate4 = nulls == 0

    # Gate 5: uniqueness within (dot_number, officer_slot)
    out = _rw_psql(
        f"SELECT count(*), count(DISTINCT (dot_number, officer_slot)) "
        f"FROM public.{MV_NAME};",
        fetch=True,
    ).strip()
    total2_str, distinct_str = [p.strip() for p in out.split("|")]
    total2, distinct = int(total2_str), int(distinct_str)
    gate5 = total2 == distinct

    return {
        "gate1_exists": gate1,
        "gate2_count": gate2,
        "gate3_slots": gate3,
        "gate4_no_nulls": gate4,
        "gate5_unique": gate5,
        "actual_total": total,
        "actual_s1": actual_s1,
        "actual_s2": actual_s2,
        "actual_nulls": nulls,
        "actual_distinct_dot_slot": distinct,
        "expected_total": expected_total,
        "expected_s1": expected_s1,
        "expected_s2": expected_s2,
        "smoke_lower": lower,
        "smoke_upper": upper,
    }


def _print_smoke_summary(r: dict) -> None:
    logger.info("=" * 70)
    logger.info("Smoke gates:")
    logger.info(
        "  gate1 (pg_class exists):                 %s",
        "PASS" if r["gate1_exists"] else "FAIL",
    )
    logger.info(
        "  gate2 (count within ±5%%):                %s  total=%s expected=%s [%s..%s]",
        "PASS" if r["gate2_count"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['expected_total']:,}",
        f"{r['smoke_lower']:,}", f"{r['smoke_upper']:,}",
    )
    logger.info(
        "  gate3 (slot distribution exact):         %s  s1=%s/%s s2=%s/%s",
        "PASS" if r["gate3_slots"] else "FAIL",
        f"{r['actual_s1']:,}", f"{r['expected_s1']:,}",
        f"{r['actual_s2']:,}", f"{r['expected_s2']:,}",
    )
    logger.info(
        "  gate4 (no NULL/empty officer names):     %s  nulls=%s",
        "PASS" if r["gate4_no_nulls"] else "FAIL", r["actual_nulls"],
    )
    logger.info(
        "  gate5 (no dupes on dot+slot):            %s  total=%s distinct=%s",
        "PASS" if r["gate5_unique"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['actual_distinct_dot_slot']:,}",
    )
    logger.info("=" * 70)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-wait", action="store_true")
    args = p.parse_args()
    if not (args.dry_run or args.apply or args.verify_only):
        p.error("specify --dry-run, --apply, or --verify-only")

    if args.dry_run:
        print(f"-- ====== {MV_NAME} ======")
        print(_ddl_for())
        return

    total, s1, s2 = _preflight()

    if args.verify_only:
        if not _mv_already_present():
            raise SystemExit(
                f"FAIL: --verify-only but {MV_NAME} not present in pg_class."
            )
        results = _smoke_gates(expected_s1=s1, expected_s2=s2)
        _print_smoke_summary(results)
        all_pass = all(
            results[k] for k in
            ("gate1_exists", "gate2_count", "gate3_slots", "gate4_no_nulls", "gate5_unique")
        )
        if not all_pass:
            sys.exit(1)
        logger.info("verify-only: ALL GATES PASSED")
        return

    # --apply path
    if _mv_already_present():
        logger.info("already present — skipping DDL: %s", MV_NAME)
    else:
        logger.info("applying DDL for %s", MV_NAME)
        ddl = _ddl_for(drop_first=False)
        _rw_psql_script(ddl, timeout_s=120)
        logger.info("DDL admitted; BACKGROUND_DDL hydration started")

    if args.skip_wait:
        logger.info("--skip-wait: returning before hydration / smoke")
        return

    logger.info("waiting for %s to hydrate (timeout %ss)", MV_NAME, PER_MV_WAIT_TIMEOUT_S)
    wait_status = _wait_for_hydration(timeout_s=PER_MV_WAIT_TIMEOUT_S)
    if wait_status == "timeout":
        logger.error(
            "HYDRATION TIMEOUT for %s after %ss — surface to operator",
            MV_NAME, PER_MV_WAIT_TIMEOUT_S,
        )
        sys.exit(2)

    logger.info("hydration complete; running smoke gates")
    results = _smoke_gates(expected_s1=s1, expected_s2=s2)
    _print_smoke_summary(results)
    all_pass = all(
        results[k] for k in
        ("gate1_exists", "gate2_count", "gate3_slots", "gate4_no_nulls", "gate5_unique")
    )
    if not all_pass:
        logger.error("SMOKE GATES FAILED — see summary above")
        sys.exit(1)
    logger.info("ALL GATES PASSED. %s ready.", MV_NAME)


if __name__ == "__main__":
    main()
