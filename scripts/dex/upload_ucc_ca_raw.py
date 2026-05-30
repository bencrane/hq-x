#!/usr/bin/env python3
"""s1 — Upload raw CA UCC zip to R2.

Uploads the operator's local zip:
  /Users/benjamincrane/DataRequest0x07222C257E6AA631FE2C8A25A269FA8C5F2F0B7B.zip

to:
  s3://dex-raw-landing-zone/ucc-ca/master/snapshot=2026-05-01/raw.zip

Usage:
    doppler run --project hq-all --config prd -- \\
        python3 apps/data-engine-x/scripts/upload_ucc_ca_raw.py --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

LOCAL_ZIP = Path(
    "/Users/benjamincrane/"
    "DataRequest0x07222C257E6AA631FE2C8A25A269FA8C5F2F0B7B.zip"
)
R2_BUCKET = "dex-raw-landing-zone"
R2_KEY = "ucc-ca/master/snapshot=2026-05-01/raw.zip"


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload CA UCC raw zip to R2 (s1)")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--apply", action="store_true")
    grp.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    for var in ("R2_ENDPOINT", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY"):
        if not os.environ.get(var):
            LOG.error("FAIL: env var %s not set", var)
            return 64

    if not LOCAL_ZIP.exists():
        LOG.error("FAIL: local zip not found at %s", LOCAL_ZIP)
        return 1

    size = LOCAL_ZIP.stat().st_size
    LOG.info("Local zip: %s (%.1f MB)", LOCAL_ZIP, size / 1e6)
    LOG.info("Target:    s3://%s/%s", R2_BUCKET, R2_KEY)

    if args.dry_run:
        LOG.info("DRY RUN — exiting without uploading")
        return 0

    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 3, "mode": "standard"}),
    )

    LOG.info("Uploading via multipart ...")
    s3.upload_file(
        str(LOCAL_ZIP),
        R2_BUCKET,
        R2_KEY,
        ExtraArgs={"ContentType": "application/zip"},
        Config=boto3.s3.transfer.TransferConfig(
            multipart_threshold=50 * 1024 * 1024,
            multipart_chunksize=50 * 1024 * 1024,
        ),
    )
    head = s3.head_object(Bucket=R2_BUCKET, Key=R2_KEY)
    LOG.info("OK: uploaded %d bytes to s3://%s/%s", head["ContentLength"], R2_BUCKET, R2_KEY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
