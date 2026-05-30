"""R2 landing zone — ZSTD-compressed Parquet to Cloudflare R2.

Single bucket (dex-raw-landing-zone) with per-source prefixes. Object key
pattern: {source_id}/{feed_name}/{feed_date}/{run_id}.parquet.zst — run_id
in the key is critical for idempotency and historical re-ingest debugging
(per Backend Harness Architectural Standards directive #2).

Credentials read from env (Modal secret 'bulk-ingest-r2'):
    R2_ENDPOINT
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY

Modal secret setup (run once from hq-all root):

    doppler run --project hq-all --config prd -- bash -c '
        modal secret create --force bulk-ingest-r2 \\
            R2_ENDPOINT="$R2_ENDPOINT" \\
            R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \\
            R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
    '

R2 is S3-API-compatible; we use boto3's "s3" client with endpoint_url
pointed at R2. region_name='auto' is the R2 convention (no real region).
"""

from __future__ import annotations

import io
import os
from collections.abc import Iterable
from datetime import date
from typing import Any
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq

from .base import R2_DEFAULT_BUCKET, LandingResult


class R2Landing:
    """Streaming-friendly Parquet+ZSTD writer for Cloudflare R2.

    For small payloads (<256MB) we materialize a single Parquet object in
    memory and put_object. For large payloads, callers should batch and
    call write_batch repeatedly with distinct run_ids — each call writes
    one R2 object. Multipart upload is not required at this layer because
    boto3.put_object transparently handles up to 5GB; larger objects need
    upload_fileobj which we expose via write_streaming() for FMCSA-class
    feeds (the 250M-row case).
    """

    landing_zone: str = "r2"

    def __init__(self, bucket: str = R2_DEFAULT_BUCKET) -> None:
        # Defer boto3 import so the module is importable in environments
        # without boto3 (CI / unit tests that don't exercise R2).
        import boto3

        self.bucket = bucket
        endpoint = os.environ.get("R2_ENDPOINT")
        access_key = os.environ.get("R2_ACCESS_KEY_ID")
        secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        if not all([endpoint, access_key, secret_key]):
            raise RuntimeError(
                "R2 credentials missing — need R2_ENDPOINT, R2_ACCESS_KEY_ID, "
                "R2_SECRET_ACCESS_KEY in env (Modal secret 'bulk-ingest-r2')."
            )
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="auto",
        )

    @staticmethod
    def object_key(
        *,
        source_id: str,
        feed_name: str,
        feed_date: date,
        run_id: UUID,
    ) -> str:
        # Plain `.parquet` extension. The ZSTD compression is INTERNAL
        # column-chunk compression baked into the Parquet container;
        # naming files `.parquet.zst` misleads RW's S3 connector into
        # treating the body as a zstd-wrapped binary (per L42).
        return f"{source_id}/{feed_name}/{feed_date.isoformat()}/{run_id}.parquet"

    def write_batch(
        self,
        *,
        source_id: str,
        feed_name: str,
        feed_date: date,
        run_id: UUID,
        rows: Iterable[dict[str, Any]],
    ) -> LandingResult:
        """Write rows as one ZSTD-compressed Parquet object to R2."""
        materialized = list(rows)
        if not materialized:
            return LandingResult(
                landing_zone=self.landing_zone,
                rows_loaded=0,
                payload_bytes=0,
                r2_bucket=self.bucket,
                r2_object_key=None,
                payload_format="parquet_zstd",
            )

        table = pa.Table.from_pylist(materialized)
        buf = io.BytesIO()
        pq.write_table(table, buf, compression="zstd", compression_level=3)
        payload = buf.getvalue()

        key = self.object_key(
            source_id=source_id,
            feed_name=feed_name,
            feed_date=feed_date,
            run_id=run_id,
        )
        # ContentType only — NOT ContentEncoding=zstd. RW's S3 reader
        # interprets that header as "decompress the body before parsing,"
        # which mangles internal-ZSTD Parquet files (per L42).
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType="application/x-parquet",
        )
        return LandingResult(
            landing_zone=self.landing_zone,
            rows_loaded=len(materialized),
            payload_bytes=len(payload),
            r2_bucket=self.bucket,
            r2_object_key=key,
            payload_format="parquet_zstd",
        )

    def write_streaming(
        self,
        *,
        source_id: str,
        feed_name: str,
        feed_date: date,
        run_id: UUID,
        batches: Iterable[pa.RecordBatch],
    ) -> LandingResult:
        """Stream RecordBatches to R2 via multipart upload using the default
        object_key pattern.

        Use for feeds whose payload exceeds memory (FMCSA Vehicle Inspection
        ~250M rows). Caller produces RecordBatches via pyarrow's incremental
        API (e.g., pq.ParquetFile(...).iter_batches() or Arrow Streaming
        Format). The first batch defines the schema; subsequent batches
        must match.

        For non-default partitioning (Hive-style year=YYYY/month=MM/day=DD,
        per-table prefixes, etc., that other ingest sources use to feed
        RisingWave's match_pattern globbing), call write_streaming_to_key
        with a caller-computed object key.
        """
        key = self.object_key(
            source_id=source_id,
            feed_name=feed_name,
            feed_date=feed_date,
            run_id=run_id,
        )
        return self.write_streaming_to_key(key=key, batches=batches)

    def write_streaming_to_key(
        self,
        *,
        key: str,
        batches: Iterable[pa.RecordBatch],
        pinned_schema: pa.Schema | None = None,
    ) -> LandingResult:
        """Stream RecordBatches to a caller-supplied R2 object key.

        Sources with custom partitioning (USAspending Hive-style year=*/
        month=*/day=*, SEC ADV table=*, etc.) compute their own key and
        bypass the default {source_id}/{feed_name}/{feed_date}/{run_id}
        pattern. The match_pattern in the corresponding RisingWave SOURCE
        DDL must mirror whatever convention the caller picks.

        ``pinned_schema`` — optional pa.Schema to pin the parquet writer's
        type system. Without it, the writer infers from the first batch,
        which fails when later batches have a different shape (e.g., the
        L49 trap where an all-null first batch infers ``null`` types that
        subsequent typed batches can't cast to). Callers carrying their
        own ``PINNED_SCHEMA`` for L49 compliance MUST pass it.
        """
        # Two-pass: write parquet to local temp, then upload_fileobj to R2.
        # Streaming-encode-then-upload is the boto3 idiom; pyarrow's parquet
        # writer needs a seekable sink, so we use a tempfile rather than
        # piping to boto3's UploadStreamWriter.
        import tempfile

        rows_loaded = 0
        payload_bytes = 0
        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            writer: pq.ParquetWriter | None = None
            for batch in batches:
                if pinned_schema is not None:
                    # Cast each batch to the pinned schema so all-null first
                    # batches don't poison the writer's inferred types (L49).
                    batch = batch.cast(pinned_schema)
                if writer is None:
                    writer = pq.ParquetWriter(
                        tmp.name,
                        schema=pinned_schema if pinned_schema is not None else batch.schema,
                        compression="zstd",
                        compression_level=3,
                    )
                writer.write_batch(batch)
                rows_loaded += batch.num_rows
            if writer is not None:
                writer.close()

            # Guard 1 (pre-upload short-circuit): if no batches yielded any rows,
            # do NOT call upload_fileobj — boto3 would PUT a 0-byte object that
            # idempotency caches treat as "already done" and downstream readers
            # cannot distinguish from a real empty result. This is the
            # poison-file class of bug fixed in cycle usaspending-poison-file-class-fix-v1.
            # Callers that legitimately expect zero rows must use
            # assert_landed_or_explicit_empty(...) from modal.landing.safety.
            if writer is None or rows_loaded == 0:
                return LandingResult(
                    landing_zone=self.landing_zone,
                    rows_loaded=0,
                    payload_bytes=0,
                    r2_bucket=self.bucket,
                    r2_object_key=None,
                    payload_format="parquet_zstd",
                )

            tmp.flush()
            tmp.seek(0)
            payload_bytes = os.path.getsize(tmp.name)

            # Guard 2 (post-flush size assertion): defense-in-depth against a
            # future regression where a writer opens but produces an empty
            # footer-only parquet. This path is unreachable in normal flow
            # (Guard 1 handles the zero-rows case), but it stops a regression
            # where someone removes Guard 1 without noticing.
            if payload_bytes == 0:
                raise RuntimeError(
                    f"R2Landing.write_streaming_to_key: refusing to upload 0-byte "
                    f"artifact to {self.bucket}/{key} (rows_loaded={rows_loaded}). "
                    f"This is the poison-file class of bug; if upstream had zero "
                    f"rows the caller must use assert_landed_or_explicit_empty(...) "
                    f"before invoking this writer."
                )

            # ContentType only — NO ContentEncoding=zstd. See L42.
            self._client.upload_fileobj(
                tmp,
                Bucket=self.bucket,
                Key=key,
                ExtraArgs={"ContentType": "application/x-parquet"},
            )

        return LandingResult(
            landing_zone=self.landing_zone,
            rows_loaded=rows_loaded,
            payload_bytes=payload_bytes,
            r2_bucket=self.bucket,
            r2_object_key=key,
            payload_format="parquet_zstd",
        )
