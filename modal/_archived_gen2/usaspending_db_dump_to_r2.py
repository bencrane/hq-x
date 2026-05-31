"""USAspending DB-dump → multi-table raw R2 Parquet landing (Stage 2).

Downloads the monthly full USAspending PostgreSQL database dump (``pg_dump -Fd``
directory format, ZIP-wrapped), extracts it on a Modal Volume, then decodes each
target table via ``pg_restore --data-only -t <table> -f -``, streams the
COPY-text output through ``scripts/_lib/pg_copy_text_parser.py``, and writes
chunked ZSTD-compressed Parquet parts to Cloudflare R2.

**This pipeline lands raw data only — it does NOT emit Lance datasets.**
Lance emit is a downstream, demand-driven concern: a dataset becomes a Lance
dataset only when a per-key random-access consumer exists for it, and that is
almost always a *projection* (``usaspending/contracts_lance``,
``usaspending/recipient_grain_lance``), not a raw dump table. The transaction
tables here (``awards`` ~180M rows, ``transaction_fpds``, ``transaction_fabs``)
are bulk-scan transformation inputs: per the data-factory architecture they
stay raw Parquet, read by DuckDB-on-R2 at projection-build time. Forcing a
180M-row table through ``lance.write_dataset`` to R2 was the root cause of the
2026-05-21 multipart-upload failure that lost the 5 largest tables.

**Architecture — fan-out.** A lightweight ``ingest`` coordinator prepares the
dump (download/extract if the Volume cache is cold, preflight, ledger) then
fans the per-table decode out across one ``land_one_table`` worker container
per table, all running in parallel. The ~510M-row dump is ~31h of *sequential*
decode (``pg_restore`` → pure-Python COPY parse is the bottleneck); the fan-out
brings wall-clock down to the longest single table (~13-18h). A single 6h
Modal container could never finish it — that 6h wall, not the multipart bug,
killed the 2026-05-18 runs.

R2 layout (per CLAUDE.md §"bulk-historical Volume-King sources"):

    s3://dex-raw-landing-zone/usaspending/db-dump/<table>/release=<YYYY-MM-DD>/part-NNNNN.parquet

``release`` is the dump's ``Last-Modified`` date. Parts are chunked at
ROWS_PER_PART rows so each upload is a small, independent object — no giant
single-file multipart upload.

**Dispatch (L47 — multi-hour job, detach mandatory):**

    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run --detach modal/usaspending_db_dump_to_r2.py::run

Or via the CLI wrapper::

    python scripts/run_usaspending_db_dump_cycle.py

**Idempotency:** at run start, compare the dump URL's ``Last-Modified`` header to
``ops.usaspending_db_dump_ingest_runs.source_observed_at`` of the latest
``status='completed'`` row.  If they match, insert a ``status='no_change'`` row
and exit without downloading.  Per-table, the validity-keyed skip gate
(``_should_skip_table``) skips any table a prior run already verified
``completed`` for the same dump release AND whose Parquet is present in R2 — so
a re-dispatch after a partial failure resumes cheaply without re-decoding
completed tables.

**Target tables (12) — ordered by priority / size:**
  1.  subaward             (~10M rows, FSRS first-tier subaward reporting)
  2.  awards               (~180M rows)
  3.  transaction_normalized (not present in this dump version — skipped)
  4.  transaction_fpds     (~100M rows)
  5.  transaction_fabs     (~200M rows)
  6.  recipient_lookup     (~5-15M rows)
  7.  recipient_profile    (~5-15M rows)
  8.  references_location  (not present in this dump version — skipped)
  9.  toptier_agency       (<1K rows, dim)
  10. subtier_agency       (<10K rows, dim)
  11. agency               (<10K rows, dim)
  12. references_cfda      (<5K rows, dim)

``transaction_normalized`` and ``references_location`` carry no
pg_schema/pg_table mapping — they are legitimately absent from this dump
version and route to an explicit ``skipped`` ledger status, never ``failed``.

**Schema:** every column lands as Parquet ``string`` (all source columns are
decoded from PG COPY text). Raw landing is intentionally untyped — downstream
projections (``contracts_lance`` etc.) do the typing. Read back with DuckDB
``read_parquet(..., all_varchar=TRUE)``.

Secrets:
    dex-db    — DEX_DB_URL_DIRECT for ledger writes.
    bulk-ingest-r2     — R2 credentials (R2_ENDPOINT, R2_ACCESS_KEY_ID,
                         R2_SECRET_ACCESS_KEY).

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/usaspending_db_dump_to_r2.py
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import modal

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Modal app + image                                                             #
# --------------------------------------------------------------------------- #

app = modal.App("data-engine-x-usaspending-db-dump-multi-table")

image = (
    modal.Image.debian_slim(python_version="3.11")
    # postgresql-client-16 from the PGDG repo — USAspending's dump is created
    # by pg_dump 16.11 and uses file-format version 1.15, which the default
    # debian bookworm postgresql-client-15 can't read ("pg_restore: error:
    # unsupported version (1.15) in file header"). Pin to 16 to match upstream.
    .apt_install("ca-certificates", "wget", "gnupg", "lsb-release", "unzip")
    .run_commands(
        "wget -q https://www.postgresql.org/media/keys/ACCC4CF8.asc -O /etc/apt/trusted.gpg.d/postgresql.asc",
        'sh -c \'echo "deb http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" > /etc/apt/sources.list.d/pgdg.list\'',
        "apt-get update",
        "apt-get install -y postgresql-client-16",
    )
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install(
        "psycopg[binary]",
        "pyarrow",
        "boto3",
        "requests",
    )
    .add_local_dir("modal/landing", remote_path="/root/landing")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
)

# Shared cache volume — dump ZIP + extracted directory persist across retries
cache_vol = modal.Volume.from_name("usaspending-db-dump-cache", create_if_missing=True)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Resource spec — the decode is fanned out: a lightweight `ingest` coordinator
# prepares the dump + ledger, then `land_one_table` workers decode one table
# each, in parallel. The ~510M-row dump is ~31h of *sequential* decode; fanning
# the 10 mapped tables across 10 worker containers brings wall-clock down to
# the longest single table (~13-18h for transaction_fabs).
#
#   Coordinator (`ingest`): tiny — prepares the dump (cached → ~2 min) then
#   blocks on the worker fan-out. cpu=2 / 4 GB still covers a cold-cache
#   172 GB download if the Volume is ever empty.
#   Worker (`land_one_table`): cpu=8 / 16 GB — pg_restore decode + ZSTD encode
#   are CPU-bound; memory is bounded by a single 50K-row Arrow batch.
#   timeout=79200 (22h) on both — under Modal's 24h cap, ample for the largest
#   table even at the slow end of the observed ~276K rows/min decode rate.
COORD_MEMORY_MB = 4096
COORD_CPU = 2
WORKER_MEMORY_MB = 16384
WORKER_CPU = 8
FUNC_TIMEOUT = 79200

# R2 raw-landing target
R2_BUCKET = "dex-raw-landing-zone"
R2_PARQUET_PREFIX = "usaspending/db-dump"  # → <prefix>/<table>/release=<date>/part-NNNNN.parquet

# Discovery API — resolves the latest dump URL (Q4-resolved format per audit)
DISCOVERY_API_URL = (
    "https://api.usaspending.gov/api/v2/bulk_download/list_database_download_files/"
)

# Local paths. The dump lives on the shared Modal Volume at /cache (read by
# every worker). Each worker stages Parquet parts to its OWN container-local
# /tmp — never the Volume — so the 10 concurrent workers never contend on it.
CACHE_DIR = "/cache"
DUMP_DIR = "/cache/usaspending_db"            # extracted pg_dump -Fd directory
PARQUET_STAGING_DIR = "/tmp/parquet-staging"  # container-local; one part at a time

# Each part holds up to this many rows. A ~180M-row table → ~180 parts; each
# part is a small, independent R2 object (single PUT or a few multipart chunks).
# Each decoded ~50K-row Arrow batch becomes one Parquet row group within a part.
ROWS_PER_PART = 1_000_000

# ZSTD compression level for the Parquet parts. Level 3 (not 9) — raw landing
# is read once by the downstream projection build; decode throughput matters
# more than a few percent of file size on a ~510M-row ingest.
PARQUET_COMPRESSION_LEVEL = 3

# --------------------------------------------------------------------------- #
# Target tables (12) — ordered by priority / size                              #
# Each entry: name + (for tables present in this dump version) pg_schema +      #
# pg_table. Entries WITHOUT pg_schema/pg_table are legitimately absent from     #
# this dump version and route to an explicit 'skipped' ledger status.          #
# --------------------------------------------------------------------------- #

import pyarrow as pa  # noqa: E402  (after Modal imports)

TABLE_CONFIG: list[dict[str, Any]] = [
    {"name": "subaward", "pg_schema": "rpt", "pg_table": "subaward_search"},
    {"name": "awards", "pg_schema": "rpt", "pg_table": "award_search"},
    # transaction_normalized was consolidated into transaction_search_* in the
    # current USAspending schema — no pg_schema/pg_table mapping → skipped.
    {"name": "transaction_normalized"},
    {"name": "transaction_fpds", "pg_schema": "rpt", "pg_table": "transaction_search_fpds"},
    {"name": "transaction_fabs", "pg_schema": "rpt", "pg_table": "transaction_search_fabs"},
    {"name": "recipient_lookup", "pg_schema": "rpt", "pg_table": "recipient_lookup"},
    {"name": "recipient_profile", "pg_schema": "rpt", "pg_table": "recipient_profile"},
    # references_location was split into multiple ref_* tables upstream — no
    # mapping → skipped.
    {"name": "references_location"},
    {"name": "toptier_agency", "pg_schema": "public", "pg_table": "toptier_agency"},
    {"name": "subtier_agency", "pg_schema": "public", "pg_table": "subtier_agency"},
    {"name": "agency", "pg_schema": "public", "pg_table": "agency"},
    {"name": "references_cfda", "pg_schema": "public", "pg_table": "references_cfda"},
]

TARGET_TABLE_NAMES = [t["name"] for t in TABLE_CONFIG]

# Tables that have a pg_schema/pg_table mapping and are expected to be present
# in the dump. Entries WITHOUT these fields (transaction_normalized,
# references_location) are legitimately absent from this dump version and must
# NOT be required by preflight or flapped to 'failed' in the ledger.
MAPPED_TABLE_CONFIGS = [t for t in TABLE_CONFIG if t.get("pg_schema") and t.get("pg_table")]


# --------------------------------------------------------------------------- #
# Helpers                                                                       #
# --------------------------------------------------------------------------- #

def _bridge_database_url() -> None:
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _connect():
    import psycopg
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ["DATABASE_URL"]
    return psycopg.connect(url, autocommit=True)


def _r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2.

    R2 is S3-compatible; ``region_name='auto'`` + the account R2 endpoint are
    the canonical settings. Adaptive retries cover transient upload errors.
    """
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 10, "mode": "adaptive"}),
    )


