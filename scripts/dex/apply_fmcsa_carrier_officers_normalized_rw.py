#!/usr/bin/env python3
"""Apply source_fmcsa_officer_normalized_derived + mv_fmcsa_carrier_officers_normalized.

Pattern B layer: catalogs the derived Parquet (produced by
build_fmcsa_carrier_officers_normalized.py) as a RW source, then creates a
latest-snapshot pass-through MV. Both objects are idempotent on
pg_class / rw_catalog.rw_sources.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-officers-normalized.md.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_normalized_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_normalized_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_officers_normalized_rw.py --verify-only

Lifted from apply_fmcsa_carrier_officers_mv_rw.py (Phase 2.5 sibling).
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
logger = logging.getLogger("apply_fmcsa_carrier_officers_normalized_rw")

SOURCE_NAME = "source_fmcsa_officer_normalized_derived"
MV_NAME = "mv_fmcsa_carrier_officers_normalized"
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30
SMOKE_TOLERANCE_FRAC = 0.05
EMPTY_RATE_GATE = 0.05
EXPECTED_S1 = 3_794_602
EXPECTED_S2 = 736_836
EXPECTED_TOTAL = EXPECTED_S1 + EXPECTED_S2

R2_MATCH_PATTERN = "fmcsa-derived/officer_normalized/snapshot=*/data.parquet"


def _source_ddl() -> str:
    return f"""
CREATE SOURCE public.{SOURCE_NAME} (
    "dot_number"                 CHARACTER VARYING,
    "officer_slot"               SMALLINT,
    "officer_name_raw"           CHARACTER VARYING,
    "officer_name_normalized"    CHARACTER VARYING,
    "officer_first_normalized"   CHARACTER VARYING,
    "officer_last_normalized"    CHARACTER VARYING,
    "legal_name"                 CHARACTER VARYING,
    "dba_name"                   CHARACTER VARYING,
    "email_address"              CHARACTER VARYING,
    "email_domain_normalized"    CHARACTER VARYING,
    "is_free_mail_domain"        CHARACTER VARYING,
    "phone"                      CHARACTER VARYING,
    "cell_phone"                 CHARACTER VARYING,
    "phy_street"                 CHARACTER VARYING,
    "phy_city"                   CHARACTER VARYING,
    "phy_state"                  CHARACTER VARYING,
    "phy_zip"                    CHARACTER VARYING,
    "phy_country"                CHARACTER VARYING,
    "mailing_street"             CHARACTER VARYING,
    "mailing_city"               CHARACTER VARYING,
    "mailing_state"              CHARACTER VARYING,
    "mailing_zip"                CHARACTER VARYING,
    "power_units"                CHARACTER VARYING,
    "fleetsize"                  CHARACTER VARYING,
    "total_drivers"              CHARACTER VARYING,
    "status_code"                CHARACTER VARYING,
    "mcs150_date"                CHARACTER VARYING,
    "add_date"                   CHARACTER VARYING,
    "business_org_desc"          CHARACTER VARYING,
    "carrier_operation"          CHARACTER VARYING,
    "safety_rating"              CHARACTER VARYING,
    "hm_ind"                     CHARACTER VARYING,
    "bus_units"                  CHARACTER VARYING,
    "dun_bradstreet_no"          CHARACTER VARYING,
    "snapshot_date"              CHARACTER VARYING
) WITH (
    connector = 's3',
    s3.bucket_name = 'dex-raw-landing-zone',
    s3.region_name = 'us-east-1',
    s3.endpoint_url = '{os.environ["R2_ENDPOINT"]}',
    s3.credentials.access = '{os.environ["R2_ACCESS_KEY_ID"]}',
    s3.credentials.secret = '{os.environ["R2_SECRET_ACCESS_KEY"]}',
    match_pattern = '{R2_MATCH_PATTERN}'
) FORMAT PLAIN ENCODE PARQUET;
""".strip()


def _mv_ddl() -> str:
    # v1 MV is a pure pass-through over the S3_V2 source. The original
    # `WHERE snapshot_date = (SELECT max(snapshot_date) FROM same_source)`
    # design caused "clear during recovery" failure (RW dropped the streaming
    # job after BACKGROUND_DDL admit) because correlated self-referential
    # aggregating subqueries against an append-only S3 source aren't
    # supported in RW 2.8.x streaming MVs. For v1 the build script is
    # operator-fired (one snapshot per --apply), so the source contains a
    # single snapshot and pass-through is correct. When daily-cron lands
    # (separate directive), latest-snapshot filtering moves to downstream
    # audience MVs (which can use static-date literal filters per L37) or
    # to operator-managed deletion of old snapshot keys from R2 before
    # adding new ones.
    return f"""
