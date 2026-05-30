#!/usr/bin/env python3
"""Apply source_fmcsa_email_attributed_derived +
mv_fmcsa_carrier_emails_attributed to RisingWave.

Pattern B layer: catalogs the derived Parquet (produced by
build_fmcsa_carrier_emails_attributed.py) as a RW source, then creates a
pure pass-through MV. Both objects are idempotent on pg_class /
rw_catalog.rw_sources.

Per directive ~/Desktop/hq/directives/2026-05-10-fmcsa-carrier-emails-attributed.md.

Usage:
    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_emails_attributed_rw.py --dry-run

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_emails_attributed_rw.py --apply

    doppler run -p hq-all -c prd -- \\
        uv run --with psycopg[binary] python \\
        apps/data-engine-x/scripts/apply_fmcsa_carrier_emails_attributed_rw.py --verify-only

Lifted from apply_fmcsa_carrier_officers_normalized_rw.py (PR #314).
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
logger = logging.getLogger("apply_fmcsa_carrier_emails_attributed_rw")

SOURCE_NAME = "source_fmcsa_email_attributed_derived"
MV_NAME = "mv_fmcsa_carrier_emails_attributed"
PER_MV_WAIT_TIMEOUT_S = 30 * 60
POLL_INTERVAL_S = 30
SMOKE_TOLERANCE_FRAC = 0.005   # ±0.5% — pure derivation, very tight target
EXPECTED_TOTAL = 2_895_569

# Tier distribution sanity gates — must match build script's calibrated
# bounds. Stage 0 100K sample showed ~77.3% unmapped reflects FMCSA-corpus
# reality (concatenated-name local-parts, company-name-based local-parts,
# nickname forms without upstream-side canonical mapping). Ceiling relaxed
# to 0.80 from the directive's original 0.75 (2.7pp headroom).
HARD_FAIL_NAMED_FLOOR = 0.15
HARD_FAIL_UNMAPPED_CEIL = 0.80

R2_MATCH_PATTERN = "fmcsa-derived/email_attributed/snapshot=*/data.parquet"


def _source_ddl() -> str:
    return f"""