def _clear_r2_prefix(s3, bucket: str, prefix: str) -> int:
    """Delete every object under ``prefix``. Returns the count deleted.

    Used before (re-)landing a table so a prior partial attempt's part files
    can't mix with the retry's parts (which would double-count rows).
    """
    deleted = 0
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        contents = resp.get("Contents", [])
        if contents:
            s3.delete_objects(
                Bucket=bucket,
                Delete={"Objects": [{"Key": o["Key"]} for o in contents]},
            )
            deleted += len(contents)
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    return deleted


def _resume_part_index(s3, bucket: str, key_prefix: str) -> int:
    """Part index to (re)start landing from — preemption-resume support.

    Lists existing ``part-NNNNN.parquet`` objects under ``key_prefix``. A prior
    (preempted) attempt leaves a contiguous run of K parts; this returns K-1 so
    the last part is re-decoded and overwritten — cheap (≤1 part redone), and it
    lets a fully-landed-but-not-ledger-stamped table self-heal on re-run.
    Returns 0 for an empty prefix. A non-contiguous prefix (corrupt — should not
    occur with sequential writes) is wiped and 0 returned.
    """
    import re

    keys: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": key_prefix + "/"}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        keys.extend(o["Key"] for o in resp.get("Contents", []))
        if resp.get("IsTruncated"):
            token = resp.get("NextContinuationToken")
        else:
            break
    idxs = sorted(
        int(m.group(1))
        for k in keys
        for m in (re.search(r"/part-(\d+)\.parquet$", k),)
        if m
    )
    if not idxs:
        return 0
    if idxs == list(range(len(idxs))):
        return len(idxs) - 1  # redo the last landed part
    logger.warning(
        "non-contiguous parts under %s (%s) — clearing for a fresh decode",
        key_prefix, idxs,
    )
    _clear_r2_prefix(s3, bucket, key_prefix + "/")
    return 0


