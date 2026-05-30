#!/usr/bin/env python3
"""s2 — Stream CA UCC CSVs to Parquet on R2.

Downloads raw.zip from R2, unzips to /tmp, then streams each CSV via DuckDB
read_csv_auto (constraint P2 — Filings.csv is 1.1 GB; never load all at once)
and writes a ZSTD-compressed Parquet per CSV.

Output layout in dex-raw-landing-zone:
    ucc-ca/master/snapshot=2026-05-01/parsed/
        filings.parquet
        debtors.parquet
        secured_parties.parquet
        filing_amendments.parquet

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/parse_ucc_ca_csvs_to_parquet.py \\
            --apply [--no-upload]
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import tempfile
import zipfile
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

R2_BUCKET = "dex-raw-landing-zone"
R2_RAW_KEY = "ucc-ca/master/snapshot=2026-05-01/raw.zip"
R2_PARSED_PREFIX = "ucc-ca/master/snapshot=2026-05-01/parsed"

# CSV filename → output parquet slug
CSV_MAP = {
    "Filings.csv": "filings",
    "Debtors.csv": "debtors",
    "SecuredParties.csv": "secured_parties",
    "FilingAmendments.csv": "filing_amendments",
}


def _r2_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )


def _connect_duckdb_to_r2():
    import duckdb

    ep = os.environ["R2_ENDPOINT"]
    account_id = ep.split("//")[-1].split(".")[0]
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(
        f"""
        CREATE SECRET (
            TYPE r2,
            KEY_ID '{os.environ["R2_ACCESS_KEY_ID"]}',
            SECRET '{os.environ["R2_SECRET_ACCESS_KEY"]}',
            ACCOUNT_ID '{account_id}'
        )
        """
    )
    return con


def _parse_and_write_parquet(csv_path: Path, parquet_path: Path) -> int:
    """Stream csv_path via DuckDB → write Parquet at parquet_path. Returns row count."""
    import duckdb

    con = duckdb.connect()
    # DuckDB read_csv_auto with streaming; pipe-delimited, double-quoted
    # This is the streaming pattern for large CSVs (constraint P2).
    row_count = con.execute(
        f"""
        COPY (
            SELECT * FROM read_csv_auto(
                '{csv_path}',
                delim='|',
                quote='"',
                header=true,
                ignore_errors=true
            )
        )
        TO '{parquet_path}'
        (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 100000)
        """
    ).fetchone()[0]
    return row_count


def main() -> int:
    ap = argparse.ArgumentParser(description="Parse CA UCC CSVs to Parquet (s2)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-upload", action="store_true", help="write locally only")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: env var %s not set", var)
            return 64

    if args.dry_run:
        LOG.info("DRY RUN — exiting without processing")
        return 0

    s3 = _r2_client()

    with tempfile.TemporaryDirectory(prefix="ucc_ca_parse_") as tmpdir:
        tmp = Path(tmpdir)

        # Download raw.zip
        zip_path = tmp / "raw.zip"
        LOG.info("Downloading s3://%s/%s ...", R2_BUCKET, R2_RAW_KEY)
        s3.download_file(R2_BUCKET, R2_RAW_KEY, str(zip_path))
        LOG.info("Downloaded %.1f MB", zip_path.stat().st_size / 1e6)

        # Unzip
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        LOG.info("Unzipping ...")
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

        # List unzipped files
        all_files = list(extract_dir.rglob("*.csv"))
        LOG.info("Unzipped CSVs: %s", [f.name for f in all_files])

        # Build a name→path map (case-insensitive match)
        csv_lookup: dict[str, Path] = {}
        for f in all_files:
            csv_lookup[f.name] = f
            csv_lookup[f.name.lower()] = f

        results = {}
        for csv_name, parquet_slug in CSV_MAP.items():
            # Try exact name then lowercase
            csv_file = csv_lookup.get(csv_name) or csv_lookup.get(csv_name.lower())
            if csv_file is None:
                LOG.error("FAIL: %s not found in zip. Available: %s", csv_name, list(csv_lookup))
                return 1

            parquet_path = tmp / f"{parquet_slug}.parquet"
            LOG.info(
                "Parsing %s (%.1f MB) → %s ...",
                csv_file.name,
                csv_file.stat().st_size / 1e6,
                parquet_path.name,
            )
            row_count = _parse_and_write_parquet(csv_file, parquet_path)
            LOG.info("  wrote %d rows, %.1f MB parquet", row_count, parquet_path.stat().st_size / 1e6)
            results[parquet_slug] = {"rows": row_count, "local_path": parquet_path}

        if args.no_upload:
            LOG.info("--no-upload set; skipping R2 upload")
            for slug, info in results.items():
                LOG.info("  %s.parquet: %d rows at %s", slug, info["rows"], info["local_path"])
            return 0

        # Upload parsed parquets to R2
        for parquet_slug, info in results.items():
            r2_key = f"{R2_PARSED_PREFIX}/{parquet_slug}.parquet"
            LOG.info("Uploading %s → s3://%s/%s ...", parquet_slug, R2_BUCKET, r2_key)
            s3.upload_file(
                str(info["local_path"]),
                R2_BUCKET,
                r2_key,
                Config=boto3.s3.transfer.TransferConfig(
                    multipart_threshold=50 * 1024 * 1024,
                    multipart_chunksize=50 * 1024 * 1024,
                ),
            )
            LOG.info("  OK: %d rows uploaded", info["rows"])

    LOG.info("s2 complete. %d parquets written.", len(results))
    return 0


import boto3  # noqa: E402 — deferred to avoid import at module-level when dry-running

if __name__ == "__main__":
    raise SystemExit(main())