SET BACKGROUND_DDL = TRUE;
CREATE MATERIALIZED VIEW public.{MV_NAME} AS
SELECT *
  FROM public.{SOURCE_NAME};
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


def _source_already_present() -> bool:
    out = _rw_psql(
        f"SELECT name FROM rw_catalog.rw_sources WHERE name = '{SOURCE_NAME}';",
        fetch=True,
    ).strip()
    return SOURCE_NAME in out


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


def _smoke_gates() -> dict:
    """Run all 7 smoke gates per directive §Verification harness."""
    expected_total = EXPECTED_TOTAL
    tolerance = int(expected_total * SMOKE_TOLERANCE_FRAC)
    lower = expected_total - tolerance
    upper = expected_total + tolerance

    # Gate 1: source exists in rw_catalog
    out = _rw_psql(
        f"SELECT name FROM rw_catalog.rw_sources WHERE name = '{SOURCE_NAME}';",
        fetch=True,
    ).strip()
    gate1 = SOURCE_NAME in out

    # Gate 2: MV exists in pg_class
    out = _rw_psql(
        f"SELECT relname FROM pg_class WHERE relname = '{MV_NAME}';",
        fetch=True,
    ).strip()
    gate2 = MV_NAME in out

    # Gate 3: total row count within ±5%
    total = int(_rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME};", fetch=True,
    ).strip())
    gate3 = lower <= total <= upper

    # Gate 4: slot distribution exact match
    out = _rw_psql(
        f"SELECT count(*) FILTER (WHERE officer_slot = 1), "
        f"count(*) FILTER (WHERE officer_slot = 2) "
        f"FROM public.{MV_NAME};",
        fetch=True,
    ).strip()
    s1_str, s2_str = [p.strip() for p in out.split("|")]
    actual_s1, actual_s2 = int(s1_str), int(s2_str)
    gate4 = (actual_s1 == EXPECTED_S1) and (actual_s2 == EXPECTED_S2)

    # Gate 5: empty first_normalized rate <5%
    empty_first = int(_rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME} "
        f"WHERE officer_first_normalized IS NULL OR officer_first_normalized = '';",
        fetch=True,
    ).strip())
    pct_first = empty_first / max(total, 1)
    gate5 = pct_first < EMPTY_RATE_GATE

    # Gate 6: empty last_normalized rate <5%
    empty_last = int(_rw_psql(
        f"SELECT count(*) FROM public.{MV_NAME} "
        f"WHERE officer_last_normalized IS NULL OR officer_last_normalized = '';",
        fetch=True,
    ).strip())
    pct_last = empty_last / max(total, 1)
    gate6 = pct_last < EMPTY_RATE_GATE

    # Gate 7: uniqueness within (dot_number, officer_slot) — pass-through MV
    # over a single snapshot should preserve sibling MV's invariant.
    out = _rw_psql(
        f"SELECT count(*), count(DISTINCT (dot_number, officer_slot)) "
        f"FROM public.{MV_NAME};",
        fetch=True,
    ).strip()
    total2_str, distinct_str = [p.strip() for p in out.split("|")]
    total2, distinct = int(total2_str), int(distinct_str)
    gate7 = total2 == distinct

    return {
        "gate1_source": gate1,
        "gate2_mv": gate2,
        "gate3_count": gate3,
        "gate4_slots": gate4,
        "gate5_empty_first": gate5,
        "gate6_empty_last": gate6,
        "gate7_unique": gate7,
        "actual_total": total,
        "actual_s1": actual_s1,
        "actual_s2": actual_s2,
        "actual_empty_first": empty_first,
        "actual_empty_first_pct": pct_first,
        "actual_empty_last": empty_last,
        "actual_empty_last_pct": pct_last,
        "actual_distinct_dot_slot": distinct,
        "expected_total": expected_total,
        "expected_s1": EXPECTED_S1,
        "expected_s2": EXPECTED_S2,
        "smoke_lower": lower,
        "smoke_upper": upper,
    }