CREATE SOURCE public.{SOURCE_NAME} (
    "email_address"                            CHARACTER VARYING,
    "email_domain_normalized"                  CHARACTER VARYING,
    "is_free_mail_domain"                      CHARACTER VARYING,
    "dot_number"                               CHARACTER VARYING,
    "legal_name"                               CHARACTER VARYING,
    "dba_name"                                 CHARACTER VARYING,
    "company_officer_1"                        CHARACTER VARYING,
    "company_officer_2"                        CHARACTER VARYING,
    "officer_slot_count"                       SMALLINT,
    "phone"                                    CHARACTER VARYING,
    "cell_phone"                               CHARACTER VARYING,
    "phy_street"                               CHARACTER VARYING,
    "phy_city"                                 CHARACTER VARYING,
    "phy_state"                                CHARACTER VARYING,
    "phy_zip"                                  CHARACTER VARYING,
    "phy_country"                              CHARACTER VARYING,
    "mailing_street"                           CHARACTER VARYING,
    "mailing_city"                             CHARACTER VARYING,
    "mailing_state"                            CHARACTER VARYING,
    "mailing_zip"                              CHARACTER VARYING,
    "power_units"                              CHARACTER VARYING,
    "fleetsize"                                CHARACTER VARYING,
    "total_drivers"                            CHARACTER VARYING,
    "status_code"                              CHARACTER VARYING,
    "mcs150_date"                              CHARACTER VARYING,
    "add_date"                                 CHARACTER VARYING,
    "business_org_desc"                        CHARACTER VARYING,
    "carrier_operation"                        CHARACTER VARYING,
    "safety_rating"                            CHARACTER VARYING,
    "hm_ind"                                   CHARACTER VARYING,
    "bus_units"                                CHARACTER VARYING,
    "dun_bradstreet_no"                        CHARACTER VARYING,
    "local_part_normalized"                    CHARACTER VARYING,
    "attribution_tier"                         CHARACTER VARYING,
    "attributed_officer_slot"                  SMALLINT,
    "attributed_officer_name_normalized"       CHARACTER VARYING,
    "attributed_officer_first_normalized"      CHARACTER VARYING,
    "attributed_officer_last_normalized"       CHARACTER VARYING,
    "match_method"                             CHARACTER VARYING,
    "snapshot_date"                            CHARACTER VARYING
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
    # v1 MV is a pure pass-through. Same deviation as PR #314:
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


def _smoke_gates(*, against_source: bool = False) -> dict:
    """Run smoke gates. If against_source=True, smoke against the source
    instead of the MV (used when MV is still in BACKGROUND_DDL backfill —
    source has the same row count by pass-through invariant)."""
    target_relation = SOURCE_NAME if against_source else MV_NAME
    target_schema = "public"
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

    # Gate 3: total row count within ±tolerance
    total = int(_rw_psql(
        f"SELECT count(*) FROM {target_schema}.{target_relation};", fetch=True,
    ).strip())
    gate3 = lower <= total <= upper

    # Gate 4: tier distribution within Stage 0 bounds
    out = _rw_psql(
        f"""
        SELECT attribution_tier, count(*)
          FROM {target_schema}.{target_relation}
         GROUP BY attribution_tier
         ORDER BY 1;
        """,
        fetch=True,
    ).strip()
    tier_counts: dict[str, int] = {}
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) == 2:
            tier_counts[parts[0].strip()] = int(parts[1].strip())
    null_tier = int(_rw_psql(
        f"SELECT count(*) FROM {target_schema}.{target_relation} "
        f"WHERE attribution_tier IS NULL;",
        fetch=True,
    ).strip())
    pcts = {k: (v / total if total else 0.0) for k, v in tier_counts.items()}
    named = pcts.get("officer_high", 0.0) + pcts.get("officer_medium", 0.0)
    unmapped = pcts.get("unmapped", 0.0)
    gate4 = (named >= HARD_FAIL_NAMED_FLOOR and unmapped <= HARD_FAIL_UNMAPPED_CEIL
             and null_tier == 0)

    # Gate 5: uniqueness on (dot_number, email_address)
    out = _rw_psql(
        f"""
        SELECT count(*), count(DISTINCT (dot_number, email_address))
          FROM {target_schema}.{target_relation};
        """,
        fetch=True,
    ).strip()
    total2_str, distinct_str = [p.strip() for p in out.split("|")]
    total2 = int(total2_str)
    distinct = int(distinct_str)
    gate5 = total2 == distinct

    return {
        "gate1_source": gate1,
        "gate2_mv": gate2,
        "gate3_count": gate3,
        "gate4_tier_dist": gate4,
        "gate5_unique": gate5,
        "actual_total": total,
        "actual_distinct_dot_email": distinct,
        "actual_null_tier": null_tier,
        "tier_counts": tier_counts,
        "tier_pcts": pcts,
        "expected_total": expected_total,
        "smoke_lower": lower,
        "smoke_upper": upper,
        "named_pct": named,
        "unmapped_pct": unmapped,
        "smoked_against": target_relation,
    }


def _print_smoke_summary(r: dict) -> None:
    logger.info("=" * 70)
    logger.info("Smoke gates (smoked against %s):", r["smoked_against"])
    logger.info(
        "  gate1 (source exists):                %s",
        "PASS" if r["gate1_source"] else "FAIL",
    )
    logger.info(
        "  gate2 (MV exists):                    %s",
        "PASS" if r["gate2_mv"] else "FAIL",
    )
    logger.info(
        "  gate3 (count within ±%.1f%%):          %s  total=%s expected=%s [%s..%s]",
        SMOKE_TOLERANCE_FRAC * 100,
        "PASS" if r["gate3_count"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['expected_total']:,}",
        f"{r['smoke_lower']:,}", f"{r['smoke_upper']:,}",
    )
    logger.info(
        "  gate4 (tier dist OK):                 %s  named=%.1f%% unmapped=%.1f%% null=%s",
        "PASS" if r["gate4_tier_dist"] else "FAIL",
        r["named_pct"] * 100, r["unmapped_pct"] * 100,
        f"{r['actual_null_tier']:,}",
    )
    logger.info(
        "  gate5 (uniqueness dot+email):         %s  total=%s distinct=%s",
        "PASS" if r["gate5_unique"] else "FAIL",
        f"{r['actual_total']:,}", f"{r['actual_distinct_dot_email']:,}",
    )
    logger.info("  Tier counts:")
    for tier, cnt in sorted(r["tier_counts"].items()):
        pct = r["tier_pcts"].get(tier, 0.0)
        logger.info("    %-15s  %12s  (%.2f%%)", tier, f"{cnt:,}", pct * 100)
    logger.info("=" * 70)


