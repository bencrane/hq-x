#!/usr/bin/env python3
"""Apply source_fmcsa_carrier_inspection_state_footprint_derived +
mv_fmcsa_carrier_inspection_state_footprint to RisingWave.

Pattern B layer: catalogs the derived Parquet (produced by
build_fmcsa_carrier_inspection_state_footprint.py) as a RW source, then
creates a pure pass-through MV. Both objects are idempotent on
pg_class / rw_catalog.rw_sources.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-inspection-state-footprint.md.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_inspection_state_footprint_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_inspection_state_footprint_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_inspection_state_footprint_rw.py --verify-only

Lifted from apply_fmcsa_carrier_officers_normalized_rw.py (PR #314) and
apply_fmcsa_carrier_emails_attributed_rw.py (PR #317).
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
logger = logging.getLogger("apply_fmcsa_carrier_inspection_state_footprint_rw")

SOURCE_NAME = "source_fmcsa_carrier_inspection_state_footprint_derived"
MV_NAME = "mv_fmcsa_carrier_inspection_state_footprint"
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30
SMOKE_TOLERANCE_FRAC = 0.005   # ±0.5% — pure derivation, very tight target
EXPECTED_TOTAL = 1_646_556
EXPECTED_DISTINCT_DOTS = 659_431
EXPECTED_DISTINCT_STATES = 57

R2_MATCH_PATTERN = "fmcsa-derived/carrier_inspection_state_footprint/snapshot=*/data.parquet"


def _source_ddl() -> str:
    return f"""
