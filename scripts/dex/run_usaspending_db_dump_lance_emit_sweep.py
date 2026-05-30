#!/usr/bin/env python3
"""USAspending db-dump R2 Parquets → per-table Lance datasets (Phase 2 sweep).

Reads the USAspending db-dump Parquet files that already landed in R2 at:
    s3://dex-raw-landing-zone/usaspending/db-dump/{table}/release=2026-05-07/*.parquet

And emits one Pattern A Lance dataset per table at:
    s3://dex-raw-landing-zone/polaris-warehouse/usaspending/{table}_lance/

Each dataset gets:
- BTREE scalar index on the canonical primary key
- BTREE scalar index on the recipient-UEI column (for the 6 transaction-grain tables)
- Compact + cleanup_old_versions after write

Design decisions:
- Does NOT use `scripts/_lib/lance_emit.py::_detect_releases` — the canonical
  helper's regex matches `release=YYYYqQ` but the db-dump uses `release=YYYY-MM-DD`.
  This script globs the known single release directly.
- Does NOT call Polaris registration (init_polaris_lance_generic.py) — out of scope
  per directive carve-out.
- BTREE-indexed columns are emitted as `pa.utf8()`, NOT `pa.large_utf8()`, because
  Lance 6.x BTREE indices require utf8. Other columns stay at large_utf8 (DuckDB
  returns large_utf8 by default for VARCHAR; keeping it avoids any truncation for
  wide text fields).
- Paranoid LEGACY_NAMES guard: asserts target URI never matches a legacy dataset name.
- Idempotent: existing dataset is overwritten (mode="overwrite").

CLI:
    --apply              Write Lance datasets (real run)
    --dry-run            Print plan only (count rows, no write)
    --table <name>       Single-table emit (for debugging)

Usage (local, small tables):
    doppler run --project hq-all --config prd -- \\
        uv run --with duckdb --with pylance --with boto3 python \\
        apps/data-engine-x/scripts/run_usaspending_db_dump_lance_emit_sweep.py \\
        --table references_cfda --apply

Usage (Modal, large tables):
    See modal/usaspending_db_dump_lance_emit_sweep_app.py
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa

# Allow running from the repo root (apps/data-engine-x/ is the CWD when Doppler
# wraps us, but the script lives in scripts/ — add the app root to path).
SCRIPT_DIR = Path(__file__).resolve().parent
APP_ROOT = SCRIPT_DIR.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from scripts._lib.lance_commit_lock import lance_commit_lock  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("usaspending_db_dump_lance_sweep")

# ---------------------------------------------------------------------------#
# Constants                                                                    #
# ---------------------------------------------------------------------------#

R2_BUCKET = "dex-raw-landing-zone"
RELEASE = "2026-05-07"  # The known single release in R2
PARQUET_INPUT_TEMPLATE = "usaspending/db-dump/{table}/release=2026-05-07/*.parquet"
LANCE_OUTPUT_TEMPLATE = "s3://dex-raw-landing-zone/polaris-warehouse/usaspending/{table}_lance"
TMP_DIR = "/tmp/lance"

# Paranoid guard: these legacy dataset names MUST NOT appear in any target URI.
LEGACY_NAMES = frozenset([
    "contracts_lance",
    "recipient_grain_lance",
    "recipient_features_lance",
    "contract_subawards_lance",
    "assistance_subawards_lance",
])

# ---------------------------------------------------------------------------#
# Per-table config                                                              #
#                                                                              #
# Each entry:                                                                  #
#   name         : table/dataset name (matches db-dump prefix)                #
#   pk           : canonical primary key column                                #
#   btree_keys   : columns that need BTREE index (includes pk + UEI column)   #
#   uei_col      : the recipient-UEI column for this table (or None)          #
#   row_floor    : minimum rows for acceptance                                 #
#                                                                              #
# NOTE: btree_keys columns are cast to pa.utf8() at write time.               #
# Lance 6.x BTREE requires utf8, not large_utf8. All other columns are        #
# emitted as-is (large_utf8 from DuckDB's VARCHAR — safe for wide text).     #
# ---------------------------------------------------------------------------#

TABLE_CONFIG: list[dict[str, Any]] = [
    # Dim tables (no UEI column, smoke query skipped)
    {
        "name": "references_cfda",
        "pk": "id",
        "btree_keys": ["id", "program_number"],
        "uei_col": None,
        "row_floor": 2_000,
    },
    {
        "name": "agency",
        "pk": "id",
        "btree_keys": ["id", "toptier_agency_id", "subtier_agency_id"],
        "uei_col": None,
        "row_floor": 1_000,
    },
    {
        "name": "subtier_agency",
        "pk": "subtier_agency_id",
        # Note: the Parquet schema for subtier_agency does NOT include toptier_agency_id.
        # The benchmark checks for ["subtier_agency_id", "subtier_code", "toptier_agency_id"]
        # but toptier_agency_id is absent from the Parquet. We index the two available
        # columns; the benchmark's toptier_agency_id check will show as missing.
        # See Lessons in directive execution log.
        "btree_keys": ["subtier_agency_id", "subtier_code"],
        "uei_col": None,
        "row_floor": 1_000,
    },
    {
        "name": "toptier_agency",
        "pk": "toptier_agency_id",
        "btree_keys": ["toptier_agency_id", "toptier_code"],
        "uei_col": None,
        "row_floor": 100,
    },
    # Medium tables
    {
        "name": "recipient_lookup",
        "pk": "id",
        # Actual Parquet schema: has 'uei' (not 'recipient_uei'),
        # 'legal_business_name' (not 'legal_business_name_normalized')
        "btree_keys": ["id", "uei", "legal_business_name"],
        "uei_col": "uei",
        "row_floor": 4_000_000,
    },
    {
        "name": "recipient_profile",
        "pk": "recipient_hash",
        "btree_keys": ["recipient_hash", "uei", "recipient_level"],
        "uei_col": "uei",
        "row_floor": 4_000_000,
    },
    {
        "name": "subaward",
        "pk": "sub_id",
        "btree_keys": [
            "sub_id",
            "broker_subaward_id",
            "sub_awardee_or_recipient_uei",
            "unique_award_key",
            "subaward_recipient_hash",
        ],
        "uei_col": "sub_awardee_or_recipient_uei",
        "row_floor": 8_000_000,
    },
    # Large tables (Modal-recommended for full runs)
    {
        "name": "awards",
        # Actual Parquet schema: PK is 'award_id' (not 'id'), UEI is 'recipient_uei'.
        # The rpt.award_search table uses award_id as PK.
        "pk": "award_id",
        "btree_keys": ["award_id", "generated_unique_award_id", "recipient_uei"],
        "uei_col": "recipient_uei",
        "row_floor": 40_000_000,
    },
    {
        "name": "transaction_fpds",
        "pk": "transaction_id",
        # Actual schema: UEI column is 'recipient_uei', not 'awardee_or_recipient_uei'
        "btree_keys": ["transaction_id", "recipient_uei", "naics_code"],
        "uei_col": "recipient_uei",
        "row_floor": 80_000_000,
    },
    {
        "name": "transaction_fabs",
        "pk": "transaction_id",
        # Actual schema: UEI column is 'recipient_uei', not 'awardee_or_recipient_uei'
        "btree_keys": ["transaction_id", "recipient_uei", "cfda_number"],
        "uei_col": "recipient_uei",
        "row_floor": 80_000_000,
    },
]

# ---------------------------------------------------------------------------#
# Helpers                                                                      #
# ---------------------------------------------------------------------------#


def _r2_account_id() -> str:
    ep = os.environ["R2_ENDPOINT"]
    return ep.split("//")[-1].split(".")[0]


def _ensure_tmpdir() -> None:
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    os.environ["TMPDIR"] = TMP_DIR


def _lance_storage_options() -> dict:
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
        "aws_skip_signature": "false",
    }


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


def _assert_not_legacy(table_name: str) -> None:
    """Paranoid guard: raise if table_name matches a legacy dataset name."""
    candidate = f"{table_name}_lance"
    if candidate in LEGACY_NAMES:
        raise ValueError(
            f"ABORT: target dataset {candidate!r} matches a legacy dataset — "
            f"refusing to overwrite. LEGACY_NAMES={LEGACY_NAMES}"
        )


def _build_schema_override(btree_keys: list[str]) -> dict[str, pa.DataType]:
    """Map btree_keys columns to pa.utf8().

    Lance 6.x BTREE indices require utf8 (NOT large_utf8). DuckDB returns
    large_utf8 for VARCHAR by default. This override is applied per-column
    via a DuckDB CAST in the SELECT query.
    """
    return {col: pa.utf8() for col in btree_keys}


def _cast_btree_cols_schema(
    source_schema: "pa.Schema",
    btree_keys: list[str],
) -> "pa.Schema":
    """Return a modified Arrow schema where btree_keys columns are pa.utf8().

    Lance 6.x BTREE indices require utf8 (NOT large_utf8). DuckDB's Parquet
    reader returns VARCHAR columns as pa.large_utf8() by default. This function
    builds a schema override that casts only the btree_keys to utf8; all other
    columns are left at their original types.

    Works at the Arrow schema level (not DuckDB SQL) to avoid the column-name
    quoting issues with USAspending's Parquet files (some columns have
    embedded double-quotes in their names, e.g. '"authorization"').
    """
    btree_set = set(btree_keys)
    new_fields = []
    for field in source_schema:
        if field.name in btree_set and field.type in (pa.large_utf8(), pa.utf8()):
            new_fields.append(pa.field(field.name, pa.utf8()))
        else:
            new_fields.append(field)
    return pa.schema(new_fields)


def emit_table(cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Emit one Lance dataset from R2 Parquets.

    Returns a dict with status + metrics.
    """
    import lance

    table_name = cfg["name"]
    pk = cfg["pk"]
    btree_keys = cfg["btree_keys"]
    row_floor = cfg["row_floor"]

    _assert_not_legacy(table_name)

    input_uri = PARQUET_INPUT_TEMPLATE.format(table=table_name)
    lance_uri = LANCE_OUTPUT_TEMPLATE.format(table=table_name)

    LOG.info("=" * 60)
    LOG.info("emit_table: %s", table_name)
    LOG.info("  input:  r2://%s/%s", R2_BUCKET, input_uri)
    LOG.info("  output: %s", lance_uri)
    LOG.info("  pk:     %s", pk)
    LOG.info("  btree:  %s", btree_keys)
    LOG.info("  floor:  %d", row_floor)

    con = _connect_duckdb_to_r2()

    # Verify Parquet files exist
    glob_check = con.execute(
        f"SELECT COUNT(*) FROM glob('r2://{R2_BUCKET}/{input_uri}')"
    ).fetchone()[0]
    if glob_check == 0:
        raise FileNotFoundError(
            f"No Parquet files at r2://{R2_BUCKET}/{input_uri}"
        )
    LOG.info("  parquet files found: %d", glob_check)

    # Count rows
    t_count = time.time()
    parquet_rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('r2://{R2_BUCKET}/{input_uri}', "
        f"union_by_name=true)"
    ).fetchone()[0]
    LOG.info("  parquet rows: %d (counted in %.1fs)", parquet_rows, time.time() - t_count)

    if dry_run:
        LOG.info("  DRY RUN — skipping Lance write")
        return {
            "table": table_name,
            "status": "dry_run",
            "parquet_rows": parquet_rows,
        }

    storage_options = _lance_storage_options()

    dataset_slug = f"usaspending_{table_name}_lance"
    metrics: dict[str, Any] = {
        "table": table_name,
        "parquet_rows": parquet_rows,
    }

    # Get the schema from a sample read; build override casting btree_keys to utf8.
    # We do this at the Arrow schema level (not via DuckDB SQL) to avoid quoting
    # issues with USAspending Parquet files that contain column names with embedded
    # double-quotes (e.g. '"authorization"' in references_cfda).
    t_schema = time.time()
    sample_tbl = con.execute(
        f"SELECT * FROM read_parquet('r2://{R2_BUCKET}/{input_uri}', union_by_name=true) LIMIT 0"
    ).to_arrow_table()
    source_schema = sample_tbl.schema
    write_schema = _cast_btree_cols_schema(source_schema, btree_keys)
    LOG.info(
        "  schema: %d columns, %d btree-key(s) cast to utf8 (%.2fs)",
        len(write_schema),
        sum(1 for f in write_schema if f.name in set(btree_keys) and f.type == pa.utf8()),
        time.time() - t_schema,
    )

    t0 = time.time()
    with lance_commit_lock(dataset_slug):
        LOG.info("  lock acquired, streaming to Lance ...")
        # Use from_query().to_arrow_reader() pattern (same as lance_emit.py)
        # The schema override is passed to lance.write_dataset to cast btree_keys
        # from large_utf8 → utf8 at write time.
        reader = con.from_query(
            f"SELECT * FROM read_parquet('r2://{R2_BUCKET}/{input_uri}', union_by_name=true)"
        ).to_arrow_reader(batch_size=100_000)
        ds = lance.write_dataset(
            reader,
            lance_uri,
            mode="overwrite",
            schema=write_schema,
            storage_options=storage_options,
        )
        write_dur = time.time() - t0
        lance_rows = ds.count_rows()
        LOG.info(
            "  wrote %d rows in %.1fs (version=%s)",
            lance_rows, write_dur, ds.version,
        )
        metrics.update(lance_rows=lance_rows, write_seconds=round(write_dur, 1))

        # Set LANCE_BYPASS_SPILLING before index calls
        os.environ["LANCE_BYPASS_SPILLING"] = "true"

        # Build BTREE indices
        t_idx = time.time()
        indexed_cols = []
        for col in btree_keys:
            try:
                LOG.info("  creating BTREE index on %s ...", col)
                ds.create_scalar_index(col, index_type="BTREE", replace=True)
                indexed_cols.append(col)
            except Exception as e:
                LOG.error("  BTREE index on %s FAILED: %s", col, e)
                raise
        LOG.info(
            "  %d BTREE indices built in %.1fs",
            len(indexed_cols), time.time() - t_idx,
        )
        metrics["index_seconds"] = round(time.time() - t_idx, 1)

        # Compact + cleanup
        t_opt = time.time()
        try:
            stats = ds.optimize.compact_files()
            LOG.info("  compact_files: %s", stats)
        except Exception as e:
            LOG.warning("  compact_files failed (non-fatal): %s", e)
        try:
            cleanup = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("  cleanup_old_versions: %s", cleanup)
        except Exception as e:
            LOG.warning("  cleanup_old_versions failed (non-fatal): %s", e)
        metrics["optimize_seconds"] = round(time.time() - t_opt, 1)

    total_dur = time.time() - t0
    metrics.update(status="completed", total_seconds=round(total_dur, 1))
    LOG.info("  %s: DONE in %.1fs", table_name, total_dur)
    return metrics