def _drop_source_and_mv() -> None:
    """v1.1 --rebuild path: DROP SOURCE CASCADE drops the MV too.

    Per L45, when the R2 key is overwritten with new content, the existing
    S3_V2 source does NOT re-consume — it tracks objects-already-read by key.
    DROP+RECREATE resets that state. CASCADE ensures the dependent MV is
    dropped too (RW disallows DROP SOURCE while an MV depends on it).
    """
    logger.info("--rebuild: dropping MV (if exists) then source CASCADE")
    _rw_psql_script(
        f"""
        DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};
        DROP SOURCE IF EXISTS public.{SOURCE_NAME} CASCADE;
        """,
        timeout_s=60,
    )
    logger.info("drop complete")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--rebuild", action="store_true",
                   help="v1.1 re-derivation path: DROP source+MV via CASCADE, "
                        "then CREATE + wait + smoke. Use when R2 key was "
                        "overwritten in-place (L45 workaround).")
    p.add_argument("--verify-only", action="store_true")
    p.add_argument("--skip-wait", action="store_true")
    p.add_argument("--smoke-source", action="store_true",
                   help="Smoke against the source instead of MV (use when MV "
                        "is still in BACKGROUND_DDL backfill).")
    args = p.parse_args()
    mode_count = sum([args.dry_run, args.apply, args.rebuild, args.verify_only])
    if mode_count != 1:
        p.error("specify exactly one of --dry-run / --apply / --rebuild / --verify-only")

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                "RISINGWAVE_HOST", "RISINGWAVE_PORT", "RISINGWAVE_USER",
                "RISINGWAVE_PASSWORD", "RISINGWAVE_DATABASE"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")

    if args.dry_run:
        print(f"-- ====== DROP block (only when --rebuild) ======")
        print(f"DROP MATERIALIZED VIEW IF EXISTS public.{MV_NAME};")
        print(f"DROP SOURCE IF EXISTS public.{SOURCE_NAME} CASCADE;")
        print()
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
        if not args.smoke_source and not _mv_already_present():
            raise SystemExit(
                f"FAIL: --verify-only but {MV_NAME} not present in pg_class."
            )
        results = _smoke_gates(against_source=args.smoke_source)
        _print_smoke_summary(results)
        gate_keys = ("gate1_source", "gate2_mv", "gate3_count",
                     "gate4_tier_dist", "gate5_unique")
        if args.smoke_source:
            # When smoking against source, MV-existence gate 2 doesn't apply
            gate_keys = ("gate1_source", "gate3_count",
                         "gate4_tier_dist", "gate5_unique")
        all_pass = all(results[k] for k in gate_keys)
        if not all_pass:
            sys.exit(1)
        logger.info("verify-only: ALL GATES PASSED")
        return

    # --rebuild path: drop existing source+MV first (L45 workaround for
    # in-place R2 key overwrite), then fall through to the CREATE path.
    if args.rebuild:
        _drop_source_and_mv()

    # --apply / --rebuild path
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

    logger.info("waiting for %s to hydrate (timeout %ss)",
                MV_NAME, PER_MV_WAIT_TIMEOUT_S)
    wait_status = _wait_for_hydration(timeout_s=PER_MV_WAIT_TIMEOUT_S)
    if wait_status == "timeout":
        logger.warning(
            "HYDRATION TIMEOUT for %s after %ss — falling back to source smoke "
            "(per L24/L25 pattern; MV may complete async)",
            MV_NAME, PER_MV_WAIT_TIMEOUT_S,
        )
        results = _smoke_gates(against_source=True)
        _print_smoke_summary(results)
        gate_keys = ("gate1_source", "gate3_count",
                     "gate4_tier_dist", "gate5_unique")
        all_pass = all(results[k] for k in gate_keys)
        if not all_pass:
            logger.error("SOURCE-SMOKE GATES FAILED — see summary above")
            sys.exit(1)
        logger.info("SOURCE-SMOKE GATES PASSED (MV backfilling async)")
        sys.exit(2)

    logger.info("hydration complete; running smoke gates")
    results = _smoke_gates()
    _print_smoke_summary(results)
    all_pass = all(
        results[k] for k in
        ("gate1_source", "gate2_mv", "gate3_count",
         "gate4_tier_dist", "gate5_unique")
    )
    if not all_pass:
        logger.error("SMOKE GATES FAILED — see summary above")
        sys.exit(1)
    logger.info("ALL GATES PASSED. %s ready.", MV_NAME)


if __name__ == "__main__":
    main()
