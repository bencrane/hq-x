"""Overture Maps Places (US slice) ingest script.

Reads the US slice of the Overture Maps Places parquet dataset from the public
S3 bucket and upserts into entities.source_overture_places via a COPY-to-temp +
ON CONFLICT upsert pattern (DFPI / FMCSA precedent).

Source:
    s3://overturemaps-us-west-2/release/<release>/theme=places/type=place/*.parquet
    Public bucket — anonymous read via pyarrow.fs.S3FileSystem(anonymous=True).

Env:
    DEX_DB_URL_POOLED  — psycopg connection string (pgbouncer pooled; no DDL).
                         Falls back to DEX_DB_URL_DIRECT if unset.
    OVERTURE_RELEASE   — (optional) pin a specific release string, e.g.
                         '2026-04-23.0'. If unset, auto-resolves to the latest
                         release in the bucket.
    MODAL_TASK_ID      — injected by Modal container runtime; captured as
                         source_task_id.

Usage:
    python run_overture_places_ingest.py [--release 2026-04-23.0]
                                         [--batch-size 10000]
                                         [--max-rows N]
                                         [--dry-run]
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import sys
import time
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import pyarrow as pa
import pyarrow.fs
import pyarrow.parquet as pq
import psycopg
import psycopg.types.json
from psycopg.types.json import Jsonb


def _json_default(obj: Any) -> Any:
    """JSON encoder default for pyarrow-derived types that json.dumps rejects.

    Overture parquet rows contain WKB geometry (bytes); some sources also surface
    pyarrow Decimal128 and datetime/date instances after to_pylist(). Encode each
    so the row round-trips as JSONB.
    """
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(obj)).decode("ascii")
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(
        f"Object of type {type(obj).__name__} is not JSON serializable"
    )


def _dumps(obj: Any) -> str:
    return json.dumps(obj, default=_json_default)


# Apply globally so every Jsonb(...) wrapper picks up the bytes/Decimal/datetime
# handling without per-call boilerplate.
psycopg.types.json.set_json_dumps(_dumps)


def _scrub_nuls(obj: Any) -> Any:
    """Recursively remove NUL (\\x00) chars from string values.

    Postgres TEXT and JSONB both reject embedded NUL bytes (psycopg surfaces
    `UntranslatableCharacter: \\u0000 cannot be converted to text`). Overture
    occasionally surfaces them in address `freeform` and similar fields from
    upstream data corruption. Drop them before the row hits COPY.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "") if "\x00" in obj else obj
    if isinstance(obj, dict):
        return {k: _scrub_nuls(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub_nuls(v) for v in obj]
    return obj

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BUCKET = "overturemaps-us-west-2"
PATH_TEMPLATE = "release/{release}/theme=places/type=place"
PROVIDER = "overture-maps"
BATCH_SIZE = 10_000

STAGE_TABLE = "_stage_overture_places"

UPSERT_COLUMNS = [
    "overture_id",
    "geometry",
    "bbox",
    "version",
    "theme",
    "type",
    "sources",
    "names",
    "categories",
    "basic_category",
    "taxonomy",
    "confidence",
    "websites",
    "socials",
    "emails",
    "phones",
    "brand",
    "addresses",
    "operating_status",
    "source_filename",
    "source_download_url",
    "source_observed_at",
    "source_run_metadata",
    "source_task_id",
    "source_schedule_id",
    "raw_source_row",
]

UPDATE_SET = [
    "geometry",
    "bbox",
    "version",
    "theme",
    "type",
    "sources",
    "names",
    "categories",
    "basic_category",
    "taxonomy",
    "confidence",
    "websites",
    "socials",
    "emails",
    "phones",
    "brand",
    "addresses",
    "operating_status",
    "source_filename",
    "source_download_url",
    "source_observed_at",
    "source_run_metadata",
    "source_task_id",
    "source_schedule_id",
    "raw_source_row",
    "ingested_at",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _database_url() -> str:
    """Return the DB connection string from env.

    Prefers DEX_DB_URL_DIRECT for this script: the COPY-to-stage + ON CONFLICT
    upsert flow runs long transactions and keeps session state across batches,
    which pgbouncer transaction-pooling can break. Falls back to pooled only
    if DIRECT is unset.
    """
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")
    if not url:
        raise RuntimeError(
            "Neither DEX_DB_URL_DIRECT nor DEX_DB_URL_POOLED is set in the environment."
        )
    return url


def insert_run(conn: psycopg.Connection, source_release_version: str) -> uuid.UUID:
    """Insert a run row with status='running'; return the generated run_id."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.overture_places_ingest_runs
                (status, source_release_version)
            VALUES ('running', %s)
            RETURNING run_id
            """,
            (source_release_version,),
        )
        run_id: uuid.UUID = cur.fetchone()[0]
    conn.commit()
    log.info("Run started: run_id=%s release=%s", run_id, source_release_version)
    return run_id


def finalize_run(
    conn: psycopg.Connection,
    run_id: uuid.UUID,
    *,
    status: str,
    rows_seen: int,
    rows_upserted: int,
    error_text: Optional[str] = None,
) -> None:
    """Update the run row to a terminal status."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.overture_places_ingest_runs
               SET status = %s,
                   rows_seen = %s,
                   rows_upserted = %s,
                   completed_at = now(),
                   error_text = %s
             WHERE run_id = %s
            """,
            (status, rows_seen, rows_upserted, error_text, run_id),
        )
    conn.commit()
    log.info(
        "Run finalized: run_id=%s status=%s rows_seen=%d rows_upserted=%d",
        run_id,
        status,
        rows_seen,
        rows_upserted,
    )