def _print_smoke_summary(r: dict) -> None:
    logger.info("=" * 70)
    logger.info("Smoke gates:")
    logger.info(
        "  gate1 (source exists):                   %s",
        "PASS" if r["gate1_source"] else "FAIL",
    )
    logger.info(
        "  gate2 (MV exists):                       %s",
        "PASS" if r["gate2_mv"] else "FAIL",
    )
    logger.info(
        "  gate3 (count within ±5%%):                %s  total=%s expected=%s [%s..%s]",
        "PASS" if r["gate3_count"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['expected_total']:,}",
        f"{r['smoke_lower']:,}", f"{r['smoke_upper']:,}",
    )
    logger.info(
        "  gate4 (slot distribution exact):         %s  s1=%s/%s s2=%s/%s",
        "PASS" if r["gate4_slots"] else "FAIL",
        f"{r['actual_s1']:,}", f"{r['expected_s1']:,}",
        f"{r['actual_s2']:,}", f"{r['expected_s2']:,}",
    )
    logger.info(
        "  gate5 (empty first <5%%):                 %s  empty=%s (%s)",
        "PASS" if r["gate5_empty_first"] else "FAIL",
        f"{r['actual_empty_first']:,}", f"{r['actual_empty_first_pct']:.1%}",
    )
    logger.info(
        "  gate6 (empty last <5%%):                  %s  empty=%s (%s)",
        "PASS" if r["gate6_empty_last"] else "FAIL",
        f"{r['actual_empty_last']:,}", f"{r['actual_empty_last_pct']:.1%}",
    )
    logger.info(
        "  gate7 (no dupes on dot+slot):            %s  total=%s distinct=%s",
        "PASS" if r["gate7_unique"] else "FAIL",
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

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "RISINGWAVE_HOST", "RISINGWAVE_PORT", "RISINGWAVE_USER",
                "RISINGWAVE_PASSWORD", "RISINGWAVE_DATABASE"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if args.dry_run:
        print(f"-- ====== {SOURCE_NAME} ======")
        print(_source_ddl())
        print()
        print(f"-- ====== {MV_NAME} ======")
        print(_mv_ddl())
        return

    if args.verify_only:
        if not _source_already_present():
            raise SystemExit(
                f"FAIL: --verify-only but {SOURCE_NAME} not present in rw_catalog."
            )
        if not _mv_already_present():
            raise SystemExit(
                f"FAIL: --verify-only but {MV_NAME} not present in pg_class."
            )
        results = _smoke_gates()
        _print_smoke_summary(results)
        all_pass = all(
            results[k] for k in
            ("gate1_source", "gate2_mv", "gate3_count", "gate4_slots",
             "gate5_empty_first", "gate6_empty_last", "gate7_unique")
        )
        if not all_pass:
            sys.exit(1)
        logger.info("verify-only: ALL GATES PASSED")
        return

    # --apply path
    if _source_already_present():
        logger.info("source already present — skipping CREATE SOURCE: %s", SOURCE_NAME)
    else:
        logger.info("applying DDL for %s", SOURCE_NAME)
        _rw_psql_script(_source_ddl(), timeout_s=120)
        logger.info("source created")

    if _mv_already_present():
        logger.info("MV already present — skipping CREATE MV: %s", MV_NAME)
    else:
        logger.info("applying DDL for %s", MV_NAME)
        _rw_psql_script(_mv_ddl(), timeout_s=120)
        logger.info("MV admitted; BACKGROUND_DDL hydration started")

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
    results = _smoke_gates()
    _print_smoke_summary(results)
    all_pass = all(
        results[k] for k in
        ("gate1_source", "gate2_mv", "gate3_count", "gate4_slots",
         "gate5_empty_first", "gate6_empty_last", "gate7_unique")
    )
    if not all_pass:
        logger.error("SMOKE GATES FAILED — see summary above")
        sys.exit(1)
    logger.info("ALL GATES PASSED. %s ready.", MV_NAME)


if __name__ == "__main__":
    main()