CREATE SOURCE public.{SOURCE_NAME} (
    "dot_number"                     CHARACTER VARYING,
    "report_state"                   CHARACTER VARYING,
    "inspection_count"               BIGINT,
    "first_inspection_date"          CHARACTER VARYING,
    "last_inspection_date"           CHARACTER VARYING,
    "inspection_count_last_365d"     BIGINT,
    "viol_total_sum"                 BIGINT,
    "oos_total_sum"                  BIGINT,
    "driver_viol_sum"                BIGINT,
    "vehicle_viol_sum"               BIGINT,
    "hazmat_viol_sum"                BIGINT,
    "snapshot_date"                  CHARACTER VARYING
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
    # v1 MV is a pure pass-through. Same deviation as PR #314 / #317:
    # `WHERE snapshot_date = (SELECT max(snapshot_date) FROM same_source)`
    # triggers DIRTY_STREAM_JOB_CLEAR in RW 2.8.x. Pure pass-through is
    # correct for v1 (one snapshot per --apply); downstream audience MVs
    # do static-date filtering per L37.
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


def _smoke_gates(*, use_source_as_proxy: bool = False) -> dict:
    """Run smoke gates. If use_source_as_proxy=True, query the source directly
    (BACKGROUND_DDL backfill not yet complete) — same shape as predecessor."""
    target = SOURCE_NAME if use_source_as_proxy else MV_NAME

    expected_total = EXPECTED_TOTAL
    tolerance = max(int(expected_total * SMOKE_TOLERANCE_FRAC), 100)
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

    # Gate 3: total row count within ±0.5%
    total = int(_rw_psql(
        f"SELECT count(*) FROM public.{target};", fetch=True,
    ).strip())
    gate3 = lower <= total <= upper

    # Gate 4: distinct dot_numbers matches expected (exact, ±0.5%)
    distinct_dots = int(_rw_psql(
        f"SELECT count(DISTINCT dot_number) FROM public.{target};", fetch=True,
    ).strip())
    dots_tolerance = max(int(EXPECTED_DISTINCT_DOTS * SMOKE_TOLERANCE_FRAC), 50)
    gate4 = abs(distinct_dots - EXPECTED_DISTINCT_DOTS) <= dots_tolerance

    # Gate 5: distinct report_state count exact (deterministic)
    distinct_states = int(_rw_psql(
        f"SELECT count(DISTINCT report_state) FROM public.{target};", fetch=True,
    ).strip())
    gate5 = distinct_states == EXPECTED_DISTINCT_STATES

    # Gate 6: uniqueness on (dot_number, report_state) — exact (per-row grain)
    out = _rw_psql(
        f"SELECT count(*), count(DISTINCT (dot_number, report_state)) "
        f"FROM public.{target};",
        fetch=True,
    ).strip()
    total2_str, distinct_str = [p.strip() for p in out.split("|")]
    total2, distinct = int(total2_str), int(distinct_str)
    gate6 = total2 == distinct

    # Gate 7: conservation — SUM(inspection_count) over MV equals upstream
    # vehicle_inspection_essentials row count (8,188,614 per --apply ledger).
    conserved_sum = int(_rw_psql(
        f"SELECT SUM(inspection_count) FROM public.{target};", fetch=True,
    ).strip())
    gate7 = conserved_sum == 8_188_614

    return {
        "gate1_source": gate1,
        "gate2_mv": gate2,
        "gate3_count": gate3,
        "gate4_distinct_dots": gate4,
        "gate5_distinct_states": gate5,
        "gate6_unique": gate6,
        "gate7_conservation": gate7,
        "actual_total": total,
        "actual_distinct_dots": distinct_dots,
        "actual_distinct_states": distinct_states,
        "actual_distinct_pairs": distinct,
        "actual_conserved_sum": conserved_sum,
        "expected_total": expected_total,
        "expected_distinct_dots": EXPECTED_DISTINCT_DOTS,
        "expected_distinct_states": EXPECTED_DISTINCT_STATES,
        "expected_conserved_sum": 8_188_614,
        "smoke_lower": lower,
        "smoke_upper": upper,
        "query_target": target,
    }


def _print_smoke_summary(r: dict) -> None:
    logger.info("=" * 70)
    logger.info(f"Smoke gates (query target: {r['query_target']}):")
    logger.info(
        "  gate1 (source exists):                   %s",
        "PASS" if r["gate1_source"] else "FAIL",
    )
    logger.info(
        "  gate2 (MV exists):                       %s",
        "PASS" if r["gate2_mv"] else "FAIL",
    )
    logger.info(
        "  gate3 (count within ±0.5%%):              %s  total=%s expected=%s [%s..%s]",
        "PASS" if r["gate3_count"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['expected_total']:,}",
        f"{r['smoke_lower']:,}", f"{r['smoke_upper']:,}",
    )
    logger.info(
        "  gate4 (distinct dots within ±0.5%%):      %s  dots=%s expected=%s",
        "PASS" if r["gate4_distinct_dots"] else "FAIL",
        f"{r['actual_distinct_dots']:,}", f"{r['expected_distinct_dots']:,}",
    )
    logger.info(
        "  gate5 (distinct states exact):           %s  states=%s expected=%s",
        "PASS" if r["gate5_distinct_states"] else "FAIL",
        r["actual_distinct_states"], r["expected_distinct_states"],
    )
    logger.info(
        "  gate6 (uniqueness on dot+state):         %s  total=%s distinct=%s",
        "PASS" if r["gate6_unique"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['actual_distinct_pairs']:,}",
    )
    logger.info(
        "  gate7 (conservation: SUM=upstream):      %s  sum=%s expected=%s",
        "PASS" if r["gate7_conservation"] else "FAIL",
        f"{r['actual_conserved_sum']:,}", f"{r['expected_conserved_sum']:,}",
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
        # MV may be in BACKGROUND_DDL backfill; smoke against source as proxy
        # if MV not yet in pg_class.
        mv_present = MV_NAME in _rw_psql(
            f"SELECT relname FROM pg_class WHERE relname = '{MV_NAME}';",
            fetch=True,
        ).strip()
        results = _smoke_gates(use_source_as_proxy=not mv_present)
        _print_smoke_summary(results)
        # gate2 may be PENDING during BACKGROUND_DDL admit — don't fail verify
        # if everything else passes.
        required_gates = ("gate1_source", "gate3_count", "gate4_distinct_dots",
                          "gate5_distinct_states", "gate6_unique", "gate7_conservation")
        all_pass = all(results[k] for k in required_gates)
        if not all_pass:
            sys.exit(1)
        logger.info("verify-only: ALL REQUIRED GATES PASSED")
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
        logger.warning(
            "HYDRATION TIMEOUT for %s after %ss — falling back to source-as-proxy smoke",
            MV_NAME, PER_MV_WAIT_TIMEOUT_S,
        )
        results = _smoke_gates(use_source_as_proxy=True)
        _print_smoke_summary(results)
        required_gates = ("gate1_source", "gate3_count", "gate4_distinct_dots",
                          "gate5_distinct_states", "gate6_unique", "gate7_conservation")
        all_pass = all(results[k] for k in required_gates)
        if not all_pass:
            sys.exit(2)
        logger.info("SOURCE-AS-PROXY GATES PASSED (MV still backfilling)")
        return

    logger.info("hydration complete; running smoke gates against MV")
    results = _smoke_gates(use_source_as_proxy=False)
    _print_smoke_summary(results)
    all_pass = all(
        results[k] for k in
        ("gate1_source", "gate2_mv", "gate3_count", "gate4_distinct_dots",
         "gate5_distinct_states", "gate6_unique", "gate7_conservation")
    )
    if not all_pass:
        logger.error("SMOKE GATES FAILED — see summary above")
        sys.exit(1)
    logger.info("ALL GATES PASSED. %s ready.", MV_NAME)


if __name__ == "__main__":
    main()
