"""SAM open construction opportunities — expected-award-size banding (Pattern A enriched-cohort).

Open SAM federal construction opportunities (naics_code LIKE '23%',
response_deadline>now()) enriched with an expected-award-size band derived
from the USAspending construction base-award historical distribution.

LEFT (spine):   sam_gov/opps_active_lance — open construction opps
                (naics_code LIKE '23%' AND response_deadline > now()).
RIGHT (distrib): usaspending/contracts_lance — construction base awards
                (naics_code LIKE '23%' AND modification_number = '0').
Output:         bridges/construction_opps_sized_lance — one row per open
                construction opp, PK notice_id.

Tiered fallback hierarchy (T1 dropped — agency code-space misalignment per
validator Probe 3):
  T2 = (naics_code, pop_state)     — finest geo+NAICS cell
  T3 = (naics_code)                — NAICS nationwide
  T4 = substr(naics_code,1,3)      — 3-digit subsector
  T5 = substr(naics_code,1,2)='23' — sector-wide backstop (67,639 awards)

MIN_SAMPLE = 8. Each tier is gated: only cells with hist_sample_count >= 8
are used. T5 always terminates (67,639 >> 8).

Size field: base_and_all_options_value (99.9% positive after TRY_CAST DOUBLE;
validator-confirmed best of all candidates).

Band thresholds (validator-calibrated, Probe 2):
  micro  < $25K
  small  $25K – $250K
  mid    $250K – $2M
  large  $2M – $25M
  mega   >= $25M

Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A enriched-cohort emit":
NOT a new identity bridge — no rows in ops.bridges / ops.match_method_versions.
Per L28: cohort emit carries its own fresh bridge_run_id UUID for provenance.
Per L17: every row carries bridge_run_id UUID.
Per L29: TRY_CAST for VARCHAR -> typed casts on money/date fields.

Anti-pattern guards in effect:
# L42: no Content-Encoding: zstd header on R2 uploads
# L54: no LIST<VARCHAR> columns — all columns are scalars
# L59: no duckdb.typing. literal anywhere

Run via:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_sam_construction_opps_sized_emit.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants ──────────────────────────────────────────────────

R2_BUCKET = "dex-raw-landing-zone"

OPPS_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/sam_gov/opps_active_lance"
USA_LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/usaspending/contracts_lance"

OUTPUT_LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/bridges/construction_opps_sized_lance"
)
DATASET_SLUG = "construction_opps_sized_lance"
POLARIS_NAMESPACE = "bridges"

# Historical-distribution input floor: 67,639 positive-size rows confirmed by
# validator Probe 1 (base_and_all_options_value 99.9% positive on 67,696 base awards).
# Floor 55,000 ≈ 81% of the positive-size population.
MIN_HIST_FLOOR = 55_000

# Dataset row-count floor: validator live-probe 2,261 open construction opps.
# Floor 1,400 ≈ 62% of live count (leaves slack for daily feed fluctuation).
MIN_ROW_FLOOR = 1_400

EMIT_VERSION = "1.0.0"

TMP_DIR = "/tmp/lance"


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    script = Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=60)
        logger.info("Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def _read_contracts_arrow(storage_options: dict):
    """Read USAspending contracts filtered to construction base awards via Lance scanner.

    Filter is a SQL string (DataFusion backend) — NOT pyarrow starts_with
    (does not serialize to Substrait; see build_bridge_sam_construction_contractors_lance.py).
    Projection: only the 4 columns needed for the distribution computation.
    """
    import lance

    logger.info("scanning USAspending contracts (construction base awards) ...")
    ds = lance.dataset(USA_LANCE_URI, storage_options=storage_options)
    tbl = ds.scanner(
        columns=[
            "naics_code",
            "primary_place_of_performance_state_code",
            "base_and_all_options_value",
            "modification_number",
        ],
        filter="naics_code LIKE '23%' AND modification_number = '0'",
    ).to_table()
    logger.info("contracts base-award rows scanned: %d", tbl.num_rows)
    return tbl


def _read_opps_arrow(storage_options: dict):
    """Read SAM opps filtered to construction sector via Lance scanner.

    AUDIT-CONFIRMED columns (15) — naics_description does NOT exist in
    opps_active_lance; projecting it would crash the scanner.
    """
    import lance

    logger.info("scanning SAM opps active (construction sector) ...")
    ds = lance.dataset(OPPS_LANCE_URI, storage_options=storage_options)
    tbl = ds.scanner(
        columns=[
            "notice_id",
            "title",
            "sol_num",
            "naics_code",
            "pop_state",
            "pop_city",
            "pop_zip",
            "response_deadline",
            "posted_date",
            "set_aside",
            "set_aside_code",
            "department_agency",
            "notice_type",
            "link",
            "description",
        ],
        filter="naics_code LIKE '23%'",
    ).to_table()
    logger.info("SAM construction opps rows scanned: %d", tbl.num_rows)
    return tbl


def emit() -> int:
    """Build construction opps + expected-award-size band enriched cohort and write Lance."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    bridge_run_id = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc).isoformat()
    logger.info("bridge_run_id=%s generated_at=%s", bridge_run_id, generated_at)

    storage_options = _storage_options()

    contracts_arrow = _read_contracts_arrow(storage_options)
    opps_arrow = _read_opps_arrow(storage_options)

    con = _duckdb_conn()
    con.register("contracts_raw", contracts_arrow)
    con.register("opps_raw", opps_arrow)

    # --- Step 1: Cast + filter historical award-size values -------------------
    # TRY_CAST(base_and_all_options_value AS DOUBLE) per L29; drop non-positive.
    con.execute(
        """
        CREATE TEMP TABLE hist_positive AS
        SELECT
            naics_code,
            primary_place_of_performance_state_code,
            TRY_CAST(base_and_all_options_value AS DOUBLE) AS award_size
        FROM contracts_raw
        WHERE TRY_CAST(base_and_all_options_value AS DOUBLE) > 0
        """
    )
    hist_n = con.execute("SELECT count(*) FROM hist_positive").fetchone()[0]
    logger.info("historical positive-size base awards: %d", hist_n)
    if hist_n < MIN_HIST_FLOOR:
        raise RuntimeError(
            f"HARD FAIL: historical positive-size rows ({hist_n}) < floor ({MIN_HIST_FLOOR}). "
            f"Check contracts_lance schema drift or base_and_all_options_value population."
        )

    # --- Step 2: Build 4 distribution tables (gated by MIN_SAMPLE = 8) -------
    # Each table contains ONLY cells with count >= 8 (MIN_SAMPLE gate applied
    # as HAVING during aggregation — "filter-then-join" shape per reviewer c1
    # step-4 rewrite). A (naics, pop_state) cell that merely exists but has
    # count<8 is absent from t2_cells, so the opp's T2 join misses and
    # COALESCE falls to T3. This is the correct "finest USABLE tier" pattern.
    # Do NOT use bare COALESCE over ungated LEFT JOINs — that binds an opp to
    # a T2 cell that exists even with count<8, silently skipping the gate.

    # T2: (naics_code, pop_state) — geo + NAICS finest
    con.execute(
        """
        CREATE TEMP TABLE t2_cells AS
        SELECT
            naics_code,
            primary_place_of_performance_state_code AS pop_state,
            COUNT(*)                                             AS hist_sample_count,
            QUANTILE_CONT(award_size, 0.50)                     AS hist_median,
            QUANTILE_CONT(award_size, 0.25)                     AS hist_p25,
            QUANTILE_CONT(award_size, 0.75)                     AS hist_p75
        FROM hist_positive
        GROUP BY naics_code, primary_place_of_performance_state_code
        HAVING COUNT(*) >= 8
        """
    )
    t2_n = con.execute("SELECT count(*) FROM t2_cells").fetchone()[0]
    logger.info("T2 cells (count>=8): %d", t2_n)

    # T3: (naics_code) — NAICS nationwide
    con.execute(
        """
        CREATE TEMP TABLE t3_cells AS
        SELECT
            naics_code,
            COUNT(*)                                             AS hist_sample_count,
            QUANTILE_CONT(award_size, 0.50)                     AS hist_median,
            QUANTILE_CONT(award_size, 0.25)                     AS hist_p25,
            QUANTILE_CONT(award_size, 0.75)                     AS hist_p75
        FROM hist_positive
        GROUP BY naics_code
        HAVING COUNT(*) >= 8
        """
    )
    t3_n = con.execute("SELECT count(*) FROM t3_cells").fetchone()[0]
    logger.info("T3 cells (count>=8): %d", t3_n)

    # T4: (substr(naics_code,1,3)) — 3-digit subsector (236/237/238)
    con.execute(
        """
        CREATE TEMP TABLE t4_cells AS
        SELECT
            SUBSTR(naics_code, 1, 3)                             AS naics_3,
            COUNT(*)                                             AS hist_sample_count,
            QUANTILE_CONT(award_size, 0.50)                     AS hist_median,
            QUANTILE_CONT(award_size, 0.25)                     AS hist_p25,
            QUANTILE_CONT(award_size, 0.75)                     AS hist_p75
        FROM hist_positive
        GROUP BY SUBSTR(naics_code, 1, 3)
        HAVING COUNT(*) >= 8
        """
    )
    t4_n = con.execute("SELECT count(*) FROM t4_cells").fetchone()[0]
    logger.info("T4 cells (count>=8): %d", t4_n)

    # T5: (substr(naics_code,1,2)='23') — sector-wide backstop; single cell.
    # 67,639 positive-size sector-23 awards >> MIN_SAMPLE=8; always terminates
    # the fallback chain. Catches bare naics_code='23' opps (10 of 2,261 live
    # opps per validator Probe 5b) that no T4 substr(...,1,3) cell matches.
    con.execute(
        """
        CREATE TEMP TABLE t5_cells AS
        SELECT
            SUBSTR(naics_code, 1, 2)                             AS naics_2,
            COUNT(*)                                             AS hist_sample_count,
            QUANTILE_CONT(award_size, 0.50)                     AS hist_median,
            QUANTILE_CONT(award_size, 0.25)                     AS hist_p25,
            QUANTILE_CONT(award_size, 0.75)                     AS hist_p75
        FROM hist_positive
        WHERE SUBSTR(naics_code, 1, 2) = '23'
        GROUP BY SUBSTR(naics_code, 1, 2)
        HAVING COUNT(*) >= 8
        """
    )
    t5_n = con.execute("SELECT count(*) FROM t5_cells").fetchone()[0]
    logger.info("T5 cells (count>=8, sector '23'): %d", t5_n)
    if t5_n < 1:
        raise RuntimeError("HARD FAIL: T5 sector-23 cell absent — fallback chain cannot terminate.")

    # --- Step 3: Filter opps to genuinely open construction -------------------
    con.execute(
        """
        CREATE TEMP TABLE opps_open AS
        SELECT *
        FROM opps_raw
        WHERE naics_code LIKE '23%'
          AND TRY_CAST(response_deadline AS TIMESTAMP) > now()
        """
    )
    opps_n = con.execute("SELECT count(*) FROM opps_open").fetchone()[0]
    logger.info("open construction opps (deadline > now): %d", opps_n)

    # --- Step 4: Tiered join — "filter-then-join" (gated cells) --------------
    # LEFT JOIN each tier table that has already been filtered to count>=8.
    # COALESCE(t2.*, t3.*, t4.*, t5.*) is safe because:
    #   - t2_cells only has cells with count>=8, so t2 miss = below-threshold or no-geo-match
    #   - t3_cells only has cells with count>=8, same logic
    #   - t4_cells only has cells with count>=8
    #   - t5_cells always has the sector-23 cell (67,639 >> 8), so every opp terminates
    # The COALESCE order (T2→T3→T4→T5) = finest-first. Every opp is banded.
    # Geo join for T2: opp.pop_state <-> t2.pop_state (both from contracts_lance
    # primary_place_of_performance_state_code and opps_active_lance pop_state).
    logger.info("joining opps to tiered distribution cells ...")
    reader = con.execute(
        f"""
        SELECT
            o.notice_id,
            o.title,
            o.sol_num,
            o.naics_code,
            o.pop_state,
            o.pop_city,
            o.pop_zip,
            o.response_deadline,
            o.posted_date,
            o.set_aside,
            o.set_aside_code,
            o.department_agency,
            o.notice_type,
            o.link,
            o.description,

            -- Tiered size distribution (finest USABLE tier with count >= 8)
            -- size_basis_tier records which tier was matched for each opp.
            CASE
                WHEN t2.hist_sample_count IS NOT NULL THEN 'T2'
                WHEN t3.hist_sample_count IS NOT NULL THEN 'T3'
                WHEN t4.hist_sample_count IS NOT NULL THEN 'T4'
                ELSE 'T5'
            END                                                     AS size_basis_tier,

            COALESCE(t2.hist_sample_count,
                     t3.hist_sample_count,
                     t4.hist_sample_count,
                     t5.hist_sample_count)                          AS hist_sample_count,

            COALESCE(t2.hist_p25,
                     t3.hist_p25,
                     t4.hist_p25,
                     t5.hist_p25)                                   AS hist_p25,

            COALESCE(t2.hist_median,
                     t3.hist_median,
                     t4.hist_median,
                     t5.hist_median)                                AS hist_median,

            COALESCE(t2.hist_p75,
                     t3.hist_p75,
                     t4.hist_p75,
                     t5.hist_p75)                                   AS hist_p75,

            -- expected_award_value = matched-cell median (robust to right tail;
            -- do NOT use mean — construction contract values are right-skewed).
            COALESCE(t2.hist_median,
                     t3.hist_median,
                     t4.hist_median,
                     t5.hist_median)                                AS expected_award_value,

            -- size_band: bucket expected_award_value on validator-calibrated thresholds
            -- (Probe 2: micro 25.3% / small 37.7% / mid 23.0% / large 10.0% / mega 4.0%).
            CASE
                WHEN COALESCE(t2.hist_median, t3.hist_median,
                              t4.hist_median, t5.hist_median) < 25000
                THEN 'micro'
                WHEN COALESCE(t2.hist_median, t3.hist_median,
                              t4.hist_median, t5.hist_median) < 250000
                THEN 'small'
                WHEN COALESCE(t2.hist_median, t3.hist_median,
                              t4.hist_median, t5.hist_median) < 2000000
                THEN 'mid'
                WHEN COALESCE(t2.hist_median, t3.hist_median,
                              t4.hist_median, t5.hist_median) < 25000000
                THEN 'large'
                ELSE 'mega'
            END                                                     AS size_band,

            -- Provenance per L17/L28 (cohort emit, not an identity bridge)
            '{bridge_run_id}'                                       AS bridge_run_id,
            '{EMIT_VERSION}'                                        AS emit_version,
            TIMESTAMP '{generated_at}'                             AS generated_at

        FROM opps_open o

        -- T2: finest (geo + NAICS) — pre-filtered to count>=8
        LEFT JOIN t2_cells t2
            ON t2.naics_code = o.naics_code
           AND t2.pop_state   = o.pop_state

        -- T3: NAICS nationwide — pre-filtered to count>=8
        LEFT JOIN t3_cells t3
            ON t3.naics_code = o.naics_code

        -- T4: 3-digit subsector — pre-filtered to count>=8
        LEFT JOIN t4_cells t4
            ON t4.naics_3 = SUBSTR(o.naics_code, 1, 3)

        -- T5: sector-23 backstop — always present (67,639 >> 8)
        LEFT JOIN t5_cells t5
            ON t5.naics_2 = SUBSTR(o.naics_code, 1, 2)
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=20_000,
        )
        rows = ds.count_rows()
        logger.info("Lance written: %d rows (version %s)", rows, ds.version)
        if rows < MIN_ROW_FLOOR:
            raise RuntimeError(f"HARD FAIL: rows ({rows}) < floor ({MIN_ROW_FLOOR})")

        for col in ("notice_id", "pop_state", "naics_code", "size_band"):
            logger.info("BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("optimize failed (non-fatal): %s", exc)

    logger.info("cohort emit complete — rows=%d uri=%s", rows, OUTPUT_LANCE_URI)

    _register_polaris(
        DATASET_SLUG,
        "bridges.construction_opps_sized_lance — SAM open federal construction "
        "opportunities (naics 23%, response_deadline>now()) enriched with an "
        "expected-award-size band derived from the USAspending construction "
        "base-award historical distribution (T2..T5 fallback hierarchy, "
        "MIN_SAMPLE=8). PK notice_id; BTREE on notice_id + pop_state + "
        "naics_code + size_band. Pattern A enriched-cohort emit per "
        "DATA-FACTORY-ARCHITECTURE-PATTERNS.md. Scoring is the follow-on "
        "sam-construction-opps-score cycle.",
    )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="SAM construction opps expected-award-size band — Lance enriched-cohort emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        storage_options = _storage_options()
        opps = _read_opps_arrow(storage_options)
        import duckdb as _ddb
        con = _ddb.connect()
        con.register("opps_raw", opps)
        n = con.execute(
            "SELECT count(*) FROM opps_raw "
            "WHERE naics_code LIKE '23%' "
            "  AND TRY_CAST(response_deadline AS TIMESTAMP) > now()"
        ).fetchone()[0]
        logger.info(
            "DRY-RUN: %d open construction opps would be emitted "
            "(pass --apply to emit)",
            n,
        )
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
