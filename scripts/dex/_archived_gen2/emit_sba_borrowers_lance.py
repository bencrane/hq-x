#!/usr/bin/env python3
"""Lance-emit: SBA canonical borrowers derive.

Reads `polaris-warehouse/sba/loans_lance/` via pyarrow.dataset + Arrow-bridge
to DuckDB (NOT the lance-duckdb extension — unstable on macOS arm64 per Lance
canary cycle report 2026-05-12).

Filters `borrname IS NOT NULL AND borrstate IS NOT NULL` to exclude EIDL rows
(which have NULL borrower fields per the audit decision in
`emit_sba_loans_lance.py`).

Groups by `(legal_name_normalized, borrstate, borrzip)` to derive one row per
canonical borrower. Writes to Lance at
`s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/`.

Schema augmentation (v1.1.0, 2026-05-27): adds `borrstreet_normalized`
sourced via a side-scan of raw 7a + 504 parquets at
`s3://dex-raw-landing-zone/sba/program={7a,504}/decade=*/*.parquet`. The raw
`borrstreet` field is normalized via `scripts._lib.address_normalize` and
the most common normalized street per (legal_name_normalized, borrstate,
borrzip5) is attached to each borrower row. This is a side channel — it
does NOT modify `sba/loans_lance/` (which is schema-frozen).

Coverage augmentation (v1.2.0, 2026-05-28): backfills `borrstreet_normalized`
for the ~84.8% of borrowers whose only SBA loan history is PPP (and so were
not reachable from the 7a+504 raw parquets). The fill source is
`sba/ppp_borrowers_lance.borrower_address_normalized`, which was baked at
PPP-emit time using the SAME `_lib.address_normalize.normalize_address_street`
base form. COALESCE order is `(7a/504-derived, PPP-derived)` — existing values
are NEVER overwritten; PPP only fills NULL gaps. Net effect: borrstreet
coverage jumps from ~18% (7a+504 only) to ~99.7% (full population minus the
residual EIDL/no-address-source slice).

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sba_borrowers_lance.py --apply

  doppler run --project hq-all --config prd -- \\
    uv run --with duckdb --with pylance --with pyarrow python \\
    apps/data-engine-x/scripts/emit_sba_borrowers_lance.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

from scripts._lib.address_normalize import (  # noqa: E402
    normalize_address_street,
    register_address_udf,
)
from scripts._lib.entity_name_normalize import (  # noqa: E402
    normalize_entity_name,
)
from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("emit_sba_borrowers_lance")

LOANS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/loans_lance/"
BORROWERS_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sba/borrowers_lance/"
DATASET_SLUG = "sba_borrowers_lance"
TMP_DIR = "/tmp/lance"

# Row floor per directive §"Volume floors"
ROW_FLOOR = 8_000_000


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _emit(dry_run: bool) -> int:
    import lance

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR

    logger.info("=" * 60)
    logger.info("emit_sba_borrowers_lance")
    logger.info("input:  %s", LOANS_LANCE_URI)
    logger.info("output: %s", BORROWERS_LANCE_URI)

    storage_options = _lance_storage_options()

    # Arrow-bridge: open Lance dataset, scan needed columns, filter nulls.
    logger.info("opening loans_lance via pyarrow (Arrow-bridge pattern) ...")
    loans_ds = lance.dataset(LOANS_LANCE_URI, storage_options=storage_options)
    logger.info("loans_lance total rows: %d", loans_ds.count_rows())

    needed_cols = [
        "legal_name_normalized", "borrname", "borrstate", "borrzip",
        "grossapproval", "approvaldate", "loanstatus", "bankname",
        "franchisename", "naicscode", "disbursementdate",
        "loan_id",
    ]
    # Filter out rows where borrname IS NULL (i.e. EIDL rows)
    # Use & on Expression objects — pc.and_() is not registered in Substrait.
    import pyarrow.compute as pc
    scanner = loans_ds.scanner(
        columns=needed_cols,
        filter=pc.field("borrname").is_valid() & pc.field("borrstate").is_valid(),
    )
    logger.info("scanning loans with borrname IS NOT NULL AND borrstate IS NOT NULL ...")
    loans_arrow = scanner.to_table()
    logger.info("filtered loans rows: %d", len(loans_arrow))

    if dry_run:
        # For dry-run, estimate borrower count via a quick DuckDB group-by.
        import duckdb
        con = duckdb.connect()
        con.register("loans_filtered", loans_arrow)
        est = con.execute(
            """SELECT COUNT(DISTINCT (legal_name_normalized, borrstate, borrzip))
               FROM loans_filtered
               WHERE legal_name_normalized IS NOT NULL"""
        ).fetchone()[0]
        logger.info("DRY RUN — estimated canonical borrowers: %d (floor=%d, pass=%s)",
                    est, ROW_FLOOR, est >= ROW_FLOOR)
        if est < ROW_FLOOR:
            logger.error("FAIL: estimated borrowers %d < floor %d", est, ROW_FLOOR)
            return 1
        return 0

    # Full derive via DuckDB
    import duckdb
    con = duckdb.connect()
    con.register("loans_filtered", loans_arrow)

    # Register Python UDFs so DuckDB can normalize raw 7a/504 street + name
    # with the SAME logic Python uses elsewhere.
    register_address_udf(con, fn_name="py_normalize_address_street")
    try:
        con.create_function(
            "py_normalize_entity",
            normalize_entity_name,
            ["VARCHAR"],
            "VARCHAR",
            null_handling="special",
        )
    except Exception:
        try:
            con.remove_function("py_normalize_entity")
        except Exception:
            pass
        con.create_function(
            "py_normalize_entity",
            normalize_entity_name,
            ["VARCHAR"],
            "VARCHAR",
            null_handling="special",
        )

    # Configure httpfs so DuckDB can read raw parquets from R2 directly.
    con.execute("INSTALL httpfs; LOAD httpfs;")
    r2_endpoint = os.environ["R2_ENDPOINT"].replace("https://", "")
    con.execute(f"SET s3_endpoint='{r2_endpoint}';")
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    con.execute(f"SET s3_access_key_id='{os.environ['R2_ACCESS_KEY_ID']}';")
    con.execute(f"SET s3_secret_access_key='{os.environ['R2_SECRET_ACCESS_KEY']}';")

    logger.info("deriving canonical borrowers via DuckDB group-by ...")
    borrower_sql = """
    SELECT
        legal_name_normalized,
        MAX(borrname)                                       AS borrname_sample,
        borrstate,
        borrzip,
        COUNT(*)                                            AS total_loans,
        SUM(grossapproval)                                  AS total_gross_approval,
        MAX(approvaldate)                                   AS max_approval_date,
        MIN(approvaldate)                                   AS min_approval_date,
        MAX(loanstatus)                                     AS latest_loanstatus,
        BOOL_OR(loanstatus = 'COMMIT')                      AS has_pending_commit,
        ARRAY_AGG(DISTINCT franchisename)
            FILTER (WHERE franchisename IS NOT NULL)        AS franchise_brands_set,
        ARRAY_AGG(DISTINCT naicscode)
            FILTER (WHERE naicscode IS NOT NULL)            AS naics_codes_set,
        ARRAY_AGG(DISTINCT bankname)
            FILTER (WHERE bankname IS NOT NULL)             AS lender_set,
        MEDIAN(
            CASE WHEN disbursementdate IS NOT NULL AND approvaldate IS NOT NULL
                 THEN date_diff('day', approvaldate, disbursementdate)
            END
        )                                                   AS time_to_disburse_p50
    FROM loans_filtered
    WHERE legal_name_normalized IS NOT NULL
      AND borrstate IS NOT NULL
    GROUP BY legal_name_normalized, borrstate, borrzip
    """
    # .arrow() returns RecordBatchReader; .read_all() materializes to pyarrow.Table
    borrowers_arrow = con.execute(borrower_sql).arrow().read_all()
    logger.info("derived borrowers: %d rows", len(borrowers_arrow))

    if len(borrowers_arrow) < ROW_FLOOR:
        logger.error("FAIL: borrowers=%d < floor=%d", len(borrowers_arrow), ROW_FLOOR)
        return 1

    # ------------------------------------------------------------------
    # Street side-scan (v1.1.0 augmentation)
    # ------------------------------------------------------------------
    # Read raw 7a + 504 parquets (which DO have borrstreet, dropped by
    # the schema-frozen loans_lance union). Normalize entity name + street
    # with the SAME rule the loans emit applies, then attach the MODE
    # (most common normalized street) per (legal_name_normalized, borrstate,
    # borrzip5) back to the borrowers aggregate.
    logger.info("side-scanning raw 7a + 504 parquets for borrstreet ...")
    t_side = time.time()
    con.register("borrowers", borrowers_arrow)
    raw_glob_7a = "s3://dex-raw-landing-zone/sba/program=7a/decade=*/*.parquet"
    raw_glob_504 = "s3://dex-raw-landing-zone/sba/program=504/decade=*/*.parquet"
    # Zip5 normalization: handle DOUBLE→VARCHAR cast residue ('92110.0'),
    # ZIP+4 dashes ('92110-4313'), and pad short results with leading zeros
    # so a 4-digit value '5491' becomes '05491'. Pure VARCHAR — never cast
    # to BIGINT because non-numeric inputs trip the cast.
    zip5_expr = (
        "LPAD(SUBSTR(REGEXP_REPLACE(TRIM(CAST({col} AS VARCHAR)), "
        "'(\\.0+|-\\d+).*$', ''), 1, 5), 5, '0')"
    )
    side_scan_sql = f"""
    WITH raw_union AS (
        SELECT borrname, borrstate, borrzip, borrstreet
        FROM read_parquet(['{raw_glob_7a}', '{raw_glob_504}'],
                          union_by_name=true, hive_partitioning=true)
        WHERE borrname IS NOT NULL
          AND borrstate IS NOT NULL
          AND borrstreet IS NOT NULL
    ),
    normalized AS (
        SELECT
            py_normalize_entity(borrname) AS legal_name_normalized,
            UPPER(TRIM(borrstate))        AS borrstate_norm,
            {zip5_expr.format(col="borrzip")} AS borrzip5,
            py_normalize_address_street(borrstreet) AS street_norm
        FROM raw_union
    ),
    counted AS (
        SELECT legal_name_normalized, borrstate_norm, borrzip5, street_norm,
               COUNT(*) AS n
        FROM normalized
        WHERE legal_name_normalized IS NOT NULL
          AND street_norm           IS NOT NULL
        GROUP BY 1,2,3,4
    ),
    mode_per_borrower AS (
        SELECT legal_name_normalized, borrstate_norm AS borrstate, borrzip5,
               ARG_MAX(street_norm, n) AS borrstreet_normalized
        FROM counted
        GROUP BY 1,2,3
    )
    SELECT
        b.*,
        m.borrstreet_normalized
    FROM borrowers b
    LEFT JOIN mode_per_borrower m
      ON  b.legal_name_normalized = m.legal_name_normalized
      AND b.borrstate             = m.borrstate
      AND {zip5_expr.format(col="b.borrzip")} = m.borrzip5
    """
    borrowers_arrow = con.execute(side_scan_sql).arrow().read_all()
    side_dur = time.time() - t_side
    import pyarrow.compute as pc_
    street_col = borrowers_arrow.column("borrstreet_normalized")
    cov = pc_.sum(pc_.is_valid(street_col)).as_py()
    logger.info(
        "post 7a+504 side-scan borrstreet_normalized: %d / %d rows (%.1f%%) in %.1fs",
        cov, len(borrowers_arrow), 100.0 * cov / max(1, len(borrowers_arrow)), side_dur,
    )

    # ------------------------------------------------------------------
    # PPP backfill (v1.2.0)
    # ------------------------------------------------------------------
    # The 7a+504 side-scan above can only attach borrstreet for the ~15%
    # of canonical borrowers whose loans_lance footprint includes 7a or
    # 504 rows. The remaining ~85% are PPP-only borrowers — they exist
    # in loans_lance (because PPP loans land there too) but the side-scan
    # never touches `program=ppp/` raw parquets.
    #
    # sba/ppp_borrowers_lance already carries `borrower_address_normalized`
    # baked at 99.6% coverage of its 10.18M PPP borrowers, normalized via
    # the SAME canonical `_lib.address_normalize.normalize_address_street`
    # base form used by the 7a+504 side-scan. Natural key
    # (legal_name_normalized, borrstate, borrzip) is identical between the
    # two aggregates (same source, same normalizer). LEFT JOIN + COALESCE
    # is the architecturally clean fill: reuse already-emitted
    # normalization work rather than re-parsing 11M raw PPP parquet rows.
    #
    # COALESCE order: `(7a/504-derived, PPP-derived)`. If a borrower has
    # loans in both programs, the 7a/504 value is preserved. PPP only
    # fills NULL gaps.
    logger.info("opening sba/ppp_borrowers_lance for borrstreet backfill ...")
    PPP_BORROWERS_LANCE_URI = (
        "s3://dex-raw-landing-zone/polaris-warehouse/sba/ppp_borrowers_lance/"
    )
    t_back = time.time()
    ppp_ds = lance.dataset(PPP_BORROWERS_LANCE_URI, storage_options=storage_options)
    ppp_arrow = ppp_ds.scanner(
        columns=[
            "legal_name_normalized",
            "borrstate",
            "borrzip",
            "borrower_address_normalized",
        ],
    ).to_table()
    logger.info("  ppp_borrowers_lance scanned: %d rows", len(ppp_arrow))
    con.register("borrowers_post_side_scan", borrowers_arrow)
    con.register("ppp_borr", ppp_arrow)
    borrowers_arrow = con.execute(
        """
        SELECT
            s.* EXCLUDE (borrstreet_normalized),
            COALESCE(s.borrstreet_normalized, p.borrower_address_normalized)
                AS borrstreet_normalized
        FROM borrowers_post_side_scan s
        LEFT JOIN ppp_borr p
          ON  s.legal_name_normalized = p.legal_name_normalized
          AND s.borrstate              = p.borrstate
          AND s.borrzip                = p.borrzip
        """
    ).arrow().read_all()
    back_dur = time.time() - t_back

    # Free the PPP table from both Python and DuckDB.
    del ppp_arrow
    try:
        con.unregister("ppp_borr")
        con.unregister("borrowers_post_side_scan")
    except Exception:
        pass

    street_col = borrowers_arrow.column("borrstreet_normalized")
    cov_post = pc_.sum(pc_.is_valid(street_col)).as_py()
    cov_lift = cov_post - cov
    logger.info(
        "post PPP backfill borrstreet_normalized: %d / %d rows (%.1f%%) "
        "[+%d rows filled by PPP in %.1fs]",
        cov_post, len(borrowers_arrow),
        100.0 * cov_post / max(1, len(borrowers_arrow)),
        cov_lift, back_dur,
    )

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing borrowers to Lance (mode=overwrite) ...")
        ds = lance.write_dataset(
            borrowers_arrow,
            BORROWERS_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info("wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version)

        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        logger.info("creating BTREE index on legal_name_normalized ...")
        t_idx = time.time()
        try:
            ds.create_scalar_index("legal_name_normalized", index_type="BTREE", replace=True)
            logger.info("  BTREE built in %.1fs", time.time() - t_idx)
        except Exception as e:
            logger.error("BTREE index FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info("=" * 60)
    logger.info("OK — borrowers written: %d", lance_count)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Lance emit: SBA canonical borrowers")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            logger.error("FAIL: %s not set", var)
            return 64

    return _emit(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
