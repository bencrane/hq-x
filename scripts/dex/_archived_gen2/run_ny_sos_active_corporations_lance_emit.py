"""NY State Active Corporations (DoS Beginning 1800) → Lance emit (Pattern A).

Cadence — **Operator-Only Bulk Run (Quarterly Batch).** State SoS pipelines
(CA / FL / NY / CO) are retired from automated schedules per the 2026-05-25
operational policy shift. The companion Modal app
``modal/ny_sos_active_corporations_app.py`` previously fired this script
daily; that schedule is now removed and the deployment is permanently
stopped. Trigger manually, point-in-time. See
``apps/data-engine-x/modal/INDEX.md`` §"State SoS pipelines".

Reads ZSTD Parquet snapshots written by run_ny_sos_active_corporations_to_r2.py
from R2, emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/sos/ny_active_corporations_lance

Source columns (Socrata n9v6-gdp6, all VARCHAR in source Parquet):
  dos_id, current_entity_name, initial_dos_filing_date, county,
  jurisdiction, entity_type, dos_process_name, dos_process_address_1,
  dos_process_address_2, dos_process_city, dos_process_state, dos_process_zip,
  ceo_name, ceo_address_1, ceo_address_2, ceo_city, ceo_state, ceo_zip,
  registered_agent_name, registered_agent_address_1, registered_agent_address_2,
  registered_agent_city, registered_agent_state, registered_agent_zip

Typed sibling columns (TRY_CAST per L29):
  initial_dos_filing_date_typed  — TRY_CAST(initial_dos_filing_date AS DATE)

Normalized column:
  entity_name_normalized  — py_normalize_entity(current_entity_name)

BTREE indexes (per audit plan + validator p1 column-name fix):
  dos_id                         — canonical PK (validator confirmed)
  entity_name_normalized         — entity-name normalized corporate name
  initial_dos_filing_date_typed  — typed sibling (validator p1: ACTUAL column is
                                   initial_dos_filing_date, not initial_filing_date)

Per CLAUDE.md:
  - DuckDB UDF registration uses string type names per L34
    (string-typed args, not the typing module — do NOT reference the typing module)
  - lance_commit_lock wrapper around lance.write_dataset
  - LANCE_BYPASS_SPILLING=true env var before create_scalar_index
  - Polaris registration via init_polaris_lance_generic
  - NO LIST<VARCHAR> columns — flat schema with pipe-joined VARCHAR for
    any multi-value field (not applicable here; schema is flat)

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_ny_sos_active_corporations_lance_emit.py [--apply]
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
from scripts._lib.entity_name_normalize import normalize_entity_name

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"

PARQUET_GLOB = (
    f"s3://{R2_BUCKET}/sos-ny/active-corporations/snapshot=*/data.parquet"
)

LANCE_URI = (
    f"s3://{R2_BUCKET}/polaris-warehouse/sos/ny_active_corporations_lance"
)

# sos namespace per state-procurement runbook §"Namespacing conventions"
POLARIS_NAMESPACE = "sos"
POLARIS_TABLE = "ny_active_corporations_lance"

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
    con.execute("SET memory_limit='6GB'")
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
    # DuckDB UDF registration: string-typed args per L34
    # Using ["VARCHAR"], "VARCHAR" — string names, not the typing module
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register Lance dataset as a Polaris Generic Table.

    Silent-fail on absent POLARIS_PUBLIC_URL per state-procurement-ingest-runbook
    §"Gotchas" item 3.
    """
    script = (
        Path(__file__).resolve().parent / "init_polaris_lance_generic.py"
    )
    if not script.exists():
        logger.warning(
            "init_polaris_lance_generic.py not found; skipping Polaris registration"
        )
        return
    cmd = [
        sys.executable, str(script),
        "--namespace", POLARIS_NAMESPACE,
        "--table", table_name,
        "--doc", doc,
    ]
    logger.info("registering Polaris: %s.%s", POLARIS_NAMESPACE, table_name)
    try:
        subprocess.run(cmd, check=True, timeout=60)
        logger.info(
            "Polaris registration OK: %s.%s", POLARIS_NAMESPACE, table_name
        )
    except subprocess.CalledProcessError as exc:
        logger.warning("Polaris registration failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def emit() -> None:
    """Emit NY DoS Active Corps Lance dataset from latest R2 snapshot."""
    import lance

    # LANCE_BYPASS_SPILLING=true per directive, before create_scalar_index
    os.environ["LANCE_BYPASS_SPILLING"] = "true"
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    con = _duckdb_conn()

    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{PARQUET_GLOB}')"
    ).fetchone()[0]
    logger.info(
        "NY DoS Active Corps: %d source rows at %s", row_count, PARQUET_GLOB
    )
    if row_count == 0:
        raise RuntimeError(
            "NY DoS Active Corps: no rows in Parquet glob — "
            "aborting (run ingest first)"
        )

    storage_options = _storage_options()

    # Build the query: all source columns + typed sibling (initial_dos_filing_date_typed)
    # + entity_name_normalized. Note: actual filing-date column is initial_dos_filing_date
    # (validator p1 confirms this — not initial_filing_date).
    # initial_dos_filing_date format is MM/DD/YYYY in the DATA.NY.GOV bulk-CSV
    # endpoint (Socrata n9v6-gdp6). DuckDB's TRY_CAST AS DATE only accepts ISO-8601,
    # so the prior cast was producing 100% NULL across all rows. Use try_strptime
    # with the umbrella fallback array shared with apps/data-engine-x/scripts/
    # build_sos_state_entity_spines_lance.py (see DATA-FACTORY-ARCHITECTURE-PATTERNS.md
    # §"Pattern A" + that script's `DATE_FORMAT_FALLBACKS`).
    reader = con.execute(
        f"""
        SELECT
            dos_id,
            current_entity_name,
            py_normalize_entity(current_entity_name)                          AS entity_name_normalized,
            initial_dos_filing_date,
            try_strptime(initial_dos_filing_date,
                         ['%Y-%m-%d', '%m/%d/%Y', '%Y%m%d', '%m%d%Y'])::DATE  AS initial_dos_filing_date_typed,
            county,
            jurisdiction,
            entity_type,
            dos_process_name,
            dos_process_address_1,
            dos_process_address_2,
            dos_process_city,
            dos_process_state,
            dos_process_zip,
            ceo_name,
            ceo_address_1,
            ceo_address_2,
            ceo_city,
            ceo_state,
            ceo_zip,
            registered_agent_name,
            registered_agent_address_1,
            registered_agent_address_2,
            registered_agent_city,
            registered_agent_state,
            registered_agent_zip,
            location_name,
            location_address_1,
            location_address_2,
            location_city,
            location_state,
            location_zip
        FROM read_parquet('{PARQUET_GLOB}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("ny_active_corporations_lance"):
        logger.info(
            "writing NY DoS Active Corps Lance dataset to %s ...", LANCE_URI
        )
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info(
            "NY DoS Active Corps Lance written: %d rows (version %s)",
            lance_rows,
            ds.version,
        )

        # BTREE indexes per audit plan + validator p1 column-name fix:
        #   dos_id, entity_name_normalized, initial_dos_filing_date_typed
        for col in (
            "dos_id",
            "entity_name_normalized",
            "initial_dos_filing_date_typed",
        ):
            logger.info("NY DoS Active Corps: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("NY DoS Active Corps: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning(
                "NY DoS Active Corps: optimize failed (non-fatal): %s", exc
            )

    logger.info(
        "NY DoS Active Corps: emit complete — lance_rows=%d uri=%s",
        lance_rows,
        LANCE_URI,
    )

    _register_polaris(
        POLARIS_TABLE,
        f"{POLARIS_NAMESPACE}.{POLARIS_TABLE} — "
        "NY State Active Corporations (DoS Beginning 1800), ~4.2M rows, "
        "daily snapshot refresh. BTREE on dos_id + entity_name_normalized + "
        "initial_dos_filing_date_typed. "
        "Source: https://data.ny.gov/api/views/n9v6-gdp6.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="NY DoS Active Corps → Lance emit"
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
        logger.info(
            "DRY-RUN NY DoS Active Corps: %d rows in Parquet glob "
            "(pass --apply to emit)",
            n,
        )
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
