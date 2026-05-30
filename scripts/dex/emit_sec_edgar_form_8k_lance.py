#!/usr/bin/env python3
"""Re-emit SEC EDGAR Form 8-K (R2 Parquet) as a Lance dataset.

Reads from the 8-K R2 prefix (per-quarter and per-year partitioned streams),
unions across all 8 streams, attaches an `item_stream` column for partitioning
inside Lance, and writes a single Lance dataset at
``polaris-warehouse/sec_edgar/form_8k_lance/``.

BTREE on ``accession_number`` — the natural join key for any per-filing
downstream lookup. The same accession appears across multiple streams (a
filing with Item 1.01 + Item 2.03 + Item 5.02 has rows in all three), so
the BTREE supports both single-stream and cross-stream queries.

Streams unioned:
  PER_QUARTER (year=YYYY/quarter=Q/...):
    filings, items_index, item_8_01_other_events
  PER_YEAR (year=YYYY/...):
    item_5_02_officer_changes, item_1_01_material_agreement,
    item_2_01_acquisition_disposition,
    item_2_03_direct_financial_obligation,
    item_5_01_change_in_control

Schemas differ across streams; the Lance union projects each stream onto a
common envelope: (accession_number, cik_normalized, item_stream,
report_year, report_quarter, raw_row_json). The narrow envelope plus
``raw_row_json`` (full row encoded as JSON) preserves fidelity without
forcing schema unification.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/emit_sec_edgar_form_8k_lance.py --dry-run
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/emit_sec_edgar_form_8k_lance.py --apply
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)


# Match the source script's PER_QUARTER_STREAMS + PER_YEAR_STREAMS.
PER_QUARTER_STREAMS: tuple[str, ...] = (
    "filings",
    "items_index",
    "item_8_01_other_events",
)
PER_YEAR_STREAMS: tuple[str, ...] = (
    "item_5_02_officer_changes",
    "item_1_01_material_agreement",
    "item_2_01_acquisition_disposition",
    "item_2_03_direct_financial_obligation",
    "item_5_01_change_in_control",
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "sec-edgar/form-8k"
LANCE_URI = "s3://dex-raw-landing-zone/polaris-warehouse/sec_edgar/form_8k_lance"
DATASET_SLUG = "sec_edgar_form_8k_lance"
BTREE_COLUMN = "accession_number"


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _connect_duckdb_to_r2():
    import duckdb
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{_r2_account_id()}'
        )
        """
    )
    return con


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


def _build_union_sql() -> str:
    """Construct a UNION ALL SELECT projecting each stream onto the common
    envelope. Uses DuckDB's TRY(read_parquet(glob)) so empty streams are
    silently skipped (a fresh ingest may have no Item 5.01 rows for a year,
    etc.).
    """
    parts: list[str] = []

    for stream in PER_QUARTER_STREAMS:
        glob_pat = (
            f"r2://{R2_BUCKET}/{R2_PREFIX}/year=*/quarter=*/{stream}/data.parquet"
        )
        # Cast accession + cik to text; coerce everything else through raw_row_json.
        parts.append(
            f"""
            SELECT
              accession_number::VARCHAR AS accession_number,
              cik_normalized::VARCHAR    AS cik_normalized,
              '{stream}'                  AS item_stream,
              CAST(report_year AS SMALLINT) AS report_year,
              CAST(report_quarter AS SMALLINT) AS report_quarter,
              to_json(t)::VARCHAR         AS raw_row_json
            FROM read_parquet('{glob_pat}') AS t
            """
        )

    for stream in PER_YEAR_STREAMS:
        glob_pat = (
            f"r2://{R2_BUCKET}/{R2_PREFIX}/year=*/{stream}/data.parquet"
        )
        parts.append(
            f"""
            SELECT
              accession_number::VARCHAR AS accession_number,
              cik_normalized::VARCHAR    AS cik_normalized,
              '{stream}'                  AS item_stream,
              CAST(report_year AS SMALLINT) AS report_year,
              CAST(NULL AS SMALLINT)        AS report_quarter,
              to_json(t)::VARCHAR         AS raw_row_json
            FROM read_parquet('{glob_pat}') AS t
            """
        )

    return "\nUNION ALL\n".join(parts)


def emit_lance(apply: bool) -> int:
    con = _connect_duckdb_to_r2()
    union_sql = _build_union_sql()

    LOG.info("=" * 60)
    LOG.info("sec_edgar_form_8k_lance: counting input rows ...")
    try:
        total = con.execute(
            f"SELECT COUNT(*) FROM ({union_sql})"
        ).fetchone()[0]
    except Exception as exc:
        # DuckDB raises if NO files match any of the globs. The 8-K cycle's
        # backfill may not have started yet — log + exit zero rows.
        LOG.warning("input glob enumeration raised: %s", exc)
        total = 0

    LOG.info("input parquet rows (across all 8 streams): %d", total)

    if not apply:
        LOG.info("DRY RUN — exiting without writing Lance dataset")
        return 0

    if total == 0:
        LOG.info("no input rows — skipping Lance write")
        return 0

    import lance

    storage_options = _lance_storage_options()

    t0 = time.time()
    reader = con.from_query(union_sql).to_arrow_reader(batch_size=100_000)
    LOG.info("writing Lance dataset (mode=overwrite) to %s ...", LANCE_URI)
    ds = lance.write_dataset(
        reader,
        LANCE_URI,
        mode="overwrite",
        storage_options=storage_options,
    )
    write_dur = time.time() - t0
    lance_count = ds.count_rows()
    LOG.info(
        "wrote %d rows to Lance in %.1fs (version=%s)",
        lance_count, write_dur, ds.version,
    )

    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    t_idx = time.time()
    LOG.info("creating BTREE scalar index on %s ...", BTREE_COLUMN)
    try:
        ds.create_scalar_index(BTREE_COLUMN, index_type="BTREE", replace=True)
        LOG.info("  index built in %.1fs", time.time() - t_idx)
    except Exception as exc:
        LOG.warning("  BTREE index build failed (non-fatal): %s", exc)

    t1 = time.time()
    LOG.info("optimize: compact + cleanup_older_than=7d ...")
    try:
        stats = ds.optimize.compact_files()
        LOG.info("  compact_files: %s", stats)
    except Exception as exc:
        LOG.warning("  compact_files failed (non-fatal): %s", exc)
    try:
        cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
        LOG.info("  cleanup_old_versions: %s", cleanup)
    except Exception as exc:
        LOG.warning("  cleanup_old_versions failed (non-fatal): %s", exc)
    LOG.info("optimize done in %.1fs", time.time() - t1)

    if total != lance_count:
        LOG.error(
            "FAIL: row count mismatch parquet=%d lance=%d",
            total, lance_count,
        )
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true", help="write Lance dataset")
    grp.add_argument("--dry-run", action="store_true", help="counts only")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    return emit_lance(apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())