def ensure_stage_table(conn: psycopg.Connection) -> None:
    """Create the temp staging table (same shape as entities.source_overture_places,
    minus the ingested_at DEFAULT — temp tables are session-scoped and discarded
    on disconnect)."""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TEMP TABLE IF NOT EXISTS {STAGE_TABLE} (
                overture_id         text,
                geometry            jsonb,
                bbox                jsonb,
                version             integer,
                theme               text,
                type                text,
                sources             jsonb,
                names               jsonb,
                categories          jsonb,
                basic_category      text,
                taxonomy            jsonb,
                confidence          numeric,
                websites            text[],
                socials             text[],
                emails              text[],
                phones              text[],
                brand               jsonb,
                addresses           jsonb,
                operating_status    jsonb,
                source_filename     text,
                source_download_url text,
                source_observed_at  timestamptz,
                source_run_metadata jsonb,
                source_task_id      text,
                source_schedule_id  text,
                raw_source_row      jsonb
            )
            """
        )
    conn.commit()

# ---------------------------------------------------------------------------
# S3 / Overture helpers
# ---------------------------------------------------------------------------


def _make_fs() -> pyarrow.fs.S3FileSystem:
    return pyarrow.fs.S3FileSystem(anonymous=True, region="us-west-2")


def resolve_release(fs: pyarrow.fs.S3FileSystem) -> str:
    """Return the release string to use.

    If OVERTURE_RELEASE env var is set, use it directly. Otherwise, list the
    'release/' prefix on the public bucket and return the max lex-sorted entry
    (Overture uses ISO-date-prefixed release names like '2026-04-23.0').
    """
    env_release = os.environ.get("OVERTURE_RELEASE")
    if env_release:
        log.info("Using pinned release from env: %s", env_release)
        return env_release

    selector = pyarrow.fs.FileSelector(f"{BUCKET}/release/", recursive=False)
    entries = fs.get_file_info(selector)
    release_names = [
        e.path.split("/release/", 1)[-1].rstrip("/")
        for e in entries
        if e.type == pyarrow.fs.FileType.Directory and "release/" in e.path
    ]
    if not release_names:
        raise RuntimeError(
            f"No release directories found under s3://{BUCKET}/release/"
        )
    latest = max(release_names)
    log.info("Auto-resolved latest Overture release: %s", latest)
    return latest


def filter_us_rows(batch: pa.RecordBatch) -> list[dict]:
    """Return rows where at least one address has country == 'US'.

    Strategy: convert the batch to Python dicts (to_pylist) and filter in
    Python. pyarrow.compute on list-of-struct is awkward for nested-list
    filters; the Python fallback adds <1s per 10k-row batch.
    """
    result = []
    for row in batch.to_pylist():
        addresses = row.get("addresses") or []
        if any(
            (addr.get("country") if isinstance(addr, dict) else None) == "US"
            for addr in addresses
        ):
            result.append(row)
    return result


# ---------------------------------------------------------------------------
# Upsert helpers
# ---------------------------------------------------------------------------


def _copy_batch_to_stage(
    conn: psycopg.Connection,
    rows: list[tuple],
    col_list: str,
) -> None:
    """TRUNCATE + COPY rows into the staging table."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {STAGE_TABLE}")
        copy_sql = (
            f"COPY {STAGE_TABLE} ({col_list}) FROM STDIN"
        )
        with cur.copy(copy_sql) as copy:
            for row_tuple in rows:
                copy.write_row(row_tuple)


