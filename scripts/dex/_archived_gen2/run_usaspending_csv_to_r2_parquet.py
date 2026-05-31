#!/usr/bin/env python3
"""Stream USAspending Award Data Archive ZIPs (FY full snapshots) into the
``usaspending.contracts`` Iceberg table.

After this script runs:
  - The streamed Parquet sits at the legacy R2 key
    ``usaspending/contracts/year={YYYY}/snapshot={YYYY-MM-DD}/data.parquet``
    (preserved for backward-compat with any consumer still reading the
    directory-convention path)
  - The same Parquet is registered into the ``usaspending.contracts``
    Iceberg table via PyIceberg ``add_files()``, producing a new Iceberg
    snapshot. Downstream audience-spec evaluation reads via Iceberg.

Streaming model (no large in-memory tables, no full-CSV-on-disk extracts):
  ZIP member -> pyarrow.csv streaming reader -> arrow batches ->
  ParquetWriter (local temp) -> boto3 upload to R2 -> Iceberg add_files ->
  delete temp.

Snapshot date defaults to the date embedded in the ZIP filename (USAspending
publishes monthly Full archives with names like `FY2024_All_Contracts_Full_20260506.zip`).

Run:
  doppler run --project hq-all --config prd -- \
    uv run python scripts/run_usaspending_csv_to_r2_parquet.py \
      --zip /Users/benjamincrane/FY2024_All_Contracts_Full_20260506.zip \
      --year 2024
  # Snapshot date auto-derived as 2026-05-06 from filename.
  # Override with: --snapshot-date 2026-05-06
  # Skip the Iceberg registration step:  --no-iceberg-register

L42 hygiene: ContentType only, no Content-Encoding header. ZSTD compression
lives internally at the Parquet column-chunk level via
`pq.write_table(..., compression='zstd')`.
"""
from __future__ import annotations

import argparse
import csv as stdlib_csv
import io
import logging
import os
import re
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts._lib.iceberg_catalog import get_catalog  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX = "usaspending/contracts"
ICEBERG_TABLE_ID = ("usaspending", "contracts")
BATCH_SIZE_BYTES = 64 * 1024 * 1024  # 64 MB CSV read blocks
LOG = logging.getLogger("usa-csv-to-r2")


def make_s3_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def stream_zip_to_parquet(zip_path: Path, year: int, out_parquet: Path) -> dict:
    """Stream every CSV inside `zip_path` into one Parquet file at `out_parquet`.

    All columns coerced to string (matches RW source declared types and
    USAspending's text-only CSV shape; avoids pyarrow type-inference choking
    on later blocks containing 'NONE' / mixed-precision values).
    """
    t0 = time.monotonic()
    rows_total = 0
    files_written = 0
    per_file: list[dict] = []
    schema: pa.Schema | None = None
    writer: pq.ParquetWriter | None = None

    with zipfile.ZipFile(zip_path) as zf:
        members = sorted([m for m in zf.namelist() if m.endswith(".csv")])
        LOG.info("zip=%s members=%d year=%d", zip_path.name, len(members), year)

        for idx, member in enumerate(members, start=1):
            t_member = time.monotonic()
            member_rows = 0
            # Peek the header first so we can declare every column as string up-front
            # (avoids pyarrow inferring a non-string type from the first 64MB block
            # then crashing on a later block whose value can't be coerced).
            with zf.open(member, "r") as peek:
                header_line = peek.readline().decode("utf-8")
            header_cols = next(stdlib_csv.reader(io.StringIO(header_line)))
            column_types = {name: pa.string() for name in header_cols}

            with zf.open(member, "r") as raw:
                read_opts = pacsv.ReadOptions(
                    block_size=BATCH_SIZE_BYTES,
                    use_threads=True,
                )
                parse_opts = pacsv.ParseOptions(
                    delimiter=",",
                    quote_char='"',
                    escape_char=False,
                    newlines_in_values=True,
                )
                conv_opts = pacsv.ConvertOptions(
                    column_types=column_types,
                    strings_can_be_null=True,
                    null_values=[""],
                )
                reader = pacsv.open_csv(
                    raw,
                    read_options=read_opts,
                    parse_options=parse_opts,
                    convert_options=conv_opts,
                )
                for batch in reader:
                    arrays = []
                    for col in batch.schema.names:
                        arr = batch.column(col)
                        if arr.type != pa.string():
                            arr = arr.cast(pa.string(), safe=False)
                        arrays.append(arr)
                    coerced = pa.RecordBatch.from_arrays(arrays, names=batch.schema.names)
                    if schema is None:
                        schema = pa.schema([(n, pa.string()) for n in coerced.schema.names])
                        writer = pq.ParquetWriter(
                            out_parquet,
                            schema,
                            compression="zstd",
                            compression_level=3,
                        )
                    else:
                        if coerced.schema.names != schema.names:
                            cols_have = {n: coerced.column(n) for n in coerced.schema.names}
                            new_arrays = []
                            for n in schema.names:
                                if n in cols_have:
                                    new_arrays.append(cols_have[n])
                                else:
                                    new_arrays.append(
                                        pa.array([None] * coerced.num_rows, type=pa.string())
                                    )
                            coerced = pa.RecordBatch.from_arrays(new_arrays, names=schema.names)
                    writer.write_batch(coerced)
                    member_rows += coerced.num_rows
                rows_total += member_rows
                files_written += 1
            elapsed = time.monotonic() - t_member
            rate = member_rows / elapsed if elapsed > 0 else 0
            LOG.info(
                "  [%d/%d] %s  rows=%d  elapsed=%.1fs  rate=%.0f rows/s",
                idx, len(members), member, member_rows, elapsed, rate,
            )
            per_file.append({"name": member, "rows": member_rows, "elapsed_s": elapsed})

    if writer:
        writer.close()
    total_elapsed = time.monotonic() - t0
    return {
        "year": year,
        "zip": str(zip_path),
        "out_parquet": str(out_parquet),
        "files_in_zip": len(per_file),
        "files_written": files_written,
        "rows_total": rows_total,
        "elapsed_s": total_elapsed,
        "per_file": per_file,
        "schema_cols": len(schema.names) if schema else 0,
        "schema_names": list(schema.names) if schema else [],
    }