def _preflight_dump_completeness(dump_dir: str) -> None:
    """~60s preflight: assert the dump contains all MAPPED_TABLE_CONFIGS tables.

    Runs ``pg_restore --list`` and checks that each TABLE_CONFIG entry with a
    pg_schema/pg_table mapping appears in the table-data section.  Entries
    without a pg_schema (transaction_normalized, references_location) are
    legitimately absent from this dump version and are NOT required.

    Raises RuntimeError if any mapped table is absent — the caller should
    fail the run before any landing work begins.
    """
    result = subprocess.run(
        ["pg_restore", "--list", dump_dir],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"pg_restore --list failed (exit {result.returncode}): {result.stderr[:500]}"
        )
    toc_text = result.stdout.lower()
    missing = []
    for cfg in MAPPED_TABLE_CONFIGS:
        pg_schema = cfg["pg_schema"].lower()
        pg_table = cfg["pg_table"].lower()
        # The TOC line format: "<oid>; <oid> <oid> TABLE DATA <schema> <table> <owner>"
        # e.g. "3699; 0 51261 TABLE DATA rpt subaward_search benjamincrane"
        marker = f"table data {pg_schema} {pg_table} "
        if marker not in toc_text:
            missing.append(f"{pg_schema}.{pg_table}")
    if missing:
        raise RuntimeError(
            f"Preflight failed — dump at {dump_dir} is missing {len(missing)} "
            f"expected table(s): {missing}. Aborting before any download/restore/landing work."
        )
    logger.info("preflight OK — all %d mapped tables present in dump TOC", len(MAPPED_TABLE_CONFIGS))


def _insert_table_ledger_rows(conn, run_id: str, table_names: list[str]) -> None:
    """Insert pending rows for all tables before any work begins (mechanism 1)."""
    for tname in table_names:
        conn.execute(
            """
            INSERT INTO ops.usaspending_db_dump_table_ingest
                (run_id, table_name, status)
            VALUES (%s::uuid, %s, 'pending')
            ON CONFLICT (run_id, table_name) DO NOTHING
            """,
            (run_id, tname),
        )
    logger.info("per-table ledger: inserted %d pending rows for run_id=%s", len(table_names), run_id)


def _update_table_ledger(
    conn,
    run_id: str,
    table_name: str,
    status: str,
    row_count: int | None = None,
    source_row_count: int | None = None,
    error_message: str | None = None,
) -> None:
    """Update a single per-table ledger row (mechanism 1)."""
    conn.execute(
        """
        UPDATE ops.usaspending_db_dump_table_ingest
        SET status           = %s,
            row_count        = %s,
            source_row_count = %s,
            error_message    = %s,
            completed_at     = CASE WHEN %s IN ('completed','failed','skipped') THEN NOW() ELSE completed_at END
        WHERE run_id = %s::uuid AND table_name = %s
        """,
        (status, row_count, source_row_count, error_message, status, run_id, table_name),
    )


def _stamp_table(run_id: str, table_name: str, status: str, **kwargs: Any) -> None:
    """Update one per-table ledger row on a fresh, short-lived connection.

    The per-table decode runs for hours; holding a single DB connection idle
    across a multi-hour ``pg_restore`` stream invites an idle-TCP drop. Every
    ledger transition opens its own connection instead.
    """
    with _connect() as conn:
        _update_table_ledger(conn, run_id, table_name, status, **kwargs)


def _ledger_source_count(run_id: str, table_name: str) -> int | None:
    """Return a prior attempt's recorded source_row_count from the ledger.

    A preempted worker restarts from the top; reusing the source count an
    earlier attempt already measured skips a redundant full count-scan of the
    dump. Returns None if not yet recorded.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT source_row_count FROM ops.usaspending_db_dump_table_ingest "
            "WHERE run_id = %s::uuid AND table_name = %s",
            (run_id, table_name),
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def _table_already_landed(
    conn,
    table_name: str,
    source_observed_at: datetime | None,
    source_row_count: int | None,
) -> bool:
    """Ledger half of the validity-keyed skip gate (mechanism 6).

    Returns True iff a prior run AGAINST THE SAME DUMP (matched on the dump's
    ``source_observed_at``) already verified this table ``completed`` with
    ``row_count`` equal to the freshly-counted source row count.

    This is NOT the 2026-05-18 buggy gate (which skipped on "dataset has any
    rows" — letting partial writes be skipped forever). Skipping here requires
    a prior run to have proven row_count == source_row_count for this exact
    dump. A partial/failed prior run has no such row → the table is re-landed.
    """
    if source_observed_at is None or source_row_count is None or source_row_count < 0:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM ops.usaspending_db_dump_table_ingest ti
        JOIN ops.usaspending_db_dump_ingest_runs r ON r.run_id = ti.run_id
        WHERE ti.table_name        = %s
          AND ti.status            = 'completed'
          AND ti.row_count         = %s
          AND r.source_observed_at = %s
        LIMIT 1
        """,
        (table_name, source_row_count, source_observed_at),
    ).fetchone()
    return row is not None


