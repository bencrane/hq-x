"""Caltrans CCOP active bid solicitations → Lance emit (Pattern A).

Reads ZSTD Parquet snapshots written by run_caltrans_ccop_to_r2.py from R2,
emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/caltrans/ccop_active_lance

CCOP source columns (per c1): project_id, project_title, county, license_class,
advertise_date, bid_date, status — all VARCHAR.

BTREE indexes (per audit plan §c4):
  project_id                       — canonical key (one row per active bid)
  county                           — for geographic filters
  license_class_normalized         — pipe-joined sorted classes (e.g. "A|C-12")
                                     for joining to CSLB licensees
  bid_date_typed                   — TRY_CAST DATE for time-based filters

Per CLAUDE.md:
  - DuckDB UDF registration uses STRING type names per HEAD 6df6d840 (string args, not the typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - BTREE on typed sibling columns (TRY_CAST DATE per L29/L49)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_caltrans_ccop_lance_emit.py [--apply]
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB = f"s3://{R2_BUCKET}/caltrans/ccop-active/snapshot=*/data.parquet"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/caltrans/ccop_active_lance"

# caltrans namespace per directive §"Audit plan" (matches existing
# polaris-warehouse/caltrans/ datasets: awards_lance, awards_pets_lance)
POLARIS_NAMESPACE = "caltrans"

TMP_DIR = "/tmp/lance"


def _normalize_license_class(raw: str | None) -> str | None:
    """Normalize a compound license-class string for BTREE joining.

    Input shapes observed in CCOP HTML:
        "A"            → "A"
        "A, C-12"      → "A|C-12"
        "A,C-12"       → "A|C-12"
        "A, B, C-12"   → "A|B|C-12"
        ""             → None
        None           → None

    Pipe-joined + sorted gives a deterministic string for equality joins to
    CSLB licensees (which carry their own license-class field per CLAUDE.md L54
    Lance 1.5.x definition-buffer cap precludes LIST<VARCHAR>).
    """
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    parts = [p.strip().upper() for p in cleaned.split(",") if p.strip()]
    if not parts:
        return None
    return "|".join(sorted(parts))


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
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL aws; LOAD aws;")
    con.execute(
        f"""
        CREATE OR REPLACE SECRET r2_secret (
            TYPE s3,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ENDPOINT '{os.environ["R2_ENDPOINT"].replace("https://", "")}',
            REGION 'us-east-1',
            URL_STYLE 'path'
        )
        """
    )
    # DuckDB UDF registration: STRING type names per HEAD 6df6d840 — CLAUDE.md §"DuckDB UDF"
    con.create_function(
        "py_normalize_license",
        _normalize_license_class,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register Lance dataset as a Polaris Generic Table."""
    script = (
        Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    )
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
    except subprocess.CalledProcessError as exc:
        logger.warning("Polaris registration failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def emit() -> None:
    """Emit CCOP Lance dataset from latest R2 snapshot."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info("CCOP: %d source rows at %s", row_count, PARQUET_GLOB)
    if row_count == 0:
        raise RuntimeError("CCOP: no rows in Parquet glob — aborting (run ingest first)")

    storage_options = _storage_options()

    reader = con.execute(
        f"""
        SELECT
            project_id,
            project_title,
            county,
            license_class,
            py_normalize_license(license_class) AS license_class_normalized,
            advertise_date,
            TRY_CAST(advertise_date AS DATE)    AS advertise_date_typed,
            bid_date,
            TRY_CAST(bid_date AS DATE)          AS bid_date_typed,
            status
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("caltrans_ccop_active_lance"):
        logger.info("writing CCOP Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("CCOP Lance written: %d rows (version %s)", lance_rows, ds.version)

        # BTREE indexes per audit plan §c4
        for col in (
            "project_id",
            "county",
            "license_class_normalized",
            "bid_date_typed",
        ):
            logger.info("CCOP: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("CCOP: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("CCOP: optimize failed (non-fatal): %s", exc)

    logger.info("CCOP: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "ccop_active_lance",
        "caltrans.ccop_active_lance — Caltrans active highway/bridge bid solicitations from "
        "ccop.dot.ca.gov/allProjects, daily snapshot, ~96-100 rows per snapshot. "
        "BTREE on project_id, county, license_class_normalized, bid_date_typed.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Caltrans CCOP active → Lance emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        con = _duckdb_conn()
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
        ).fetchone()[0]
        logger.info("DRY-RUN CCOP: %d rows in Parquet glob (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