def upload_parquet(local_path: Path, year: int, snapshot_date: str) -> dict:
    s3 = make_s3_client()
    key = f"{R2_PREFIX}/year={year}/snapshot={snapshot_date}/data.parquet"
    size = local_path.stat().st_size
    t0 = time.monotonic()
    # L42: ContentType only, no transport-level encoding header.
    s3.upload_file(
        str(local_path),
        R2_BUCKET,
        key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    elapsed = time.monotonic() - t0
    LOG.info(
        "uploaded s3://%s/%s  bytes=%d  elapsed=%.1fs  rate=%.1f MiB/s",
        R2_BUCKET, key, size, elapsed,
        (size / (1024 * 1024)) / elapsed if elapsed > 0 else 0,
    )
    return {"r2_key": f"s3://{R2_BUCKET}/{key}", "bytes": size, "elapsed_s": elapsed}


def register_in_iceberg(r2_uri: str) -> dict:
    """Register the uploaded Parquet as a new data file in the
    ``usaspending.contracts`` Iceberg table; commits one new snapshot.

    This is the Iceberg-substrate replacement for the manual
    ``snapshot=YYYY-MM-DD/`` directory convention. The R2 key itself is
    still written (so existing readers using ``read_parquet()`` against
    the directory layout keep working); the Iceberg manifest is what new
    consumers (audience_factory et al.) read via PyIceberg.
    """
    t0 = time.monotonic()
    catalog = get_catalog()
    table = catalog.load_table(ICEBERG_TABLE_ID)
    table.add_files([r2_uri])
    snapshot_id = table.metadata.current_snapshot_id
    elapsed = time.monotonic() - t0
    LOG.info(
        "iceberg add_files done; new snapshot=%s elapsed=%.1fs",
        snapshot_id, elapsed,
    )
    return {"snapshot_id": snapshot_id, "elapsed_s": elapsed}


def derive_snapshot_date(zip_path: Path) -> str:
    """Extract YYYY-MM-DD from a USAspending ZIP filename.

    Matches the canonical pattern `FY{YYYY}_All_Contracts_Full_{YYYYMMDD}.zip`."""
    m = re.search(r"_(\d{4})(\d{2})(\d{2})\.zip$", zip_path.name)
    if not m:
        raise ValueError(
            f"Cannot derive snapshot date from filename: {zip_path.name}. "
            f"Pass --snapshot-date YYYY-MM-DD explicitly."
        )
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", required=True, type=Path)
    ap.add_argument("--year", required=True, type=int)
    ap.add_argument(
        "--snapshot-date",
        default=None,
        help="YYYY-MM-DD; auto-derived from ZIP filename if omitted",
    )
    ap.add_argument(
        "--keep-local",
        action="store_true",
        help="Don't delete the local Parquet after upload",
    )
    ap.add_argument(
        "--no-iceberg-register",
        action="store_true",
        help="Skip the post-upload Iceberg add_files() registration",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if not args.zip.exists():
        LOG.error("zip not found: %s", args.zip)
        return 2

    snapshot_date = args.snapshot_date or derive_snapshot_date(args.zip)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", snapshot_date):
        LOG.error("snapshot-date must be YYYY-MM-DD, got: %s", snapshot_date)
        return 2
    LOG.info("snapshot_date=%s year=%d zip=%s", snapshot_date, args.year, args.zip)

    with tempfile.NamedTemporaryFile(
        prefix=f"usa_fy{args.year}_", suffix=".parquet", delete=False
    ) as tf:
        local_parquet = Path(tf.name)

    try:
        stats = stream_zip_to_parquet(args.zip, args.year, local_parquet)
        LOG.info(
            "DONE write  rows=%d  cols=%d  files=%d  elapsed=%.1fs  parquet=%s  size=%.1f MiB",
            stats["rows_total"],
            stats["schema_cols"],
            stats["files_written"],
            stats["elapsed_s"],
            local_parquet,
            local_parquet.stat().st_size / (1024 * 1024),
        )
        upload_stats = upload_parquet(local_parquet, args.year, snapshot_date)
        LOG.info(
            "DONE upload  key=%s  bytes=%d",
            upload_stats["r2_key"],
            upload_stats["bytes"],
        )
        if not args.no_iceberg_register:
            iceberg_stats = register_in_iceberg(upload_stats["r2_key"])
            LOG.info(
                "DONE iceberg  snapshot=%s",
                iceberg_stats["snapshot_id"],
            )
        else:
            LOG.info("skipping Iceberg registration (--no-iceberg-register)")
        return 0
    finally:
        if not args.keep_local and local_parquet.exists():
            local_parquet.unlink()
            LOG.info("deleted local parquet %s", local_parquet)


if __name__ == "__main__":
    sys.exit(main())