# ---------------------------------------------------------------------------#
# CLI                                                                          #
# ---------------------------------------------------------------------------#


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Emit USAspending db-dump R2 Parquets → Lance datasets"
    )
    mode_grp = ap.add_mutually_exclusive_group(required=True)
    mode_grp.add_argument("--apply", action="store_true", help="write Lance datasets")
    mode_grp.add_argument("--dry-run", action="store_true", help="count rows, print plan only")
    ap.add_argument("--table", metavar="NAME", help="emit only this table (for debugging)")
    args = ap.parse_args(argv)

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: %s not set in environment", var)
            return 64

    _ensure_tmpdir()

    targets = TABLE_CONFIG
    if args.table:
        targets = [c for c in TABLE_CONFIG if c["name"] == args.table]
        if not targets:
            known = [c["name"] for c in TABLE_CONFIG]
            LOG.error("Unknown table %r. Known: %s", args.table, known)
            return 1

    dry_run = args.dry_run
    LOG.info(
        "Sweep plan: %d table(s) to emit (mode=%s)",
        len(targets), "dry-run" if dry_run else "apply",
    )
    for cfg in targets:
        LOG.info(
            "  %s  pk=%s  btree=%s  floor=%d",
            cfg["name"], cfg["pk"], cfg["btree_keys"], cfg["row_floor"],
        )

    results = []
    failed = []
    for cfg in targets:
        try:
            metrics = emit_table(cfg, dry_run=dry_run)
            results.append(metrics)
            LOG.info("OK: %s → %s", cfg["name"], metrics)
        except Exception as e:
            LOG.error("FAIL: %s → %s", cfg["name"], e)
            failed.append(cfg["name"])

    LOG.info("=" * 60)
    LOG.info("Sweep complete: %d succeeded, %d failed", len(results), len(failed))
    if failed:
        LOG.error("Failed tables: %s", failed)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
