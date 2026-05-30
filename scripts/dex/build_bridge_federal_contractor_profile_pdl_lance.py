#!/usr/bin/env python3
"""Emit bridges.federal_contractor_profile_pdl_lance.

Pattern A enriched-cohort emit. One row per UEI in
``spines/federal_contractor_profile_lance`` (101,413 confirmed federal prime
award winners with certs / fleet / capital / sub-award / agency-mix
enrichment), LEFT JOINed on uei against ``bridges/sam_pdl_usaspending_lance``
to carry the PDL LinkedIn URL through to the orchestrator's fast path.

The hydration orchestrator currently uses `bridges/sam_pdl_usaspending_lance`
(294,842 UEIs at SAM ∩ PDL grain) and runtime-filters with
``lifetime_total_obligated > $150K`` + ``latest_action_date >= now - 90d``.
That cohort is "registered in SAM AND matched in PDL, that happen to have
federal contracting history." The output dataset here is the inverse-shape:
"confirmed prime award winners (already filtered) PLUS the PDL LinkedIn
URL when one exists." Switching the orchestrator to read this dataset
tightens the cohort universe from 294,842 → 101,413 and bakes the
$150K-obligation filter into the dataset rather than the SQL.

Sources
  Left:  s3://dex-raw-landing-zone/polaris-warehouse/spines/federal_contractor_profile_lance
         (101,413 rows, UEI grain, ~80 cols)
  Right: s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_usaspending_lance
         (294,842 rows, UEI grain — projecting (uei, pdl_linkedin_url) only)

Output
  s3://dex-raw-landing-zone/polaris-warehouse/bridges/federal_contractor_profile_pdl_lance
  Schema: profile.* + pdl_linkedin_url
  Grain:  1 row per UEI (matches the LEFT side's grain)
  BTREE:  uei

Run via:
    cd apps/data-engine-x && \\
      doppler run --project hq-all --config prd -- \\
      uv run python -m scripts.build_bridge_federal_contractor_profile_pdl_lance
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

# Allow running from scripts/ or from project root.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))

import duckdb
import lance
import pyarrow as pa

from scripts._lib.lance_commit_lock import lance_commit_lock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("build_bridge_federal_contractor_profile_pdl_lance")

DATASET_SLUG = "federal_contractor_profile_pdl_lance"

PROFILE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/spines/federal_contractor_profile_lance"
)
BRIDGE_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/sam_pdl_usaspending_lance"
)
OUTPUT_LANCE_URI = (
    "s3://dex-raw-landing-zone/polaris-warehouse/bridges/federal_contractor_profile_pdl_lance"
)

# Volume floor. The LEFT JOIN preserves the left-side row count exactly,
# so any deviation from the input profile row count is a structural bug.
EXPECTED_INPUT_ROWS = 101_413


def _r2_storage_options() -> dict[str, str]:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "auto",
        "aws_virtual_hosted_style_request": "false",
    }


def main() -> int:
    opts = _r2_storage_options()

    # ── Phase 1: open Lance scanners ──────────────────────────────────────
    LOG.info("opening profile spine: %s", PROFILE_LANCE_URI)
    profile_ds = lance.dataset(PROFILE_LANCE_URI, storage_options=opts)
    profile_n = profile_ds.count_rows()
    LOG.info("profile spine rows: %d", profile_n)
    if profile_n != EXPECTED_INPUT_ROWS:
        LOG.warning(
            "profile spine row count %d != expected %d — proceeding but worth checking",
            profile_n,
            EXPECTED_INPUT_ROWS,
        )

    LOG.info("opening pdl bridge: %s (projecting uei + pdl_linkedin_url)", BRIDGE_LANCE_URI)
    bridge_ds = lance.dataset(BRIDGE_LANCE_URI, storage_options=opts)
    bridge_n = bridge_ds.count_rows()
    LOG.info("pdl bridge rows: %d", bridge_n)

    # ── Phase 2: materialize both into Arrow tables ──────────────────────
    t0 = time.monotonic()
    LOG.info("scanning profile spine into Arrow ...")
    profile_tbl = profile_ds.to_table()
    LOG.info("profile Arrow table: %d rows, %d cols, %.1f MB",
             profile_tbl.num_rows, profile_tbl.num_columns,
             profile_tbl.nbytes / 1024 / 1024)

    LOG.info("scanning pdl bridge into Arrow (2-col projection) ...")
    bridge_tbl = bridge_ds.to_table(columns=["uei", "pdl_linkedin_url"])
    LOG.info("pdl bridge Arrow table: %d rows, %.1f MB",
             bridge_tbl.num_rows, bridge_tbl.nbytes / 1024 / 1024)

    # ── Phase 3: DuckDB LEFT JOIN ─────────────────────────────────────────
    LOG.info("executing LEFT JOIN in DuckDB ...")
    con = duckdb.connect(":memory:")
    try:
        con.register("profile", profile_tbl)
        con.register("bridge", bridge_tbl)

        # Inspect for duplicate UEIs on the right side. sam_pdl_usaspending_lance
        # is documented as UEI-grain, but in practice the 2026-05-16 emit landed
        # with ~19K duplicate UEIs (same root cause class as the
        # federal_contractor_master dedup repair on 2026-05-25). LEFT JOINing
        # against duplicates would inflate the output past the LEFT-side grain,
        # so collapse the right side to 1 row per UEI here, preferring rows
        # with a populated pdl_linkedin_url and (among those) the
        # lexicographically-first URL for determinism.
        right_dup_count = con.execute(
            "SELECT COUNT(*) - COUNT(DISTINCT uei) FROM bridge"
        ).fetchone()[0]
        LOG.info(
            "right-side duplicate UEIs in sam_pdl_usaspending_lance: %d "
            "(deduping in-place; not propagated)",
            right_dup_count,
        )

        con.execute(
            """
            CREATE TEMP TABLE bridge_dedup AS
            SELECT uei, pdl_linkedin_url
            FROM (
                SELECT
                    uei,
                    pdl_linkedin_url,
                    ROW_NUMBER() OVER (
                        PARTITION BY uei
                        ORDER BY
                            (pdl_linkedin_url IS NULL) ASC,  -- non-null first
                            pdl_linkedin_url ASC             -- deterministic tie-break
                    ) AS rn
                FROM bridge
            )
            WHERE rn = 1
            """
        )
        dedup_count = con.execute(
            "SELECT COUNT(*) FROM bridge_dedup"
        ).fetchone()[0]
        LOG.info(
            "bridge_dedup row count: %d (was %d → collapsed %d dupes)",
            dedup_count,
            bridge_tbl.num_rows,
            bridge_tbl.num_rows - dedup_count,
        )

        joined_tbl = con.execute(
            """
            SELECT
                p.*,
                b.pdl_linkedin_url
            FROM profile AS p
            LEFT JOIN bridge_dedup AS b
              ON p.uei = b.uei
            """
        ).fetch_arrow_table()
        LOG.info(
            "join result: %d rows, %d cols (%.1f MB)",
            joined_tbl.num_rows,
            joined_tbl.num_columns,
            joined_tbl.nbytes / 1024 / 1024,
        )

        # Pre-write gate: row count must equal the LEFT side count.
        if joined_tbl.num_rows != profile_n:
            raise RuntimeError(
                f"joined row count {joined_tbl.num_rows} != profile row count {profile_n} "
                f"(LEFT JOIN should preserve left grain exactly). Refusing to emit."
            )

        # Per-column probe of the new field.
        link_null_n = joined_tbl["pdl_linkedin_url"].null_count
        link_non_null_n = joined_tbl.num_rows - link_null_n
        LOG.info(
            "pdl_linkedin_url non-null on %d / %d rows (%.1f%%)",
            link_non_null_n,
            joined_tbl.num_rows,
            100.0 * link_non_null_n / joined_tbl.num_rows,
        )

    finally:
        con.close()

    LOG.info("DuckDB join wall time: %.1f s", time.monotonic() - t0)

    # ── Phase 4: write Lance dataset under advisory lock ─────────────────
    LOG.info("writing Lance dataset: %s", OUTPUT_LANCE_URI)
    t1 = time.monotonic()
    with lance_commit_lock(DATASET_SLUG):
        out_ds = lance.write_dataset(
            joined_tbl,
            OUTPUT_LANCE_URI,
            mode="overwrite",
            storage_options=opts,
        )
        LOG.info(
            "Lance write complete: %d rows in %.1f s",
            out_ds.count_rows(),
            time.monotonic() - t1,
        )

        # ── Phase 5: BTREE on uei ─────────────────────────────────────────
        LOG.info("creating BTREE scalar index on uei ...")
        t2 = time.monotonic()
        out_ds.create_scalar_index("uei", index_type="BTREE", replace=True)
        LOG.info("BTREE created in %.1f s", time.monotonic() - t2)

    # ── Phase 6: post-write verification ─────────────────────────────────
    final_ds = lance.dataset(OUTPUT_LANCE_URI, storage_options=opts)
    final_n = final_ds.count_rows()
    LOG.info("post-write row count: %d", final_n)

    if final_n != profile_n:
        LOG.error(
            "post-write row count %d != expected %d (profile spine)",
            final_n,
            profile_n,
        )
        return 1

    # Re-probe non-null linkedin via the final dataset (independent of the
    # in-memory Arrow table).
    final_link_n = final_ds.to_table(columns=["pdl_linkedin_url"])[
        "pdl_linkedin_url"
    ].null_count
    final_link_present = final_n - final_link_n
    LOG.info(
        "final pdl_linkedin_url non-null: %d / %d (%.1f%%) — fast-path coverage",
        final_link_present,
        final_n,
        100.0 * final_link_present / final_n,
    )

    LOG.info("emit complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