def _upsert_from_stage(conn: psycopg.Connection) -> int:
    """INSERT INTO entities.source_overture_places ... SELECT * FROM stage
    ON CONFLICT (overture_id) DO UPDATE; return rowcount."""
    col_list = ", ".join(UPSERT_COLUMNS)
    update_set = ",\n                ".join(
        f"{c} = EXCLUDED.{c}" if c != "ingested_at" else "ingested_at = now()"
        for c in UPDATE_SET
    )
    sql = f"""
        INSERT INTO entities.source_overture_places ({col_list})
        SELECT {col_list} FROM {STAGE_TABLE}
        ON CONFLICT (overture_id) DO UPDATE SET
            {update_set}
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.rowcount


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(cli_args: Optional[list[str]] = None) -> dict:
    parser = argparse.ArgumentParser(description="Overture Places ingest")
    parser.add_argument(
        "--release",
        default=None,
        help="Overture release string, e.g. '2026-04-23.0'. "
        "Defaults to auto-resolved latest.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help="Parquet batch size (rows per pyarrow batch).",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Stop after ingesting this many US rows (smoke-test limit).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse + filter rows but do NOT write to DB.",
    )
    args = parser.parse_args(cli_args if cli_args is not None else sys.argv[1:])

    if args.release:
        os.environ["OVERTURE_RELEASE"] = args.release

    fs = _make_fs()
    release = resolve_release(fs)
    parquet_prefix = f"{BUCKET}/{PATH_TEMPLATE.format(release=release)}"
    log.info("Parquet prefix: s3://%s", parquet_prefix)

    db_url = _database_url()
    modal_task_id = os.environ.get("MODAL_TASK_ID")

    col_list = ", ".join(UPSERT_COLUMNS)

    rows_seen = 0
    rows_upserted = 0

    with psycopg.connect(db_url, autocommit=False) as conn:
        if args.dry_run:
            log.info("DRY RUN — no DB writes.")
            run_id = uuid.uuid4()
        else:
            run_id = insert_run(conn, source_release_version=release)
            ensure_stage_table(conn)

        try:
            selector = pyarrow.fs.FileSelector(parquet_prefix, recursive=True)
            file_infos = fs.get_file_info(selector)
            parquet_files = [
                f for f in file_infos if f.path.endswith(".parquet")
            ]
            log.info("Found %d parquet file(s) under prefix", len(parquet_files))

            for f_info in parquet_files:
                log.info("Processing: %s", f_info.path)

                # Retry loop around the parquet read to absorb transient S3
                # network failures (curl error 35 / Connection reset by peer).
                # On retry we restart the file from the beginning; already-
                # committed rows hit ON CONFLICT (overture_id) DO UPDATE and
                # are no-ops, so retries are idempotent.
                file_attempt = 0
                file_done = False
                while not file_done:
                    file_attempt += 1
                    try:
                        # Recreate the S3FileSystem on each attempt. Pyarrow's
                        # S3FileSystem keeps an internal connection pool; after
                        # long backoffs those pooled HTTPS connections can be
                        # closed server-side, surfacing as "Connection reset by
                        # peer" / "SSL connect error" on the very first read of
                        # the new attempt even when the network is fine.
                        fs = _make_fs()
                        pf = pq.ParquetFile(f_info.path, filesystem=fs)

                        for batch in pf.iter_batches(batch_size=args.batch_size):
                            us_rows = filter_us_rows(batch)
                            if not us_rows:
                                continue

                            observed_at = datetime.now(timezone.utc)
                            source_filename = os.path.basename(f_info.path)
                            source_download_url = f"s3://{f_info.path}"
                            source_run_metadata = {
                                "release": release,
                                "run_id": str(run_id),
                                "modal_app": "data-engine-x-overture-places-ingest",
                            }

                            row_tuples = []
                            for r in us_rows:
                                r = _scrub_nuls(r)
                                row_tuples.append((
                                    r["id"],                                                    # overture_id
                                    Jsonb(r["geometry"]) if r.get("geometry") else None,       # geometry
                                    Jsonb(r["bbox"]) if r.get("bbox") else None,               # bbox
                                    r.get("version"),                                           # version
                                    r.get("theme"),                                             # theme
                                    r.get("type"),                                              # type
                                    Jsonb(r["sources"]) if r.get("sources") else None,         # sources
                                    Jsonb(r["names"]) if r.get("names") else None,             # names
                                    Jsonb(r["categories"]) if r.get("categories") else None,   # categories
                                    r.get("basic_category"),                                   # basic_category
                                    Jsonb(r["taxonomy"]) if r.get("taxonomy") else None,       # taxonomy
                                    r.get("confidence"),                                        # confidence
                                    r.get("websites"),                                          # websites (text[])
                                    r.get("socials"),                                           # socials (text[])
                                    r.get("emails"),                                            # emails (text[])
                                    r.get("phones"),                                            # phones (text[])
                                    Jsonb(r["brand"]) if r.get("brand") else None,             # brand
                                    Jsonb(r["addresses"]) if r.get("addresses") else None,     # addresses
                                    Jsonb(r["operating_status"]) if r.get("operating_status") else None,  # operating_status
                                    source_filename,                                            # source_filename
                                    source_download_url,                                        # source_download_url
                                    observed_at,                                                # source_observed_at
                                    Jsonb(source_run_metadata),                                 # source_run_metadata
                                    modal_task_id,                                              # source_task_id
                                    None,                                                       # source_schedule_id
                                    Jsonb(r),                                                   # raw_source_row
                                ))

                            rows_seen += len(row_tuples)

                            if not args.dry_run:
                                _copy_batch_to_stage(conn, row_tuples, col_list)
                                batch_upserted = _upsert_from_stage(conn)
                                conn.commit()
                                rows_upserted += batch_upserted
                                log.info(
                                    "Batch upserted: %d rows (cumulative seen=%d upserted=%d)",
                                    batch_upserted,
                                    rows_seen,
                                    rows_upserted,
                                )

                            if args.max_rows and rows_seen >= args.max_rows:
                                log.info("--max-rows %d reached; stopping early.", args.max_rows)
                                break

                        file_done = True

                    except OSError as e:
                        # Sustained S3/network outages can last several minutes.
                        # 20 attempts with backoff capped at 300s = up to ~30 min
                        # of total wait, enough to ride out multi-minute blips.
                        if file_attempt >= 20:
                            log.error(
                                "Giving up on %s after %d attempts: %s",
                                f_info.path, file_attempt, e,
                            )
                            raise
                        wait_s = min(300, 2 ** file_attempt)
                        log.warning(
                            "S3 read error on %s (attempt %d): %s — retrying in %ds",
                            f_info.path, file_attempt, e, wait_s,
                        )
                        time.sleep(wait_s)

                if args.max_rows and rows_seen >= args.max_rows:
                    break

            if not args.dry_run:
                finalize_run(
                    conn,
                    run_id,
                    status="succeeded",
                    rows_seen=rows_seen,
                    rows_upserted=rows_upserted,
                )

        except Exception as exc:
            if not args.dry_run:
                try:
                    conn.rollback()
                    finalize_run(
                        conn,
                        run_id,
                        status="failed",
                        rows_seen=rows_seen,
                        rows_upserted=rows_upserted,
                        error_text=str(exc),
                    )
                except Exception as inner:
                    log.error("Failed to record error in run table: %s", inner)
            raise

    result = {
        "run_id": str(run_id),
        "release": release,
        "rows_seen": rows_seen,
        "rows_upserted": rows_upserted,
    }
    log.info("Done: %s", result)
    return result


if __name__ == "__main__":
    main()