def _should_skip_table(
    conn,
    s3,
    bucket: str,
    table_name: str,
    source_observed_at: datetime | None,
    release: str,
    source_row_count: int | None,
) -> bool:
    """Validity-keyed skip gate (mechanism 6) — the full decision.

    Skip a table iff BOTH hold:
      (a) a prior run verified it ``completed`` for the SAME dump
          (``_table_already_landed`` — the ledger check), AND
      (b) its Parquet is actually present in R2 under this release.

    Condition (b) is load-bearing: a ``completed`` ledger row from the
    pre-rework Lance era points at ``polaris-warehouse/usaspending/<t>_lance/``,
    NOT at this pipeline's ``usaspending/db-dump/`` prefix — so (b) is False
    for such a row and the table is correctly re-landed as Parquet.
    """
    if not _table_already_landed(conn, table_name, source_observed_at, source_row_count):
        return False
    prefix = f"{R2_PARQUET_PREFIX}/{table_name}/release={release}/"
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return bool(resp.get("Contents"))


def _count_rows_from_dump(dump_dir: str, pg_schema: str, pg_table: str) -> int:
    """Count source rows in the dump for a given table via pg_restore stdout.

    Spawns pg_restore --data-only, skips the COPY header, counts data lines
    until the terminator. Used to populate source_row_count for write-then-verify.
    Returns -1 if the table is not found in the dump.
    """
    proc = subprocess.Popen(
        [
            "pg_restore", "--data-only",
            "-n", pg_schema, "-t", pg_table,
            "-f", "-", dump_dir,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    count = 0
    in_copy = False
    try:
        for raw_line in proc.stdout:  # type: ignore[union-attr]
            try:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
            except Exception:
                continue
            if not in_copy:
                if line.upper().startswith("COPY ") and "FROM STDIN" in line.upper():
                    in_copy = True
            else:
                if line == "\\.":
                    break
                count += 1
    finally:
        proc.stdout.close()  # type: ignore[union-attr]
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if not in_copy:
        logger.warning("_count_rows_from_dump: no COPY block found for %s.%s", pg_schema, pg_table)
        return -1
    return count


def _resolve_dump_url(override: str | None = None) -> tuple[str, str]:
    """Return (dump_url, last_modified_str).

    Calls the USAspending discovery API unless an override URL is provided.

    Discovery API returns a URL in the form:
        https://files.usaspending.gov/database_download/usaspending-db_YYYYMMDD.zip
    (ZIP-wrapped pg_dump -Fd directory, ~172 GB; resolved monthly cadence).
    """
    import requests

    if override:
        resp = requests.head(override, timeout=30)
        resp.raise_for_status()
        last_modified = resp.headers.get("Last-Modified", "")
        return override, last_modified

    resp = requests.get(DISCOVERY_API_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    url = data["full_download_file"]["url"]
    head_resp = requests.head(url, timeout=30)
    head_resp.raise_for_status()
    last_modified = head_resp.headers.get("Last-Modified", "")
    return url, last_modified


def _last_modified_to_utc(last_modified_str: str) -> datetime | None:
    """Parse RFC 2822 Last-Modified header to UTC datetime."""
    if not last_modified_str:
        return None
    try:
        return parsedate_to_datetime(last_modified_str).astimezone(timezone.utc)
    except Exception:
        return None


def _release_tag(source_observed_at: datetime | None) -> str:
    """R2 ``release=`` partition value — the dump's publish date (UTC).

    Falls back to the current UTC date when the upstream omits Last-Modified.
    """
    dt = source_observed_at or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _check_idempotent(conn, source_observed_at: datetime | None) -> bool:
    """Return True if the dump is unchanged since the last completed run."""
    if source_observed_at is None:
        return False
    row = conn.execute(
        """
        SELECT source_observed_at
        FROM ops.usaspending_db_dump_ingest_runs
        WHERE status = 'completed'
        ORDER BY started_at DESC
        LIMIT 1
        """,
    ).fetchone()
    if row is None:
        return False
    prior: datetime = row[0]
    if prior.tzinfo is None:
        prior = prior.replace(tzinfo=timezone.utc)
    delta = abs((source_observed_at - prior).total_seconds())
    return delta < 60  # within 1 minute → same dump


def _insert_ledger_row(conn, status: str, source_observed_at: datetime | None, source_download_url: str) -> str:
    """Insert a new ledger row; return the run_id."""
    row = conn.execute(
        """
        INSERT INTO ops.usaspending_db_dump_ingest_runs
            (source_observed_at, source_download_url, status)
        VALUES (%s, %s, %s)
        RETURNING run_id
        """,
        (source_observed_at, source_download_url, status),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _update_ledger_row(
    conn,
    run_id: str,
    status: str,
    tables_extracted: dict[str, int] | None = None,
    total_rows_written: int | None = None,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE ops.usaspending_db_dump_ingest_runs
        SET status              = %s,
            completed_at        = NOW(),
            tables_extracted    = %s,
            total_rows_written  = %s,
            error_message       = %s
        WHERE run_id = %s::uuid
        """,
        (
            status,
            json.dumps(tables_extracted) if tables_extracted else None,
            total_rows_written,
            error_message,
            run_id,
        ),
    )


def _download_dump(url: str, cache_dir: str) -> str:
    """Download the dump ZIP to the cache volume with HTTP-Range resume + retries.

    The 172 GB dump downloads from files.usaspending.gov via CloudFront-fronted
    S3, which has been observed to drop the connection mid-transfer on long
    streams (ChunkedEncodingError: IncompleteRead). Strategy:
      1. HEAD-probe to get the upstream Content-Length (the "expected" size).
      2. Check local-file size; if it matches expected, treat as cached.
      3. If local-file size < expected, resume via HTTP Range header from
         that byte offset (S3 supports Accept-Ranges by default).
      4. Wrap the download loop in a retry-with-backoff (exponential, max 5
         attempts) for ChunkedEncodingError / ConnectionError / read timeouts.

    Uses ``requests`` (with certifi's bundled CA store) instead of urllib —
    debian_slim's system CA bundle has been observed to fail SSL verification
    on the USAspending Entrust cert chain.
    """
    import time

    import requests
    from requests.exceptions import ChunkedEncodingError, ConnectionError as ReqConnErr, ReadTimeout

    filename = url.split("/")[-1]
    local_path = os.path.join(cache_dir, filename)
    os.makedirs(cache_dir, exist_ok=True)

    # 1. HEAD-probe upstream for expected size.
    head = requests.head(url, allow_redirects=True, timeout=30)
    head.raise_for_status()
    expected_size = int(head.headers.get("Content-Length", "0"))
    if expected_size == 0:
        raise RuntimeError(f"upstream Content-Length missing or zero for {url}")
    logger.info("upstream expected size: %d bytes (~%.1f GB)", expected_size, expected_size / 1e9)

    # 2. Local-cache check.
    if os.path.exists(local_path):
        local_size = os.path.getsize(local_path)
        if local_size == expected_size:
            logger.info("dump already fully cached at %s — skipping download", local_path)
            return local_path
        logger.info("partial cache at %s (%d / %d bytes, %.1f%%); resuming via Range",
                    local_path, local_size, expected_size, 100.0 * local_size / expected_size)
    else:
        local_size = 0

    # 3. Download (or resume) with retries.
    max_attempts = 5
    for attempt in range(1, max_attempts + 1):
        try:
            local_size = os.path.getsize(local_path) if os.path.exists(local_path) else 0
            if local_size >= expected_size:
                logger.info("local size %d >= expected %d, done", local_size, expected_size)
                break

            headers = {}
            mode = "wb"
            if local_size > 0:
                headers["Range"] = f"bytes={local_size}-"
                mode = "ab"
                logger.info("attempt %d/%d: resuming from byte %d", attempt, max_attempts, local_size)
            else:
                logger.info("attempt %d/%d: starting fresh download", attempt, max_attempts)

            with requests.get(url, headers=headers, stream=True, timeout=(30, 600)) as resp:
                # 206 Partial Content for Range, 200 for full
                if resp.status_code not in (200, 206):
                    resp.raise_for_status()
                with open(local_path, mode) as f:
                    bytes_this_attempt = 0
                    last_log = time.time()
                    for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                        if chunk:
                            f.write(chunk)
                            bytes_this_attempt += len(chunk)
                            now = time.time()
                            if now - last_log > 30:
                                total = local_size + bytes_this_attempt
                                logger.info("  progress: %d / %d bytes (%.1f%%)",
                                            total, expected_size, 100.0 * total / expected_size)
                                last_log = now

            final_size = os.path.getsize(local_path)
            if final_size == expected_size:
                logger.info("download complete: %s (%d bytes)", local_path, final_size)
                return local_path
            else:
                logger.warning("attempt %d ended with %d / %d bytes; retrying",
                               attempt, final_size, expected_size)

        except (ChunkedEncodingError, ReqConnErr, ReadTimeout, OSError) as exc:
            logger.warning("attempt %d failed: %s: %s; backoff + retry",
                           attempt, type(exc).__name__, exc)
            if attempt >= max_attempts:
                raise
            backoff = min(60, 2 ** attempt)
            logger.info("sleeping %ds before retry", backoff)
            time.sleep(backoff)

    raise RuntimeError(f"failed to download dump after {max_attempts} attempts")


def _extract_dump(zip_path: str, extract_dir: str) -> None:
    """Extract the pg_dump -Fd directory from the ZIP archive."""
    if os.path.exists(os.path.join(extract_dir, "toc.dat")):
        logger.info("dump directory already extracted at %s — skipping", extract_dir)
        return

    logger.info("extracting %s → %s", zip_path, extract_dir)
    os.makedirs(extract_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        # pg_dump -Fd archives contain files at the top level or in a single
        # directory layer.  Detect and strip a single top-level directory.
        names = zf.namelist()
        top_level_dirs = {n.split("/")[0] for n in names if "/" in n}
        strip_prefix = ""
        if len(top_level_dirs) == 1:
            strip_prefix = list(top_level_dirs)[0] + "/"

        for member in zf.infolist():
            member_path = member.filename
            if strip_prefix and member_path.startswith(strip_prefix):
                member_path = member_path[len(strip_prefix):]
            if not member_path:
                continue
            dest = os.path.join(extract_dir, member_path)
            if member.is_dir():
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    logger.info("extraction complete: %s", extract_dir)

    # Delete the ZIP to free ~172 GB disk space during the decode phase
    os.remove(zip_path)
    logger.info("deleted zip %s to free disk", zip_path)


def _infer_schema_from_toc(
    dump_dir: str, pg_schema: str, pg_table: str
) -> tuple[list[str], list[pa.DataType]]:
    """Infer column names from a pg_restore COPY header for ``<pg_schema>.<pg_table>``.

    USAspending's dump uses schema-qualified names (rpt.subaward_search,
    public.toptier_agency, etc.), so we MUST pass ``-n <schema> -t <table>``
    to pg_restore. Without ``-n``, pg_restore's ``-t`` only matches when
    no two tables in different schemas share the same name — too fragile.

    Returns (column_names, column_types). Every column lands as ``large_utf8``
    — raw landing is intentionally untyped (PG COPY text decoded straight to
    string; downstream projections do the typing).
    Returns ([], []) on failure (logs stderr + stdout sample for diagnostics).
    """
    import re

    qualified = f"{pg_schema}.{pg_table}"
    proc = subprocess.Popen(
        [
            "pg_restore", "--data-only",
            "-n", pg_schema, "-t", pg_table,
            "-f", "-", dump_dir,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    copy_re = re.compile(r"^COPY\s+\S+\s+\((.*)\)\s+FROM\s+stdin;", re.IGNORECASE)
    col_names: list[str] = []
    sample_lines: list[str] = []
    lines_read = 0
    for raw_line in proc.stdout:  # type: ignore[union-attr]
        lines_read += 1
        if lines_read > 200:  # safety: bail after 200 preamble lines
            break
        try:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            continue
        if lines_read <= 30:
            sample_lines.append(line[:200])
        m = copy_re.match(line)
        if m:
            col_names = [c.strip() for c in m.group(1).split(",")]
            logger.info(
                "  inferred %d cols for %s from COPY header (first 5: %s)",
                len(col_names), qualified, col_names[:5],
            )
            break

    if not col_names:
        try:
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace")[:1500] if proc.stderr else ""
        except Exception:
            stderr_text = ""
        logger.warning(
            "  no COPY header for %s after %d lines\n    stdout sample (first %d lines):\n      %s\n    stderr: %s",
            qualified, lines_read, len(sample_lines),
            "\n      ".join(sample_lines[:10]),
            stderr_text,
        )

    proc.kill()
    proc.wait()
    return col_names, [pa.large_utf8()] * len(col_names)


def _land_table_to_parquet(
    table_cfg: dict[str, Any],
    dump_dir: str,
    s3,
    bucket: str,
    release: str,
    run_id: str,
    source_row_count: int,
) -> int:
    """Decode one table via pg_restore → COPY-text parser → chunked R2 Parquet.

    Streams the decoded Arrow RecordBatches into ZSTD-compressed Parquet parts
    of up to ROWS_PER_PART rows each, uploading + deleting each part as it
    completes (bounded local disk). Returns the verified row count.

    Updates the per-table ledger via short-lived connections (``_stamp_table``):
    running on start, completed/failed on finish — no DB connection is held
    across the multi-hour decode. Raises on any failure.
    """
    import pyarrow.parquet as pq
    from scripts._lib.pg_copy_text_parser import parse_copy_stream

    table_name = table_cfg["name"]
    pg_schema = table_cfg["pg_schema"]
    pg_table = table_cfg["pg_table"]
    key_prefix = f"{R2_PARQUET_PREFIX}/{table_name}/release={release}"

    logger.info("--- START table=%s → s3://%s/%s/", table_name, bucket, key_prefix)
    _stamp_table(run_id, table_name, "running", source_row_count=source_row_count)

    # Infer column names from the actual COPY header
    col_names, col_types = _infer_schema_from_toc(dump_dir, pg_schema, pg_table)
    if not col_names:
        err = f"No COPY header found for {pg_schema}.{pg_table}"
        _stamp_table(run_id, table_name, "failed",
                     source_row_count=source_row_count, error_message=err)
        raise RuntimeError(err)
    arrow_schema = pa.schema([pa.field(name, pa.large_utf8()) for name in col_names])

    # Preemption resume: pick up from the last part a prior attempt landed.
    # Modal can preempt a multi-hour worker and restart it with the same input;
    # without resume, every preemption restarts the decode from row 0. Parts
    # already in R2 are KEPT — the decoder skips those rows (a cheap raw-line
    # skip) and re-decodes only the last landed part onward.
    resume_part_idx = _resume_part_index(s3, bucket, key_prefix)
    skip_rows = resume_part_idx * ROWS_PER_PART
    if resume_part_idx > 0:
        logger.info("  resuming table=%s at part-%05d (%d rows already landed)",
                    table_name, resume_part_idx, skip_rows)

    staging = os.path.join(PARQUET_STAGING_DIR, table_name)
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    proc = subprocess.Popen(
        [
            "pg_restore", "--data-only",
            "-n", pg_schema, "-t", pg_table,
            "-f", "-", dump_dir,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    rows_written = skip_rows
    part_idx = resume_part_idx
    writer = None
    local_path = ""
    rows_in_part = 0

    def _flush_part() -> None:
        nonlocal writer, local_path, rows_in_part, part_idx
        assert writer is not None
        writer.close()
        writer = None
        key = f"{key_prefix}/part-{part_idx:05d}.parquet"
        s3.upload_file(
            local_path, bucket, key,
            ExtraArgs={"ContentType": "application/x-parquet"},
        )
        os.remove(local_path)
        logger.info("  uploaded %s (%d rows)", key, rows_in_part)
        rows_in_part = 0
        part_idx += 1

    try:
        batch_iter = parse_copy_stream(
            proc.stdout, col_types, col_names, skip_rows=skip_rows,  # type: ignore[arg-type]
        )
        for batch in batch_iter:
            if writer is None:
                local_path = os.path.join(staging, f"part-{part_idx:05d}.parquet")
                writer = pq.ParquetWriter(
                    local_path, arrow_schema,
                    compression="zstd", compression_level=PARQUET_COMPRESSION_LEVEL,
                )
            writer.write_batch(batch)
            rows_in_part += batch.num_rows
            rows_written += batch.num_rows
            if rows_in_part >= ROWS_PER_PART:
                _flush_part()
        if writer is not None:
            _flush_part()
    except Exception as write_exc:
        err = f"{type(write_exc).__name__}: {write_exc}"
        _stamp_table(
            run_id, table_name, "failed",
            row_count=rows_written, source_row_count=source_row_count,
            error_message=err[:500],
        )
        raise
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
        proc.stdout.close()  # type: ignore[union-attr]
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        shutil.rmtree(staging, ignore_errors=True)

    # Write-then-verify (mechanism 2): the count decoded + written must equal
    # the independently-counted source row count. A truncated pg_restore decode
    # (subprocess died mid-stream) is caught here.
    if rows_written != source_row_count:
        err = (
            f"write-then-verify FAILED for {table_name}: "
            f"wrote {rows_written} rows but source had {source_row_count} rows"
        )
        logger.error(err)
        _stamp_table(
            run_id, table_name, "failed",
            row_count=rows_written, source_row_count=source_row_count,
            error_message=err,
        )
        raise RuntimeError(err)

    _stamp_table(
        run_id, table_name, "completed",
        row_count=rows_written, source_row_count=source_row_count,
    )
    logger.info("--- DONE table=%s rows=%d parts=%d", table_name, rows_written, part_idx)
    return rows_written


# --------------------------------------------------------------------------- #
# Modal functions — coordinator + per-table workers (fan-out)                   #
# --------------------------------------------------------------------------- #

# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=WORKER_MEMORY_MB,
    cpu=WORKER_CPU,
    timeout=FUNC_TIMEOUT,
    volumes={CACHE_DIR: cache_vol},
    # Modal preempts long-running containers — observed ~every few minutes on
    # the 2026-05-21 runs, fast enough that a multi-hour decode cannot make
    # net forward progress under any checkpoint scheme. nonpreemptible
    # guarantees the worker runs straight through (3x price multiplier on
    # CPU+memory — acceptable for a one-time ~510M-row bulk ingest).
    nonpreemptible=True,
)
def land_one_table(arg: dict[str, Any]) -> dict[str, Any]:
    """Worker — decode + land ONE table to R2 Parquet.

    One container per table; the coordinator fans these out in parallel via
    ``.map()``. Never raises — every outcome (completed / failed) is written to
    the per-table ledger and returned as a status dict, so one table's failure
    can't abort its siblings.

    ``arg`` keys: run_id, table_cfg, release, source_observed_at (datetime|None).
    """
    _bridge_database_url()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    run_id = arg["run_id"]
    table_cfg = arg["table_cfg"]
    release = arg["release"]
    source_observed_at = arg["source_observed_at"]
    tname = table_cfg["name"]
    pg_schema = table_cfg["pg_schema"]
    pg_table = table_cfg["pg_table"]

    logger.info("=== worker START table=%s run_id=%s ===", tname, run_id)
    try:
        s3 = _r2_client()

        # Source row count — reuse a prior (preempted) attempt's recorded count
        # if the ledger already has it; otherwise scan the dump.
        source_count = _ledger_source_count(run_id, tname)
        if source_count is None:
            source_count = _count_rows_from_dump(DUMP_DIR, pg_schema, pg_table)
        if source_count < 0:
            err = f"No COPY block found in dump for {pg_schema}.{pg_table}"
            _stamp_table(run_id, tname, "failed", error_message=err)
            return {"table": tname, "status": "failed", "row_count": -1, "error": err}

        # Validity-keyed skip gate — resume cheaply on re-dispatch.
        with _connect() as conn:
            skip = _should_skip_table(
                conn, s3, R2_BUCKET, tname, source_observed_at, release, source_count,
            )
        if skip:
            _stamp_table(run_id, tname, "completed",
                         row_count=source_count, source_row_count=source_count)
            logger.info("=== worker SKIP table=%s — already landed for this dump (%d rows) ===",
                        tname, source_count)
            return {"table": tname, "status": "completed", "row_count": source_count}

        row_count = _land_table_to_parquet(
            table_cfg, DUMP_DIR, s3, R2_BUCKET, release, run_id, source_count,
        )
        logger.info("=== worker DONE table=%s rows=%d ===", tname, row_count)
        return {"table": tname, "status": "completed", "row_count": row_count}
    except Exception as exc:  # noqa: BLE001 — workers must never raise
        logger.error("worker table=%s FAILED: %s", tname, exc, exc_info=True)
        try:
            _stamp_table(run_id, tname, "failed",
                         error_message=f"{type(exc).__name__}: {exc}"[:500])
        except Exception:
            pass
        return {"table": tname, "status": "failed", "row_count": -1, "error": str(exc)[:500]}


# retry-policy: script-loop-FIXME-resume-download-loop
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    memory=COORD_MEMORY_MB,
    cpu=COORD_CPU,
    timeout=FUNC_TIMEOUT,
    volumes={CACHE_DIR: cache_vol},
    # The coordinator blocks ~hours on .map(); a preemption would restart the
    # whole run with a fresh run_id and re-fan-out. Pin it non-preemptible too.
    nonpreemptible=True,
)
def ingest(dump_url_override: str | None = None) -> dict[str, Any]:
    """Coordinator — prepare the dump, fan the per-table decode out, finalize.

    Runs remotely so ``modal run --detach`` survives a laptop disconnect.
    Phases:
      1. Resolve the dump URL, idempotency check, insert the run ledger row.
      2. Download + extract the dump if the Volume cache is cold; preflight;
         insert the 12 per-table 'pending' rows; mark the 2 unmapped 'skipped'.
      3. ``land_one_table.map(...)`` — fan the 10 mapped tables across 10
         worker containers, in parallel. Blocks until all return.
      4. Aggregate, set the run-level status, exit nonzero on any failure.

    Args:
        dump_url_override: Optional override URL for the dump ZIP.

    Returns:
        dict with status, run_id, tables_extracted, total_rows_written.
    """
    _bridge_database_url()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logger.info("=== USAspending DB-dump → raw Parquet landing START (fan-out) ===")

    import sys as _sys
    _sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    # Phase 1 — resolve + ledger.
    with _connect() as conn:
        dump_url, last_modified_str = _resolve_dump_url(dump_url_override)
        logger.info("dump_url=%s last_modified=%s", dump_url, last_modified_str)
        source_observed_at = _last_modified_to_utc(last_modified_str)

        if _check_idempotent(conn, source_observed_at):
            run_id = _insert_ledger_row(conn, "no_change", source_observed_at, dump_url)
            logger.info("dump unchanged (source_observed_at=%s) — no_change, run_id=%s",
                        source_observed_at, run_id)
            return {"status": "no_change", "run_id": run_id}

        run_id = _insert_ledger_row(conn, "running", source_observed_at, dump_url)
        logger.info("started run_id=%s", run_id)

    hb_cm = HeartbeatLoop(
        cron_app=app.name,
        cron_function="ingest",
        run_id=run_id,
    )
    hb_cm.__enter__()
    hb_cm.set_stage("phase_1_resolve_and_ledger", {"dump_url": dump_url})

    release = _release_tag(source_observed_at)
    logger.info("release tag = %s", release)

    try:
        # Phase 2 — prepare. Download + extract only if the Volume cache is cold.
        hb_cm.set_stage("phase_2_download_and_extract")
        toc_path = os.path.join(DUMP_DIR, "toc.dat")
        if os.path.exists(toc_path):
            logger.info("extracted dump dir already cached at %s — skipping download + extract",
                        DUMP_DIR)
        else:
            zip_path = _download_dump(dump_url, CACHE_DIR)
            _extract_dump(zip_path, DUMP_DIR)
            # Persist the extraction so the fanned-out worker containers see it.
            cache_vol.commit()

        # Preflight (run AFTER extraction so the TOC is on disk). Raises and
        # fails the run if any mapped table is missing.
        _preflight_dump_completeness(DUMP_DIR)

        # Insert all 12 'pending' rows; route the 2 unmapped ones to 'skipped'.
        with _connect() as conn:
            _insert_table_ledger_rows(conn, run_id, [t["name"] for t in TABLE_CONFIG])
            for cfg in TABLE_CONFIG:
                if not cfg.get("pg_schema") or not cfg.get("pg_table"):
                    _update_table_ledger(conn, run_id, cfg["name"], "skipped",
                                         row_count=0, source_row_count=0)
                    logger.info("table=%s unmapped (not in this dump version) — skipped",
                                cfg["name"])

        # Phase 3 — fan the mapped tables out, one worker container each.
        worker_args = [
            {
                "run_id": run_id,
                "table_cfg": cfg,
                "release": release,
                "source_observed_at": source_observed_at,
            }
            for cfg in MAPPED_TABLE_CONFIGS
        ]
        hb_cm.set_stage("phase_3_fanout_workers", {"workers": len(worker_args)})
        logger.info("fanning out %d table workers in parallel", len(worker_args))
        results = list(land_one_table.map(worker_args, return_exceptions=True))

        # Phase 4 — aggregate.
        tables_extracted: dict[str, int] = {
            cfg["name"]: 0 for cfg in TABLE_CONFIG
            if not cfg.get("pg_schema") or not cfg.get("pg_table")
        }
        total_rows = 0
        failed_tables: list[str] = []
        for cfg, res in zip(MAPPED_TABLE_CONFIGS, results):
            tname = cfg["name"]
            if isinstance(res, dict) and res.get("status") == "completed":
                tables_extracted[tname] = res["row_count"]
                total_rows += res["row_count"]
            else:
                tables_extracted[tname] = -1
                failed_tables.append(tname)
                # A worker container that died (OOM / Modal infra / timeout)
                # surfaces from .map() as an exception — stamp its ledger row.
                if isinstance(res, BaseException):
                    logger.error("worker for table=%s died: %s", tname, res)
                    try:
                        _stamp_table(
                            run_id, tname, "failed",
                            error_message=f"worker container died: {type(res).__name__}: {res}"[:500],
                        )
                    except Exception:
                        pass

        run_status = "failed" if failed_tables else "completed"
        with _connect() as conn:
            _update_ledger_row(
                conn, run_id,
                status=run_status,
                tables_extracted=tables_extracted,
                total_rows_written=total_rows,
                error_message=f"failed tables: {failed_tables}" if failed_tables else None,
            )

        if failed_tables:
            logger.error(
                "=== USAspending DB-dump landing PARTIAL FAILURE: %d table(s) failed: %s ===",
                len(failed_tables), failed_tables,
            )
            logger.info("run_id=%s total_rows=%d", run_id, total_rows)
            hb_cm.__exit__(None, None, None)
            # Exit nonzero so Modal and the CLI wrapper surface the failure.
            sys.exit(1)

        logger.info("=== USAspending DB-dump → raw Parquet landing COMPLETE ===")
        logger.info("run_id=%s total_rows=%d tables=%s", run_id, total_rows,
                    list(tables_extracted.keys()))
        hb_cm.__exit__(None, None, None)
        return {
            "status": "completed",
            "run_id": run_id,
            "tables_extracted": tables_extracted,
            "total_rows_written": total_rows,
        }

    except SystemExit:
        hb_cm.__exit__(None, None, None)
        raise
    except Exception as exc:
        err_msg = f"{type(exc).__name__}: {exc}"
        logger.error("ingest FAILED: %s", err_msg, exc_info=True)
        try:
            with _connect() as conn:
                _update_ledger_row(conn, run_id, status="failed", error_message=err_msg)
        except Exception:
            pass
        hb_cm.__exit__(None, None, None)
        raise


# --------------------------------------------------------------------------- #
# @app.local_entrypoint — enables `modal run --detach .../run`                 #
# --------------------------------------------------------------------------- #

@app.local_entrypoint()
def run(dump_url: str = "") -> None:
    """Local entrypoint for ``modal run --detach`` dispatch.

    Usage:
        modal run --detach apps/data-engine-x/modal/usaspending_db_dump_to_r2.py::run
        modal run --detach apps/data-engine-x/modal/usaspending_db_dump_to_r2.py::run --dump-url <url>

    The coordinator (and its fanned-out workers) run remotely; --detach lets
    the local process exit without waiting for the multi-hour job (L47).
    """
    url_override = dump_url if dump_url else None
    logger.info("dispatching ingest coordinator via Modal; dump_url_override=%s", url_override)
    ingest.remote(dump_url_override=url_override)
