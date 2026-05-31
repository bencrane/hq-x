"""ClinicalTrials.gov device studies -> Lance emit (Pattern A).

Reads the ZSTD Parquet snapshot written by run_clinicaltrials_device_studies_to_r2.py
from R2 and emits one Lance dataset at:
  s3://dex-raw-landing-zone/polaris-warehouse/clinicaltrials/device_studies_lance

DEVIATION #2 from the runbook: the runbook's c4 globs all snapshot=* partitions.
CT.gov device_studies is a current-state entity registry (one row per nct_id),
so this emit reads ONLY the latest snapshot= partition into Lance
(mode='overwrite'). The max snapshot= partition is resolved via a boto3 listing,
the single file is downloaded to /tmp and read locally with read_parquet (L58 —
direct DuckDB R2 httpfs reads can 404 on single-file keys; glob reads inject a
synthetic Hive partition column). Historical snapshots remain in R2, retained on
purpose for the future "what moved" diff cycle — NOT consumed here.

Source columns (per c1, all VARCHAR): nct_id, study_title, overall_status,
why_stopped, study_type, phases, lead_sponsor_name, lead_sponsor_class,
collaborator_names, collaborator_classes, device_intervention_names,
device_intervention_types, enrollment_count, start_date, completion_date,
first_posted_date, last_update_posted_date, results_first_posted_date,
conditions, location_states, location_countries, has_results, raw_json.

BTREE indexes:
  nct_id                       — canonical PK (one row per study)
  lead_sponsor_name_normalized — normalized sponsor name; pre-stages the future
                                 cross-source company-domain bridge join key
                                 (the bridge itself is a separate cycle).

Per CLAUDE.md:
  - DuckDB UDF registration uses string type args, not the typing module
  - lance_commit_lock wrapper around lance.write_dataset
  - compact_files() + cleanup_old_versions(7d)
  - Polaris registration via init_polaris_lance_generic

Usage:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        uv run python scripts/run_clinicaltrials_device_studies_lance_emit.py [--apply]
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
R2_PREFIX = "clinicaltrials-gov/device-studies"

LANCE_URI = f"s3://{R2_BUCKET}/polaris-warehouse/clinicaltrials/device_studies_lance"

POLARIS_NAMESPACE = "clinicaltrials"

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


def _resolve_latest_snapshot_key() -> str:
    """List the R2 prefix and return the data.parquet key of the MAX snapshot=.

    Deviation #2: current-state entity registry — only the latest snapshot
    partition is emitted into Lance. snapshot= partitions sort lexicographically
    by their YYYY-MM-DD value, so the max string is the most-recent snapshot.
    """
    s3 = _r2_client()
    paginator = s3.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=R2_BUCKET, Prefix=f"{R2_PREFIX}/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/data.parquet") and "snapshot=" in key:
                keys.append(key)
    if not keys:
        raise RuntimeError(
            f"no snapshot=*/data.parquet under s3://{R2_BUCKET}/{R2_PREFIX}/ "
            "— run the c1 R2 ingest first"
        )
    latest = max(keys)
    logger.info("latest snapshot key: %s (of %d snapshot partitions)", latest, len(keys))
    return latest


def _download_latest_snapshot() -> str:
    """Download the latest snapshot data.parquet to /tmp; return the local path.

    Per L58: download single-file keys via boto3 and read_parquet locally —
    direct DuckDB R2 httpfs reads can 404 on single-file keys.
    """
    key = _resolve_latest_snapshot_key()
    # snapshot=YYYY-MM-DD is the second-to-last path segment.
    snapshot_segment = key.split("/")[-2]
    local = f"/tmp/clinicaltrials-device-studies-{snapshot_segment}.parquet"
    if not Path(local).exists():
        logger.info("downloading s3://%s/%s -> %s", R2_BUCKET, key, local)
        _r2_client().download_file(R2_BUCKET, key, local)
    else:
        logger.info("reusing cached local snapshot: %s", local)
    return local


def _duckdb_conn():
    import duckdb

    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET threads=4")
    con.execute("SET memory_limit='4GB'")
    con.execute(f"SET temp_directory='{TMP_DIR}'")
    con.execute("SET preserve_insertion_order=false")
    # DuckDB UDF registration: string type args per L34 (not the typing module).
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
    """Emit the CT.gov device-studies Lance dataset from the latest R2 snapshot."""
    import lance

    os.environ.setdefault("LANCE_BYPASS_SPILLING", "true")
    os.environ["TMPDIR"] = TMP_DIR
    Path(TMP_DIR).mkdir(parents=True, exist_ok=True)

    local_parquet = _download_latest_snapshot()

    con = _duckdb_conn()
    row_count = con.execute(
        f"SELECT count(*) FROM read_parquet('{local_parquet}')"
    ).fetchone()[0]
    logger.info("CT.gov device studies: %d source rows in %s", row_count, local_parquet)
    if row_count == 0:
        raise RuntimeError("CT.gov device studies: latest snapshot has 0 rows — aborting")

    storage_options = _storage_options()

    # All source columns are VARCHAR (L9); lead_sponsor_name_normalized is the
    # only derived column — normalized via the shared py_normalize_entity UDF.
    reader = con.execute(
        f"""
        SELECT
            nct_id,
            study_title,
            overall_status,
            why_stopped,
            study_type,
            phases,
            lead_sponsor_name,
            py_normalize_entity(lead_sponsor_name) AS lead_sponsor_name_normalized,
            lead_sponsor_class,
            collaborator_names,
            collaborator_classes,
            device_intervention_names,
            device_intervention_types,
            enrollment_count,
            start_date,
            completion_date,
            first_posted_date,
            last_update_posted_date,
            results_first_posted_date,
            conditions,
            location_states,
            location_countries,
            has_results,
            raw_json
        FROM read_parquet('{local_parquet}')
        """
    ).fetch_record_batch(rows_per_batch=10_000)

    with lance_commit_lock("clinicaltrials_device_studies_lance"):
        logger.info("writing CT.gov device-studies Lance dataset to %s ...", LANCE_URI)
        ds = lance.write_dataset(
            reader,
            LANCE_URI,
            mode="overwrite",
            storage_options=storage_options,
            max_rows_per_file=5000,
        )
        lance_rows = ds.count_rows()
        logger.info("CT.gov device-studies Lance written: %d rows (version %s)", lance_rows, ds.version)

        for col in ("nct_id", "lead_sponsor_name_normalized"):
            logger.info("CT.gov device studies: creating BTREE on %s ...", col)
            ds.create_scalar_index(col, index_type="BTREE", replace=True)
            logger.info("CT.gov device studies: BTREE on %s OK", col)

        try:
            ds.optimize.compact_files()
            ds.cleanup_old_versions(older_than=timedelta(days=7))
        except Exception as exc:
            logger.warning("CT.gov device studies: optimize failed (non-fatal): %s", exc)

    logger.info("CT.gov device studies: emit complete — lance_rows=%d uri=%s", lance_rows, LANCE_URI)

    _register_polaris(
        "device_studies_lance",
        "clinicaltrials.device_studies_lance — complete ClinicalTrials.gov "
        "device-intervention study corpus (all statuses) from the public CT.gov "
        "API v2 (query.intr=device), weekly Modal refresh, latest-snapshot "
        "emit. BTREE on nct_id, lead_sponsor_name_normalized.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ClinicalTrials.gov device studies -> Lance emit"
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Actually write the Lance dataset (default: dry-run row count only)",
    )
    args = parser.parse_args()

    if not args.apply:
        local_parquet = _download_latest_snapshot()
        con = _duckdb_conn()
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{local_parquet}')"
        ).fetchone()[0]
        logger.info("DRY-RUN CT.gov device studies: %d rows in latest snapshot (pass --apply to emit)", n)
        return 0

    emit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
