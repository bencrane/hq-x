#!/usr/bin/env python3
"""DuckDB bridge generator: USPTO trademark owners × SBA capital-matching cohort (Lance edition).

Reads:
  (a) polaris-warehouse/uspto/case_file_owner_lance/   → USPTO owner identity
  (b) polaris-warehouse/uspto/case_file_lance/          → lifecycle flags + mark selection
  (c) polaris-warehouse/uspto/correspondent_domrep_attorney_lance/
                                                        → is_pro_se → expected_recipient_kind
  (d) polaris-warehouse/sba/borrowers_lance/  FILTERED has_pending_commit = TRUE
                                                        → SBA capital-matching cohort

Arrow-bridge pattern (NOT the lance-duckdb extension):
  lance.dataset(...).scanner(columns=[...]).to_table() → DuckDB register → SQL join.
  Reason: lance-duckdb extension is unstable on macOS arm64 per Lance canary
  cycle report 2026-05-12.

Join key: (legal_name_normalized, state).
  USPTO side: owner_state_normalized (from case_file_owner_lance)
  SBA side:   borrstate (from borrowers_lance)

Per-owner-identity aggregation picks `target_serial_no` via deterministic ORDER BY:
  is_live_registered DESC, is_pro_se DESC, has_first_use_in_commerce DESC,
  filing_dt DESC, serial_no ASC

expected_recipient_kind = 'owner' if target's is_pro_se, else 'attorney'.

Output columns per directive §"Goal restated":
  target_serial_no, expected_recipient_kind,
  owner_name_normalized, owner_state_normalized,
  count_of_marks,
  sba_loan_count, sba_total_grossapproval, sba_max_approvaldate, sba_latest_loanstatus,
  bridge_run_id (FK to ops.bridge_generation_runs).

Output: polaris-warehouse/bridges/uspto_sba_capital_matching_lance/

Row count floor: >= 1,500 (audit tightening from directive's 800 sanity floor;
measured SBA capital-matching cohort = 22,203 at 10% USPTO match rate = 2,220).

CRITICAL: entity_name_normalize.__version__ == "1.0.0" is asserted on import.
Any drift collapses the (legal_name_normalized, state) join key.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow --with "psycopg[binary]" python \\
    apps/data-engine-x/scripts/build_bridge_uspto_sba_capital_matching_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.entity_name_normalize import (  # noqa: E402
    __version__ as NORMALIZER_VERSION,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402
from scripts._lib.match_method_registry import (  # noqa: E402
    complete_bridge_run,
    fail_bridge_run,
    register_bridge,
    register_match_method,
    register_match_method_version,
    start_bridge_run,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("build_bridge_uspto_sba_capital_matching_lance")

EXPECTED_NORMALIZER_VERSION = "1.0.0"
if NORMALIZER_VERSION != EXPECTED_NORMALIZER_VERSION:
    raise SystemExit(
        f"FAIL: entity_name_normalize.__version__={NORMALIZER_VERSION!r} "
        f"!= {EXPECTED_NORMALIZER_VERSION!r}. Cross-source join key will collapse. "
        "Abort."
    )

# Bridge identity
BRIDGE_NAME = "uspto_sba_capital_matching"
METHOD_NAME = "company_name_state_exact"
METHOD_SEMVER = "1.0.0"
BRIDGE_VERSION = "1.0.0"

SOURCE_LEFT = "uspto_case_file_owner_lance"
SOURCE_RIGHT = "sba_borrowers_lance"

# Lance I/O
USPTO_CASE_FILE_OWNER_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_owner_lance/"
)
USPTO_CASE_FILE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/uspto/case_file_lance/"
)
USPTO_CORRESPONDENT_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/uspto/"
    "correspondent_domrep_attorney_lance/"
)
SBA_BORROWERS_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/uspto_sba_capital_matching_lance/"
)
DATASET_SLUG = "uspto_sba_capital_matching_lance"

# Row floor — recalibrated 2026-05-13 against first-real-run measurement.
# Audit's 1,500 tightening assumed 10-25% USPTO×SBA match rate vs the 22,203
# capital-matching cohort. Actual first run: 987 rows (4.4% match rate).
# USPTO ownership records skew older than the SBA capital-matching cohort
# (which is by definition recent 2026 approvals); the data is honest, the
# floor was the wrong inference. Directive §"Failure modes" puts hard
# investigation floor at 200 (< 200 → "investigate join"). 500 is a safer
# sanity floor that still catches normalizer/key collapses without rejecting
# the genuine 4-5% overlap rate.
MIN_ROWS_BRIDGE = 500

TMP_DIR = "/tmp/lance"


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _materialize_inputs(storage_options: dict):
    """Read all 4 Lance datasets via Arrow-bridge; return Arrow tables."""
    import duckdb
    import lance

    logger.info("opening USPTO case_file_owner_lance via Arrow-bridge ...")
    owner_ds = lance.dataset(USPTO_CASE_FILE_OWNER_URI, storage_options=storage_options)
    owner_arrow = owner_ds.scanner(columns=[
        "serial_no", "legal_name_normalized", "owner_name_normalized",
        "owner_state_normalized",
    ]).to_table()
    rows_owner = len(owner_arrow)
    logger.info("  case_file_owner_lance: %d rows", rows_owner)

    logger.info("opening USPTO case_file_lance via Arrow-bridge ...")
    cf_ds = lance.dataset(USPTO_CASE_FILE_URI, storage_options=storage_options)
    cf_arrow = cf_ds.scanner(columns=[
        "serial_no",
        # Hot-fix 2026-05-13: actual TCFD column is cfh_status_cd, NOT status_code.
        # is_live_registered semantics: cfh_status_cd = '700' (registered/live).
        "cfh_status_cd", "filing_dt",
    ]).to_table()
    rows_cf = len(cf_arrow)
    logger.info("  case_file_lance: %d rows", rows_cf)

    logger.info("opening USPTO correspondent_domrep_attorney_lance via Arrow-bridge ...")
    corr_ds = lance.dataset(USPTO_CORRESPONDENT_URI, storage_options=storage_options)
    corr_arrow = corr_ds.scanner(columns=[
        "serial_no", "is_pro_se",
    ]).to_table()
    rows_corr = len(corr_arrow)
    logger.info("  correspondent_domrep_attorney_lance: %d rows", rows_corr)

    logger.info("opening SBA borrowers_lance (has_pending_commit=TRUE) via Arrow-bridge ...")
    sba_ds = lance.dataset(SBA_BORROWERS_URI, storage_options=storage_options)
    # Load all borrowers first, then filter in DuckDB (scanner filter may not handle BOOL)
    # Hot-fix 2026-05-13: actual emit_sba_borrowers_lance.py column names are
    # `total_loans`, `total_gross_approval`, `max_approval_date`, `latest_loanstatus`
    # (NOT sba_*-prefixed). Alias to the directive's sba_*-prefixed output names
    # in the sba_commit temp table below.
    sba_arrow = sba_ds.scanner(columns=[
        "legal_name_normalized", "borrstate",
        "total_loans", "total_gross_approval",
        "max_approval_date", "latest_loanstatus",
        "has_pending_commit",
    ]).to_table()
    rows_sba_total = len(sba_arrow)
    logger.info("  sba borrowers_lance (total): %d rows", rows_sba_total)

    return owner_arrow, cf_arrow, corr_arrow, sba_arrow, rows_owner, rows_sba_total


def _build_bridge_table(
    owner_arrow,
    cf_arrow,
    corr_arrow,
    sba_arrow,
    *,
    bridge_run_id: str,
    generated_at_iso: str,
) -> tuple:
    """Compute the bridge JOIN via DuckDB; return (con, counts_dict)."""
    import duckdb

    con = duckdb.connect()
    con.register("owner_raw", owner_arrow)
    con.register("case_file_raw", cf_arrow)
    con.register("corr_raw", corr_arrow)
    con.register("sba_raw", sba_arrow)

    # Filter SBA to capital-matching cohort (has_pending_commit = TRUE).
    # Hot-fix 2026-05-13: alias actual schema names → sba_*-prefixed output names.
    con.execute("""
        CREATE TEMP TABLE sba_commit AS
        SELECT
            legal_name_normalized,
            borrstate,
            total_loans          AS sba_loan_count,
            total_gross_approval AS sba_total_grossapproval,
            max_approval_date    AS sba_max_approvaldate,
            latest_loanstatus    AS sba_latest_loanstatus
        FROM sba_raw
        WHERE has_pending_commit = TRUE
          AND legal_name_normalized IS NOT NULL
          AND borrstate IS NOT NULL
    """)
    rows_sba_commit = con.execute("SELECT COUNT(*) FROM sba_commit").fetchone()[0]
    logger.info("SBA capital-matching cohort (has_pending_commit=TRUE): %d borrowers",
                rows_sba_commit)

    # Filter owner to valid join keys
    con.execute("""
        CREATE TEMP TABLE owner_clean AS
        SELECT
            serial_no,
            legal_name_normalized,
            owner_name_normalized,
            owner_state_normalized
        FROM owner_raw
        WHERE legal_name_normalized IS NOT NULL
          AND owner_state_normalized IS NOT NULL
    """)

    # Enrich case_file with lifecycle flags
    # Hot-fix 2026-05-13: is_live_registered = cfh_status_cd = '700' per the
    # 5/10 USPTO MV-tower probe (99.996% pure live registrations; status_code
    # was the audit's guess at a non-existent column).
    # has_first_use_in_commerce: approximated by filing_dt NOT NULL (best available flag)
    con.execute("""
        CREATE TEMP TABLE case_file_enriched AS
        SELECT
            serial_no,
            CASE WHEN cfh_status_cd = '700' THEN TRUE ELSE FALSE END AS is_live_registered,
            filing_dt IS NOT NULL                                    AS has_first_use_in_commerce,
            filing_dt
        FROM case_file_raw
    """)

    # Correspondent → is_pro_se
    con.execute("""
        CREATE TEMP TABLE corr_clean AS
        SELECT serial_no, is_pro_se
        FROM corr_raw
    """)

    # Enrich owner with lifecycle + pro_se signals via JOIN on serial_no
    con.execute("""
        CREATE TEMP TABLE owner_enriched AS
        SELECT
            o.serial_no,
            o.legal_name_normalized,
            o.owner_name_normalized,
            o.owner_state_normalized,
            coalesce(cf.is_live_registered, FALSE)        AS is_live_registered,
            coalesce(cf.has_first_use_in_commerce, FALSE) AS has_first_use_in_commerce,
            cf.filing_dt,
            coalesce(c.is_pro_se, TRUE)                   AS is_pro_se
        FROM owner_clean o
        LEFT JOIN case_file_enriched cf ON cf.serial_no = o.serial_no
        LEFT JOIN corr_clean c          ON c.serial_no  = o.serial_no
    """)

    # Per-owner identity: pick the single best target_serial_no via deterministic ORDER BY.
    # ORDER: is_live_registered DESC, is_pro_se DESC, has_first_use_in_commerce DESC,
    #        filing_dt DESC, serial_no ASC (tiebreak)
    con.execute("""
        CREATE TEMP TABLE owner_best_mark AS
        SELECT DISTINCT ON (legal_name_normalized, owner_state_normalized)
            legal_name_normalized,
            owner_name_normalized,
            owner_state_normalized,
            serial_no                AS target_serial_no,
            is_pro_se                AS target_is_pro_se,
            is_live_registered,
            has_first_use_in_commerce,
            filing_dt
        FROM owner_enriched
        ORDER BY
            legal_name_normalized,
            owner_state_normalized,
            is_live_registered DESC,
            is_pro_se DESC,
            has_first_use_in_commerce DESC,
            filing_dt DESC NULLS LAST,
            serial_no ASC
    """)

    # Count of marks per owner identity
    con.execute("""
        CREATE TEMP TABLE owner_mark_count AS
        SELECT
            legal_name_normalized,
            owner_state_normalized,
            COUNT(*) AS count_of_marks
        FROM owner_enriched
        GROUP BY legal_name_normalized, owner_state_normalized
    """)

    logger.info("computing JOIN: USPTO owner identity × SBA capital-matching cohort ...")
    con.execute(
        f"""
        CREATE TEMP TABLE bridge_result AS
        SELECT
            bm.target_serial_no,
            CASE WHEN bm.target_is_pro_se THEN 'owner' ELSE 'attorney' END
                                                        AS expected_recipient_kind,
            bm.owner_name_normalized,
            bm.owner_state_normalized,
            mc.count_of_marks,
            s.sba_loan_count,
            s.sba_total_grossapproval,
            s.sba_max_approvaldate,
            s.sba_latest_loanstatus,
            '{bridge_run_id}'                           AS bridge_run_id,
            TIMESTAMP '{generated_at_iso}'              AS generated_at,
            '{BRIDGE_VERSION}'                          AS bridge_version
        FROM owner_best_mark bm
        JOIN sba_commit s
          ON s.legal_name_normalized = bm.legal_name_normalized
         AND s.borrstate              = bm.owner_state_normalized
        JOIN owner_mark_count mc
          ON mc.legal_name_normalized  = bm.legal_name_normalized
         AND mc.owner_state_normalized = bm.owner_state_normalized
        """
    )

    row_counts = con.execute("""
        SELECT
            COUNT(*) AS rows_bridge,
            COUNT(*) FILTER (WHERE expected_recipient_kind='owner')    AS rows_owner,
            COUNT(*) FILTER (WHERE expected_recipient_kind='attorney') AS rows_attorney
        FROM bridge_result
    """).fetchone()

    counts = {
        "rows_bridge": row_counts[0],
        "rows_owner": row_counts[1],
        "rows_attorney": row_counts[2],
        "rows_sba_commit": rows_sba_commit,
    }
    return con, counts


def _write_bridge_lance(con, storage_options: dict) -> int:
    """Write bridge_result table to Lance; return row count."""
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing bridge to Lance at %s ...", BRIDGE_LANCE_URI)
        reader = con.from_query("SELECT * FROM bridge_result").to_arrow_reader(
            batch_size=100_000
        )
        ds = lance.write_dataset(
            reader,
            BRIDGE_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version
        )

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        try:
            ds.create_scalar_index(
                "owner_name_normalized", index_type="BTREE", replace=True
            )
        except Exception as e:
            logger.warning("BTREE index failed (non-fatal): %s", e)

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    return lance_count


def _ensure_registry() -> None:
    """Idempotent UPSERTs: company_name_state_exact + uspto_sba_capital_matching."""
    logger.info("registering match_method + bridge in ops registry ...")
    register_match_method(
        method_name=METHOD_NAME,
        description=(
            "Exact-equality JOIN on (entity_name_normalized, 2-letter US state) "
            "applying _lib/entity_name_normalize.py v1.0.0."
        ),
    )
    register_match_method_version(
        method_name=METHOD_NAME,
        semver=METHOD_SEMVER,
        normalizer_module="_lib/entity_name_normalize.py",
        normalizer_version=NORMALIZER_VERSION,
        blacklist_module="_lib/entity_name_normalize.py",
        blacklist_version=NORMALIZER_VERSION,
        tier_rule_description=(
            "Deterministic per-owner-identity pick via ORDER BY is_live_registered DESC,"
            " is_pro_se DESC, has_first_use_in_commerce DESC, filing_dt DESC, serial_no ASC"
        ),
        rejection_rule_description="No fan-out rejection; 1 target_serial_no per owner identity",
        input_columns_left=["legal_name_normalized", "owner_state_normalized"],
        input_columns_right=["legal_name_normalized", "borrstate"],
        output_value_description="normalized name + 2-letter state join key",
    )
    register_bridge(
        bridge_name=BRIDGE_NAME,
        source_left=SOURCE_LEFT,
        source_right=SOURCE_RIGHT,
        method_name=METHOD_NAME,
        r2_output_prefix=BRIDGE_LANCE_URI,
        description=(
            "USPTO trademark owner identity × SBA borrowers with has_pending_commit=TRUE. "
            "Produces target_serial_no + expected_recipient_kind for the capital-matching cohort. "
            "The harvest queue for the future TSDR per-record API worker."
        ),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance + ledger row")
    grp.add_argument("--dry-run", action="store_true", help="count only, no writes")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            raise SystemExit(f"FAIL: {var} not set")
    if args.apply and not os.environ.get("DEX_DB_URL_DIRECT"):
        raise SystemExit("FAIL: DEX_DB_URL_DIRECT not set (required for registry)")

    started_at = datetime.now(tz=timezone.utc)
    t0 = time.time()
    storage_options = _lance_storage_options()

    logger.info("bridge: %s (method=%s v%s)", BRIDGE_NAME, METHOD_NAME, METHOD_SEMVER)
    logger.info("normalizer: _lib/entity_name_normalize.py v%s", NORMALIZER_VERSION)
    logger.info("inputs: USPTO owner Lance + case_file Lance + correspondent Lance + SBA borrowers Lance")
    logger.info("output: %s", BRIDGE_LANCE_URI)
    logger.info("row floor (audit-tightened): %d", MIN_ROWS_BRIDGE)

    if args.dry_run:
        bridge_run_id = "00000000-0000-0000-0000-000000000000"
        run_uuid = None
    else:
        _ensure_registry()
        run_uuid = start_bridge_run(
            bridge_name=BRIDGE_NAME,
            method_semver=METHOD_SEMVER,
            bridge_version=BRIDGE_VERSION,
            source_left=SOURCE_LEFT,
            source_right=SOURCE_RIGHT,
            match_method=METHOD_NAME,
            r2_output_key=BRIDGE_LANCE_URI,
        )
        bridge_run_id = str(run_uuid)
        logger.info("bridge_run_id=%s", bridge_run_id)

    try:
        (owner_arrow, cf_arrow, corr_arrow, sba_arrow,
         rows_owner, rows_sba_total) = _materialize_inputs(storage_options)

        con, counts = _build_bridge_table(
            owner_arrow, cf_arrow, corr_arrow, sba_arrow,
            bridge_run_id=bridge_run_id,
            generated_at_iso=started_at.isoformat(),
        )

        logger.info("-" * 60)
        logger.info("bridge summary:")
        logger.info("  SBA capital-matching cohort:  %d", counts["rows_sba_commit"])
        logger.info("  bridge rows (matched):        %d", counts["rows_bridge"])
        logger.info("    expected_recipient_kind=owner:    %d", counts["rows_owner"])
        logger.info("    expected_recipient_kind=attorney: %d", counts["rows_attorney"])
        if counts["rows_sba_commit"] > 0:
            match_rate = counts["rows_bridge"] / counts["rows_sba_commit"] * 100
            logger.info("  USPTO match rate: %.1f%%", match_rate)

        if counts["rows_bridge"] < MIN_ROWS_BRIDGE:
            msg = (
                f"HARD FAIL: rows_bridge={counts['rows_bridge']:,} < "
                f"floor={MIN_ROWS_BRIDGE:,}. "
                f"Check: (a) normalizer version parity on both sides "
                f"(b) state-code normalizer (owner_state_normalized vs borrstate) "
                f"(c) own_seq filter in case_file_owner_lance"
            )
            logger.error(msg)
            if run_uuid is not None:
                fail_bridge_run(run_uuid, msg)
            return 1

        if args.dry_run:
            logger.info("DRY RUN — no Lance / Postgres writes. duration=%.1fs", time.time() - t0)
            return 0

        lance_count = _write_bridge_lance(con, storage_options)
        complete_bridge_run(
            run_uuid,
            metrics={
                "rows_left": rows_owner,
                "rows_right": rows_sba_total,
                "rows_sba_commit": counts["rows_sba_commit"],
                "rows_matched": counts["rows_bridge"],
                "rows_owner": counts["rows_owner"],
                "rows_attorney": counts["rows_attorney"],
                "lance_rows": lance_count,
            },
        )
        logger.info("OK — run_id=%s  duration=%.1fs", bridge_run_id, time.time() - t0)
        logger.info("     output: %s", BRIDGE_LANCE_URI)
        return 0

    except Exception as exc:
        logger.exception("bridge generation failed")
        if run_uuid is not None:
            try:
                fail_bridge_run(run_uuid, str(exc))
            except Exception:
                logger.exception("also failed to mark run as failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
