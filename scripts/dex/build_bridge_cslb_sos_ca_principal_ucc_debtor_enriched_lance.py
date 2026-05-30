#!/usr/bin/env python3
"""CSLB x SoS x principal x UCC-debtor enriched cohort -- Pattern A enriched-cohort emit (Lance).

Two-hop join: cohort x UCC-debtor SoS bridge (aggregated per entity_num via DuckDB GROUP BY).

Identical-twin structure to PR #484 (build_bridge_cslb_sos_ca_principal_sba_enriched_lance.py)
and PR #489 (build_bridge_cslb_sos_ca_principal_usaspending_enriched_lance.py),
swapping SBA/USAspending-side inputs for UCC-debtor-side. PR #485 list-encoding fix applied
(array_to_string(..., '|') for LIST<VARCHAR>).

Per Pattern A enriched-cohort (PR #469 / PR #484): identity-bridge registry calls are ABSENT
(no ops.bridges row written, no match-method-registry imports). YES ops.data_sources row.
YES per-row inherited + own bridge_run_id provenance (3 inherited propagations + own UUID).

INNER side: bridges.cslb_sos_ca_principal_lance (PR #483; 616,622 principal-grain rows).
Identity hop (aggregation source): bridges.ucc_ca_debtor_sos_ca_owner_lance (PR #490;
  3,062,504 rows; carries entity_num + ucc1_num + initial_filing_date (VARCHAR M/D/Y)
  + confidence_tier + bridge_run_id); GROUPED BY entity_num for per-entity rollup;
  LEFT JOINed back to the cohort spine.

OPTION A (direct from bridge): ucc_ca.filings_lance (7.75M rows) adds no lien-dollar amounts
and no lender names — bridge has all necessary fields at the join grain. filings_lance is
FORBIDDEN in this script per validator Option A decision.

Aggregate at entity_num grain to handle multi-filing-per-entity cases:
  ucc_active_lien_count = count(DISTINCT ucc1_num),
  ucc_distinct_filings_set = array_to_string(list_distinct(list(ucc1_num)), '|'),
  ucc_max_filing_date = max(strptime(initial_filing_date, '%m/%d/%Y'))  -- parsed DATE, NOT bare VARCHAR MAX,
  ucc_debtor_sos_bridge_run_ids = array_to_string(list_distinct(list(bridge_run_id)), '|').

Validator probe: 250,958 enriched rows (~40.7% of cohort 616,622). HIGH coverage (much higher
than directive's 5-20% estimate and USAspending's 1.25%). MIN_ENRICHED_FLOOR = 200,000.
Consumer filter: WHERE ucc_active_lien_count IS NOT NULL (or > 0).

Per DATA-FACTORY-ARCHITECTURE-PATTERNS.md §"Pattern A enriched-cohort emit":
NOT a new identity bridge (no new ops.bridges row, no new match method).

Run (apply):
    cd ~/hq-all/apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python scripts/build_bridge_cslb_sos_ca_principal_ucc_debtor_enriched_lance.py --apply

Dry-run (print row count + coverage stats only):
    uv run python scripts/build_bridge_cslb_sos_ca_principal_ucc_debtor_enriched_lance.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# sys.path.insert per PR #481 fix -- allows _lib imports from worktree root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

# ---------------------------------------------------------------------------
# Constants (load-bearing -- verify-s1.sh greps must match exactly)
# ---------------------------------------------------------------------------

DATASET_SLUG = "cslb_sos_ca_principal_ucc_debtor_enriched_lance"
BRIDGE_VERSION = "1.0.0"

# Validator probe: cohort = 616,622; LEFT JOIN preserves scale; floor at 500K
# gives ~19% headroom.
MIN_ROWS_MATCHED = 500_000

# Secondary floor: probe shows 250,958 rows enriched (40.7% coverage; ~20% headroom).
# Guards against silent regression in the UCC-debtor identity-bridge chain.
MIN_ENRICHED_FLOOR = 200_000

COHORT_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_lance"
UCC_DEBTOR_BRIDGE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/ucc_ca_debtor_sos_ca_owner_lance"
OUTPUT_LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/bridges/cslb_sos_ca_principal_ucc_debtor_enriched_lance"

TMP_DIR = "/tmp/lance"

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)


def _storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _ensure_db_url() -> None:
    """Normalize DEX_DB_URL_DIRECT from DATABASE_URL fallback if needed."""
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CSLB x SoS x principal x UCC-debtor enriched cohort -- Pattern A enriched-cohort emit (Lance)."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the Lance dataset. Without this flag runs in dry-run mode (row+coverage counts only).",
    )
    args = parser.parse_args()

    _ensure_db_url()
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("TMPDIR", TMP_DIR)
    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")

    import duckdb
    import lance

    storage_options = _storage_options()

    # Per-emit provenance (own UUID separate from any inherited upstream IDs).
    BRIDGE_RUN_ID = str(uuid.uuid4())
    generated_at = datetime.now(timezone.utc)
    generated_at_iso = generated_at.isoformat()

    logger.info(
        "emit %s starting at %s (output=%s) apply=%s",
        BRIDGE_RUN_ID, generated_at_iso, OUTPUT_LANCE_URI, args.apply,
    )

    # ---- Step 1: PyLance scanners (two inputs) ---- #

    logger.info("opening %s ...", COHORT_URI)
    ds_cohort = lance.dataset(COHORT_URI, storage_options=storage_options)
    cohort_arrow = ds_cohort.scanner().to_table()  # all cohort cols
    logger.info("  cohort: %d rows x %d cols", cohort_arrow.num_rows, len(cohort_arrow.column_names))

    logger.info("opening %s ...", UCC_DEBTOR_BRIDGE_URI)
    ds_bridge = lance.dataset(UCC_DEBTOR_BRIDGE_URI, storage_options=storage_options)
    bridge_arrow = ds_bridge.scanner(
        columns=["entity_num", "ucc1_num", "initial_filing_date", "confidence_tier", "bridge_run_id"],
    ).to_table()
    logger.info("  ucc_ca_debtor_sos_ca_owner bridge (projected): %d rows", bridge_arrow.num_rows)

    # ---- Step 2: DuckDB per-entity rollup then LEFT JOIN ---- #

    con = duckdb.connect()
    con.execute("SET threads=2")
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET max_temp_directory_size='120GB'")
    con.execute("SET preserve_insertion_order=false")
    con.register("co", cohort_arrow)
    con.register("br", bridge_arrow)

    # Step 2a: per-entity_num rollup over the UCC-debtor bridge.
    # Aggregate FIRST so cohort spine sees one row per entity_num on right side of LEFT JOIN.
    # ucc_max_filing_date: MUST use strptime(initial_filing_date, '%m/%d/%Y') before MAX --
    #   bridge stores dates as VARCHAR M/D/Y; bare MAX(VARCHAR) gives lexicographic max
    #   which is WRONG ('12/31/1979' > '01/01/2025'). Per validator P4 + contract.md §"Aggregation columns".
    # List columns serialized as pipe-delimited VARCHAR per PR #485 list-encoding fix to avoid
    # Lance "Definition buffer size too large" on LIST<VARCHAR> columns (Lance 1.5.x limitation).
    con.execute(
        """
        CREATE TEMP TABLE ucc_agg AS
        SELECT
            br.entity_num,
            count(DISTINCT br.ucc1_num)                                         AS ucc_active_lien_count,
            array_to_string(list_distinct(list(br.ucc1_num)), '|')               AS ucc_distinct_filings_set,
            max(strptime(br.initial_filing_date, '%m/%d/%Y'))                    AS ucc_max_filing_date,
            array_to_string(list_distinct(list(br.bridge_run_id)), '|')          AS ucc_debtor_sos_bridge_run_ids
        FROM br
        GROUP BY br.entity_num
        """
    )

    # Step 2b: cohort spine x LEFT JOIN ucc_agg.
    # Per L17 provenance: rename cohort's bridge_run_id -> cslb_principal_bridge_run_id;
    # cohort's cslb_sos_bridge_run_id propagates verbatim; UCC-debtor bridge_run_ids land
    # as ucc_debtor_sos_bridge_run_ids (pipe-delimited list_distinct per PR #485);
    # this emit's own UUID stamps as bridge_run_id. EXCLUDE cohort's bridge_version +
    # generated_at (this emit re-stamps them).
    con.execute(
        f"""
        CREATE TEMP TABLE enriched AS
        SELECT
            co.* EXCLUDE (bridge_run_id, bridge_version, generated_at),
            co.bridge_run_id                                      AS cslb_principal_bridge_run_id,
            ua.ucc_active_lien_count,
            ua.ucc_distinct_filings_set,
            ua.ucc_max_filing_date,
            ua.ucc_debtor_sos_bridge_run_ids,
            '{BRIDGE_RUN_ID}'                                     AS bridge_run_id,
            '{BRIDGE_VERSION}'                                    AS bridge_version,
            TIMESTAMP '{generated_at_iso}'                        AS generated_at
        FROM co
        LEFT JOIN ucc_agg ua ON co.sos_entity_num = ua.entity_num
        """
    )

    rows_out = con.execute("SELECT count(*) FROM enriched").fetchone()[0]
    rows_enriched = con.execute(
        "SELECT count(*) FROM enriched WHERE ucc_active_lien_count IS NOT NULL"
    ).fetchone()[0]
    coverage_pct = (rows_enriched / rows_out * 100) if rows_out else 0.0
    max_lien_count = con.execute(
        "SELECT coalesce(max(ucc_active_lien_count), 0) FROM enriched"
    ).fetchone()[0]

    logger.info(
        "enriched: %d rows (%d with UCC = %.2f%% coverage; max_lien_count=%s)",
        rows_out, rows_enriched, coverage_pct, max_lien_count,
    )

    if rows_out < MIN_ROWS_MATCHED:
        logger.error("HARD FAIL: rows=%d < MIN_ROWS_MATCHED=%d", rows_out, MIN_ROWS_MATCHED)
        return 1

    if rows_enriched < MIN_ENRICHED_FLOOR:
        logger.error(
            "HARD FAIL: enriched rows=%d < MIN_ENRICHED_FLOOR=%d (UCC-debtor bridge chain regression)",
            rows_enriched, MIN_ENRICHED_FLOOR,
        )
        return 1

    if not args.apply:
        logger.info(
            "DRY-RUN: would write %d rows (%d enriched = %.2f%% coverage). Pass --apply to write.",
            rows_out, rows_enriched, coverage_pct,
        )
        return 0

    # ---- Step 3: Lance write inside commit lock + dual BTREE ---- #

    t0 = time.time()
    with lance_commit_lock(DATASET_SLUG):
        logger.info("writing Lance dataset to %s ...", OUTPUT_LANCE_URI)
        reader = con.from_query("SELECT * FROM enriched").to_arrow_reader(
            batch_size=100_000,
        )
        ds = lance.write_dataset(
            reader,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_count = ds.count_rows()
        logger.info(
            "wrote %d rows in %.1fs (version=%s)", lance_count, write_dur, ds.version,
        )

        try:
            ds.create_scalar_index("cslb_license_no", index_type="BTREE", replace=True)
            logger.info("BTREE on cslb_license_no: OK")
        except Exception as e:
            logger.error("BTREE on cslb_license_no FAILED: %s", e)
            raise
        try:
            ds.create_scalar_index("sos_entity_num", index_type="BTREE", replace=True)
            logger.info("BTREE on sos_entity_num: OK")
        except Exception as e:
            logger.error("BTREE on sos_entity_num FAILED: %s", e)
            raise

        try:
            ds.optimize.compact_files()
        except Exception as e:
            logger.warning("compact_files failed (non-fatal): %s", e)
        try:
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as e:
            logger.warning("cleanup_old_versions failed (non-fatal): %s", e)

    logger.info(
        "OK: bridges.cslb_sos_ca_principal_ucc_debtor_enriched_lance written (%d rows; enriched=%d/%.2f%%; bridge_run_id=%s)",
        lance_count, rows_enriched, coverage_pct, BRIDGE_RUN_ID,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
