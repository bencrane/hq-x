"""WARN Act layoff notices (Big Local News) → Lance emit (Pattern A).

Reads the latest ZSTD Parquet snapshot written by run_warn_notices_to_r2.py
from R2 and emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/warn/notices_lance

The Big Local News integrated.csv is a cumulative integrated dataset — each
daily snapshot is a full copy of the entire history. So this emit reads ONLY
the latest snapshot partition (not the snapshot=* glob) and overwrites the
Lance dataset.

Source columns (15, all VARCHAR): hash_id, first_inserted_date, notice_date,
effective_date, postal_code, company, location, jobs, is_closure, is_temporary,
is_superseded, is_amendment, likely_ancestor, estimated_amendments,
last_updated_date.

The emitted Lance dataset adds typed siblings + a normalized company column:
  notice_date_typed, effective_date_typed (DATE)
  first_inserted_date_typed, last_updated_date_typed (TIMESTAMP)
  jobs_typed (BIGINT), estimated_amendments_typed (INTEGER)
  company_normalized — scripts._lib.entity_name_normalize.normalize_entity_name,
    precomputed so the future warn × SoS/USAspending/SBA name+state bridge
    (Pattern B, legal_name_state_exact) joins on a ready key.

BTREE indexes: hash_id (canonical PK), company_normalized, postal_code,
notice_date_typed.

Per CLAUDE.md / lessons:
  - L58: single-file httpfs reads can 404 — boto3-download the latest snapshot
    to /tmp, then read_parquet on the local file.
  - DuckDB UDF registration uses STRING type names per L34 (string args, not
    the typing module).
  - lance_commit_lock wrapper around lance.write_dataset.
  - Polaris registration via init_polaris_lance_generic.

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_warn_notices_lance_emit.py [--apply]
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

from scripts._lib.entity_name_normalize import normalize_entity_name
from scripts._lib.lance_commit_lock import lance_commit_lock

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

# ── load-bearing constants (verify harness greps for these) ─────────────────

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "warn/notices"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/warn/notices_lance"

# warn namespace — the national WARN feed, not a per-state namespace.
POLARIS_NAMESPACE = "warn"

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


def _r2_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
    )


def _latest_snapshot_parquet(local_dir: str) -> str:
    """Locate the latest warn/notices snapshot in R2, download it to local_dir.

    integrated.csv is cumulative, so the latest snapshot is the complete current
    dataset. Per L58, single-file httpfs reads can 404 — download via boto3 and
    read the file locally instead.
    """
    s3 = _r2_client()
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(
        Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX}/snapshot="
    ):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("data.parquet"):
                keys.append(obj["Key"])
    if not keys:
        raise RuntimeError(
            f"no snapshots under s3://{R2_BUCKET}/{R2_PREFIX}/ — run the R2 ingest first"
        )
    # snapshot=YYYY-MM-DD sorts lexicographically by date — max() is the latest.
    latest_key = max(keys)
    local_path = os.path.join(local_dir, "warn-notices-latest.parquet")
    logger.info("latest snapshot: %s → %s", latest_key, local_path)
    s3.download_file(R2_BUCKET, latest_key, local_path)
    return local_path


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    # DuckDB UDF registration: STRING type names per L34 (string args, not the typing module)
    con.create_function(
        "py_normalize_entity",
        normalize_entity_name,
        ["VARCHAR"],
        "VARCHAR",
        null_handling="special",
    )
    return con


def _register_polaris(table_name: str, doc: str) -> None:
    """Register the Lance dataset as a Polaris Generic Table."""
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
    except subprocess.CalledProcessError as exc:
        logger.warning("Polaris registration failed (non-fatal): %s", exc)
    except Exception as exc:
        logger.warning("Polaris registration error (non-fatal): %s", exc)


def emit() -> None:
    """Emit the WARN notices Lance dataset from the latest R2 snapshot."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    local_parquet = _latest_snapshot_parquet(TMP_DIR)

    con = _duckdb_conn()
    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    logger.info("WARN notices: %d source rows", row_count)
    if row_count == 0:
        raise RuntimeError("WARN notices: latest snapshot has 0 rows — aborting")

    reader = con.execute(
        f"""
        SELECT
            hash_id,
            first_inserted_date,
            TRY_CAST(first_inserted_date AS TIMESTAMP) AS first_inserted_date_typed,
            notice_date,
            TRY_CAST(notice_date AS DATE)              AS notice_date_typed,
            effective_date,
            TRY_CAST(effective_date AS DATE)           AS effective_date_typed,
            postal_code,
            company,
            py_normalize_entity(company)               AS company_normalized,
            location,
            jobs,
            TRY_CAST(jobs AS BIGINT)                   AS jobs_typed,
            is_closure,
            is_temporary,
            is_superseded,
            is_amendment,
            likely_ancestor,
            estimated_amendments,
            TRY_CAST(estimated_amendments AS INTEGER)  AS estimated_amendments_typed,
            last_updated_date,
            TRY_CAST(last_updated_date AS TIMESTAMP)   AS last_updated_date_typed
        FROM read_parquet('{local_parquet}')
        """
    ).fetch_record_batch(rows_per_batch=20_000)

    with lance_commit_lock("warn_notices_lance"):
        logger.info("writing WARN notices Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=_storage_options(),
            max_rows_per_file=20_000,
        )
        lance_rows = ds.count_rows()
        logger.info(
            "WARN notices Lance written: %d rows (version %s)", lance_rows, ds.version
        )

        # BTREE indexes — canonical PK + bridge/filter keys.
        for col in ("hash_id", "company_normalized", "postal_code", "notice_date_typed"):
            logger.info("WARN notices: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("WARN notices: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("WARN notices: optimize failed (non-fatal): %s", exc)

    logger.info(
        "WARN notices: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI
    )

    _register_polaris(
        "notices_lance",
        "warn.notices_lance — WARN Act layoff + closure notices, all states "
        "(40 + DC), consolidated daily from Big Local News warn-transformer "
        "integrated.csv. BTREE on hash_id, company_normalized, postal_code, "
        "notice_date_typed.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="WARN Act notices → Lance emit"
    )
    parser.add_argument(
        "--apply", action="store_true", default=False,
        help="Actually write the Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
        local_parquet = _latest_snapshot_parquet(TMP_DIR)
        con = _duckdb_conn()
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{local_parquet}')"
        ).fetchone()[0]
        logger.info(
            "DRY-RUN WARN notices: %d rows in latest snapshot (pass --apply to emit)", n
        )
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
