"""Streaming parser for NOAA AIS daily zipped CSV → pyarrow RecordBatches.

Memory profile: the daily zip is ~3-5 GB, the inner CSV ~10-30 GB raw. We
download the zip to a tempfile, then stream-decode the CSV via pyarrow's
incremental CSV reader. Each yielded RecordBatch is ~100k-500k rows; the
caller (R2Landing.write_streaming) writes them into a single parquet+zstd
object with one open ParquetWriter.

Output schema (PARQUET_SCHEMA): the 17 NOAA AIS columns mapped to lowercase
snake_case, plus per-batch provenance (source_run_id, ingested_at). Mirrors
entities.source_ais_pings's column shape so the RisingWave source over R2
can re-use the same field names.

Usage:

    for batch in stream_daily_csv_batches(zip_tempfile_path, feed_date,
                                           run_id, ingested_at):
        ...  # send to R2Landing.write_streaming via a generator
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from datetime import date, datetime
from uuid import UUID

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pa_csv

from noaa_ais.feed_urls import daily_csv_filename_inside_zip

# NOAA daily CSV is 17 columns; pyarrow ReadOptions.column_names overrides
# the upper-case header NOAA ships. skip_rows=1 drops that header row.
NOAA_CSV_COLUMN_NAMES: tuple[str, ...] = (
    "mmsi",
    "base_datetime",
    "lat",
    "lon",
    "sog",
    "cog",
    "heading",
    "vessel_name",
    "imo",
    "call_sign",
    "vessel_type",
    "status",
    "length",
    "width",
    "draft",
    "cargo",
    "transceiver_class",
)

# NOAA emits BaseDateTime as a tz-naive ISO string ("2024-01-02T00:00:00").
# pyarrow's CSV reader refuses to coerce a tz-naive string into timestamp[tz=UTC]
# (it wants %z in the format), so we read tz-naive and assume_timezone in the
# augment step. Output schema is tz-aware UTC (PARQUET_SCHEMA below).
NOAA_CSV_COLUMN_TYPES: dict[str, pa.DataType] = {
    "mmsi": pa.int64(),
    "base_datetime": pa.timestamp("us"),
    "lat": pa.float64(),
    "lon": pa.float64(),
    "sog": pa.float64(),
    "cog": pa.float64(),
    "heading": pa.float64(),
    "vessel_name": pa.string(),
    "imo": pa.string(),
    "call_sign": pa.string(),
    "vessel_type": pa.int32(),
    "status": pa.int32(),
    "length": pa.float64(),
    "width": pa.float64(),
    "draft": pa.float64(),
    "cargo": pa.int32(),
    "transceiver_class": pa.string(),
}

# Schema written to R2: source CSV columns + run-grain provenance.
# base_datetime is widened from CSV's tz-naive to tz-aware UTC at augment time.
def _parquet_field(name: str) -> pa.Field:
    if name == "base_datetime":
        return pa.field("base_datetime", pa.timestamp("us", tz="UTC"))
    return pa.field(name, NOAA_CSV_COLUMN_TYPES[name])


PARQUET_SCHEMA: pa.Schema = pa.schema(
    [_parquet_field(name) for name in NOAA_CSV_COLUMN_NAMES]
    + [
        pa.field("source_run_id", pa.string(), nullable=False),
        pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

# 4 MB block_size — big enough that pyarrow's CSV reader amortizes its
# parse-state overhead, small enough that a malformed line localizes to a
# small batch. Tuned against FAA aircraft registry's 1 MB default which
# produced excessive batch counts on multi-GB inputs.
CSV_BLOCK_SIZE = 1 << 22


def _read_options() -> pa_csv.ReadOptions:
    return pa_csv.ReadOptions(
        use_threads=True,
        block_size=CSV_BLOCK_SIZE,
        column_names=list(NOAA_CSV_COLUMN_NAMES),
        skip_rows=1,
    )


def _convert_options() -> pa_csv.ConvertOptions:
    return pa_csv.ConvertOptions(
        column_types=NOAA_CSV_COLUMN_TYPES,
        null_values=["", "NULL", "null"],
        strings_can_be_null=True,
        # NOAA emits "%Y-%m-%dT%H:%M:%S" UTC. The pa.timestamp(tz="UTC")
        # column type drives wall-clock-to-timestamp conversion; we pass
        # the explicit format so the parser doesn't fall back to its
        # locale-dependent default.
        timestamp_parsers=["%Y-%m-%dT%H:%M:%S"],
    )


def _augment_with_provenance(
    batch: pa.RecordBatch, *, run_id: UUID, ingested_at: datetime
) -> pa.RecordBatch:
    """Widen base_datetime to tz=UTC, append source_run_id + ingested_at.

    pyarrow RecordBatch is immutable; we rebuild via from_arrays so the
    schema matches PARQUET_SCHEMA exactly. base_datetime is widened from
    tz-naive (what NOAA's CSV gives us) to tz-aware UTC via
    pc.assume_timezone — no wall-clock shift, just the metadata flip.
    """
    n = batch.num_rows
    base_dt_idx = batch.schema.get_field_index("base_datetime")
    base_dt_utc = pc.assume_timezone(batch.column(base_dt_idx), "UTC")

    columns: list[pa.Array] = []
    for i, name in enumerate(NOAA_CSV_COLUMN_NAMES):
        if name == "base_datetime":
            columns.append(base_dt_utc)
        else:
            columns.append(batch.column(i))

    columns.append(pa.array([str(run_id)] * n, type=pa.string()))
    columns.append(pa.array([ingested_at] * n, type=pa.timestamp("us", tz="UTC")))
    return pa.RecordBatch.from_arrays(columns, schema=PARQUET_SCHEMA)


def stream_daily_csv_batches(
    *,
    zip_path: str,
    feed_date: date,
    run_id: UUID,
    ingested_at: datetime,
) -> Iterator[pa.RecordBatch]:
    """Stream RecordBatches from one NOAA daily-CSV zip.

    The zip contains exactly one file named AIS_YYYY_MM_DD.csv (per
    feed_urls.daily_csv_filename_inside_zip). Older NOAA archives
    occasionally nested the CSV under a subdirectory; we resolve by
    looking for the first .csv member if the canonical name is absent.
    """
    canonical = daily_csv_filename_inside_zip(feed_date)
    with zipfile.ZipFile(zip_path) as zf:
        member_names = zf.namelist()
        if canonical in member_names:
            csv_member = canonical
        else:
            csv_candidates = [m for m in member_names if m.lower().endswith(".csv")]
            if not csv_candidates:
                raise RuntimeError(
                    f"No CSV inside {zip_path} for feed_date={feed_date.isoformat()}; "
                    f"members: {member_names}"
                )
            csv_member = csv_candidates[0]

        with zf.open(csv_member) as csv_stream:
            reader = pa_csv.open_csv(
                csv_stream,
                read_options=_read_options(),
                parse_options=pa_csv.ParseOptions(delimiter=","),
                convert_options=_convert_options(),
            )
            try:
                while True:
                    try:
                        batch = reader.read_next_batch()
                    except StopIteration:
                        break
                    if batch.num_rows == 0:
                        continue
                    yield _augment_with_provenance(
                        batch, run_id=run_id, ingested_at=ingested_at
                    )
            finally:
                reader.close()
