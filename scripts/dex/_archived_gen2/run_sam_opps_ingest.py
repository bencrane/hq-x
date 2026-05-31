"""SAM.gov Contract Opportunities — bulk CSV → R2 Parquet ingest.

Handles three modes:

  --mode active            Daily full snapshot of currently-Active=Yes notices
                           (~80k rows; 30-50 MB ZSTD Parquet).
                           R2: sam-gov-opps/active/snapshot={YYYY-MM-DD}/{run_id}.parquet.zst

  --mode archived --fy N   Single fiscal-year archived snapshot (~10-25k
                           rows partial / full FY).
                           R2: sam-gov-opps/archived/fy={N}/snapshot={YYYY-MM-DD}/{run_id}.parquet.zst

  --backfill-historical    Loops --mode archived for --start-fy through
                           --end-fy. Skips FYs already ingested today.

Wrapped by:
  modal/sam_opps_active_daily_app.py   (cron 12:00 UTC daily)
  modal/sam_opps_archived_weekly_app.py (cron 14:00 UTC Mon)

Idempotency: HEAD-check the source URL's Last-Modified; if unchanged from
the most recent 'completed' run for this (source_id, feed_name, slice),
record outcome='probe_said_no_change' and skip the download. The active
feed's per-day partition prevents collision with prior days regardless.

CSV parsing: source CSV is **Windows-1252 encoded** (curly apostrophes
\\x92 etc. in Description text); DuckDB's CSV reader only handles UTF-8
and silently drops rows with non-UTF8 bytes when ignore_errors is set
(observed loss: ~17% of rows on 2026-05-09 smoke). The script transcodes
cp1252 → UTF-8 via iconv before DuckDB sees the file. iconv ships in
Debian glibc (Modal's debian_slim image has it).

DuckDB read_csv: header=TRUE + all_varchar=TRUE + sample_size=-1 +
quote='"' + escape='"'. Description carries embedded newlines (~17% of
rows); DuckDB handles those correctly with the explicit quote/escape.
Column count == 52 (47 source + 5 provenance) is the post-write safety
check.

Type discipline (per L2 amended 2026-05-09): every output column is cast
to VARCHAR before write. The downstream RW source DDL declares all cols
CHARACTER VARYING; both sides match. Type coercion happens in essentials
MV via L29 regex-CASE pattern.

Usage (CLI standalone — local backfill):

    cd ~/hq-all && \\
      doppler run --project hq-all --config prd --command \\
        'uv run python apps/data-engine-x/scripts/run_sam_opps_ingest.py \\
          --mode archived --fy 2024'

    cd ~/hq-all && \\
      doppler run --project hq-all --config prd --command \\
        'uv run python apps/data-engine-x/scripts/run_sam_opps_ingest.py \\
          --backfill-historical --start-fy 2018 --end-fy 2025'

    # Active feed local smoke (Modal will run this on cron):
    cd ~/hq-all && \\
      doppler run --project hq-all --config prd --command \\
        'uv run python apps/data-engine-x/scripts/run_sam_opps_ingest.py \\
          --mode active'
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_sam_opps_ingest")

R2_BUCKET = "dex-raw-landing-zone"
SOURCE_ID_ACTIVE = "sam_opps_active"
FEED_NAME_ACTIVE = "active"
SOURCE_ID_ARCHIVED = "sam_opps_archived"
FEED_NAME_ARCHIVED = "fy_archive"

ACTIVE_URL = (
    "https://sam.gov/api/prod/fileextractservices/v1/api/download/"
    "Contract%20Opportunities/datagov/ContractOpportunitiesFullCSV.csv"
    "?privacy=Public"
)


def archived_url(fy: int) -> str:
    return (
        "https://sam.gov/api/prod/fileextractservices/v1/api/download/"
        "Contract%20Opportunities/Archived%20Data/"
        f"FY{fy}_archived_opportunities.csv?privacy=Public"
    )


# 47 columns from the SAM Contract Opportunities CSV. Order MUST match the
# CSV header verbatim — DuckDB infers column names from header but the typed
# projection below references them by name. List preserved here for the RW
# source DDL to mirror exactly.
SAM_OPPS_COLUMNS_RAW = [
    "NoticeId", "Title", "Sol#", "Department/Ind.Agency", "CGAC", "Sub-Tier",
    "FPDS Code", "Office", "AAC Code", "PostedDate", "Type", "BaseType",
    "ArchiveType", "ArchiveDate", "SetASideCode", "SetASide", "ResponseDeadLine",
    "NaicsCode", "ClassificationCode", "PopStreetAddress", "PopCity", "PopState",
    "PopZip", "PopCountry", "Active", "AwardNumber", "AwardDate", "Award$",
    "Awardee", "PrimaryContactTitle", "PrimaryContactFullname",
    "PrimaryContactEmail", "PrimaryContactPhone", "PrimaryContactFax",
    "SecondaryContactTitle", "SecondaryContactFullname", "SecondaryContactEmail",
    "SecondaryContactPhone", "SecondaryContactFax", "OrganizationType", "State",
    "City", "ZipCode", "CountryCode", "AdditionalInfoLink", "Link", "Description",
]

# snake_case version for downstream DDL / MVs. Mapping is applied at DuckDB
# transform time (SELECT ... AS snake_case).
SAM_OPPS_COLUMNS_SNAKE = [
    "notice_id", "title", "sol_num", "department_agency", "cgac", "sub_tier",
    "fpds_code", "office", "aac_code", "posted_date", "notice_type", "base_type",
    "archive_type", "archive_date", "set_aside_code", "set_aside", "response_deadline",
    "naics_code", "classification_code", "pop_street_address", "pop_city", "pop_state",
    "pop_zip", "pop_country", "active_flag", "award_number", "award_date", "award_amount",
    "awardee", "primary_contact_title", "primary_contact_fullname",
    "primary_contact_email", "primary_contact_phone", "primary_contact_fax",
    "secondary_contact_title", "secondary_contact_fullname", "secondary_contact_email",
    "secondary_contact_phone", "secondary_contact_fax", "organization_type", "org_state",
    "org_city", "org_zip", "org_country", "additional_info_link", "link", "description",
]
assert len(SAM_OPPS_COLUMNS_RAW) == len(SAM_OPPS_COLUMNS_SNAKE) == 47


# ──────────────────────────────────────────────────────────────────────────
# bulk_ingest ledger helpers
# ──────────────────────────────────────────────────────────────────────────

def _db_url() -> str:
    url = (
        os.environ.get("DEX_DB_URL_DIRECT")
        or os.environ.get("DEX_DB_URL_POOLED")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError(
            "DEX_DB_URL_DIRECT / DEX_DB_URL_POOLED / DATABASE_URL not set"
        )
    return url


def _record_run_pending(
    *,
    run_id: uuid.UUID,
    source_id: str,
    feed_name: str,
    feed_date: date,
    r2_object_key: str,
    started_at: datetime,
    evidence: dict[str, Any],
) -> None:
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bulk_ingest.feed_ingest_runs (
                    run_id, source_id, feed_name, feed_date, attempt,
                    status, outcome, started_at, landing_zone, r2_bucket,
                    r2_object_key, payload_format, evidence
                ) VALUES (
                    %s, %s, %s, %s, 1,
                    'running', 'never_ran', %s, 'r2', %s,
                    %s, 'parquet_zstd', %s::jsonb
                )
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE SET
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at,
                    r2_object_key = EXCLUDED.r2_object_key,
                    evidence = EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    str(run_id), source_id, feed_name, feed_date.isoformat(),
                    started_at, R2_BUCKET, r2_object_key,
                    json.dumps(evidence, default=str),
                ),
            )
        conn.commit()


def _record_run_terminal(
    *,
    run_id: uuid.UUID,
    source_id: str,
    feed_name: str,
    feed_date: date,
    status: str,
    outcome: str,
    started_at: datetime,
    rows_loaded: int | None,
    payload_bytes: int | None,
    r2_object_key: str | None,
    error_class: str | None,
    error_message: str | None,
    evidence: dict[str, Any],
) -> None:
    import psycopg
    completed_at = datetime.now(timezone.utc)
    duration = (completed_at - started_at).total_seconds()
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bulk_ingest.feed_ingest_runs SET
                    status = %s,
                    outcome = %s,
                    completed_at = %s,
                    duration_seconds = %s,
                    rows_loaded = %s,
                    payload_bytes = %s,
                    r2_object_key = COALESCE(%s, r2_object_key),
                    error_class = %s,
                    error_message = %s,
                    evidence = COALESCE(evidence, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE run_id = %s AND source_id = %s
                  AND feed_name = %s AND attempt = 1
                """,
                (
                    status, outcome, completed_at, duration,
                    rows_loaded, payload_bytes, r2_object_key,
                    error_class, error_message,
                    json.dumps(evidence, default=str),
                    str(run_id), source_id, feed_name,
                ),
            )
        conn.commit()


def _last_completed_observed_at(
    *,
    source_id: str,
    feed_name: str,
    feed_slice: str,
) -> str | None:
    """Returns evidence.source_observed_at for the most recent 'completed'
    run that matches feed_slice (e.g., 'fy=2024' or 'snapshot' for active).
    Used for HEAD-Last-Modified probe shortcut."""
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT evidence->>'source_observed_at'
                FROM bulk_ingest.feed_ingest_runs
                WHERE source_id = %s AND feed_name = %s
                  AND status = 'completed'
                  AND (evidence->>'feed_slice') = %s
                ORDER BY started_at DESC LIMIT 1
                """,
                (source_id, feed_name, feed_slice),
            )
            row = cur.fetchone()
            return row[0] if row else None


# ──────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ──────────────────────────────────────────────────────────────────────────

def _head_last_modified(url: str) -> str | None:
    """Returns the Last-Modified of the destination after one redirect hop.
    SAM's URL is a 303 → S3 presigned URL; both legs may carry Last-Modified."""
    import httpx
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            r = client.head(url, headers={"User-Agent": "Mozilla/5.0"})
            return r.headers.get("Last-Modified") or r.headers.get("last-modified")
    except Exception as exc:
        logger.warning("HEAD probe failed for %s: %s", url[:80], exc)
        return None


def _download_to_tmp(url: str) -> Path:
    """Stream-download the CSV to a tempfile. Returns the path."""
    import httpx
    fd, tmp_path_str = tempfile.mkstemp(suffix=".csv", prefix="sam_opps_")
    os.close(fd)
    tmp_path = Path(tmp_path_str)
    bytes_total = 0
    t0 = time.time()
    with httpx.Client(follow_redirects=True, timeout=300.0) as client:
        with client.stream(
            "GET", url, headers={"User-Agent": "Mozilla/5.0"}
        ) as resp:
            resp.raise_for_status()
            with tmp_path.open("wb") as f:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    f.write(chunk)
                    bytes_total += len(chunk)
    elapsed = time.time() - t0
    logger.info(
        "downloaded %s bytes in %.1fs to %s",
        f"{bytes_total:,}", elapsed, tmp_path,
    )
    return tmp_path


# ──────────────────────────────────────────────────────────────────────────
# DuckDB CSV → Parquet transform
# ──────────────────────────────────────────────────────────────────────────

def _transcode_cp1252_to_utf8(src: Path) -> Path:
    """SAM CSV is Windows-1252; transcode to UTF-8 in-place via iconv.
    Returns path to UTF-8 file (a sibling tmp file). Caller cleans up both."""
    import subprocess
    fd, dst_str = tempfile.mkstemp(suffix=".utf8.csv", prefix="sam_opps_")
    os.close(fd)
    dst = Path(dst_str)
    t0 = time.time()
    proc = subprocess.run(
        ["iconv", "-f", "WINDOWS-1252", "-t", "UTF-8", str(src)],
        stdout=open(dst, "wb"), stderr=subprocess.PIPE, check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"iconv cp1252→utf8 failed (exit {proc.returncode}): "
            f"{proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    logger.info(
        "transcoded cp1252→utf8 in %.1fs (src=%s bytes, dst=%s bytes)",
        time.time() - t0, f"{src.stat().st_size:,}", f"{dst.stat().st_size:,}",
    )
    return dst


def _csv_to_parquet_zst(
    *,
    csv_path: Path,
    out_path: Path,
    run_id: uuid.UUID,
    snapshot_date: date,
    source_url: str,
    source_observed_at: str | None,
) -> tuple[int, int]:
    """Read CSV via DuckDB, project to snake_case + provenance cols, all
    cast to VARCHAR per L2 path #1, write ZSTD-internal Parquet to out_path.

    Caller passes the cp1252-encoded CSV path; this function transcodes to
    UTF-8 via iconv first (DuckDB silently drops non-UTF8 rows otherwise).

    Returns (row_count, byte_count)."""
    import duckdb

    utf8_path = _transcode_cp1252_to_utf8(csv_path)
    con = duckdb.connect()
    try:
        # Build the SELECT projection. Each output column is cast to VARCHAR.
        select_lines: list[str] = []
        for raw, snake in zip(SAM_OPPS_COLUMNS_RAW, SAM_OPPS_COLUMNS_SNAKE):
            select_lines.append(f'  CAST("{raw}" AS VARCHAR) AS {snake}')
        # Provenance cols. All VARCHAR per L2.
        select_lines += [
            f"  CAST('{run_id}' AS VARCHAR)             AS _ingest_run_id",
            f"  CAST('{snapshot_date.isoformat()}' AS VARCHAR) AS _snapshot_date",
            f"  CAST('{datetime.now(timezone.utc).isoformat()}' AS VARCHAR) AS _ingested_at",
            f"  CAST({_sql_escape(source_observed_at)} AS VARCHAR) AS _source_observed_at",
            f"  CAST({_sql_escape(source_url)} AS VARCHAR) AS _source_url",
        ]
        select_clause = ",\n".join(select_lines)

        copy_sql = f"""
        COPY (
          SELECT
{select_clause}
          FROM read_csv(
            '{utf8_path}',
            header = TRUE,
            all_varchar = TRUE,
            sample_size = -1,
            quote = '"',
            escape = '"'
          )
        ) TO '{out_path}'
          (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 3, ROW_GROUP_SIZE 100000);
        """
        con.execute(copy_sql)

        # Row count from the just-written Parquet (cheap).
        rows_out = con.execute(
            f"SELECT count(*) FROM read_parquet('{out_path}')"
        ).fetchone()[0]
        # Column count safety check per L9 (47 raw + 5 provenance = 52).
        cols_out = con.execute(
            f"SELECT * FROM read_parquet('{out_path}') LIMIT 0"
        ).description
        ncols = len(cols_out)
        if ncols != 52:
            raise RuntimeError(
                f"column-count safety check FAILED: got {ncols}, expected 52 "
                "(47 SAM cols + 5 provenance)"
            )
    finally:
        con.close()
        if utf8_path.exists():
            utf8_path.unlink(missing_ok=True)
    bytes_out = out_path.stat().st_size
    return int(rows_out), int(bytes_out)


def _sql_escape(s: str | None) -> str:
    if s is None:
        return "NULL"
    return "'" + s.replace("'", "''") + "'"


# ──────────────────────────────────────────────────────────────────────────
# R2 upload
# ──────────────────────────────────────────────────────────────────────────

def _r2_object_key(
    *, kind: str, snapshot_date: date, run_id: uuid.UUID, fy: int | None = None
) -> str:
    # Canonical Hive-style {prefix}/snapshot={date}/data.parquet filename
    # (run_id retained in LandingResult metadata + ledger for traceability;
    # not in the key — one canonical file per (kind, snapshot, fy) tuple;
    # multiple ingest runs on the same day atomic-replace last-write-wins).
    #
    # File extension is plain '.parquet'. The ZSTD compression is INTERNAL
    # column-chunk compression baked into the Parquet container; setting
    # the file extension to .parquet.zst (and a Content-Encoding: zstd
    # response header) misleads RW's S3 connector into treating the body
    # as a zstd-wrapped binary and double-decoding (per the bug found
    # 2026-05-09 — see L42 in lessons-learned).
    del run_id  # silence "unused" lint; intentionally not in path
    if kind == "active":
        return f"sam-gov-opps/active/snapshot={snapshot_date.isoformat()}/data.parquet"
    if kind == "archived":
        if fy is None:
            raise ValueError("fy required for archived kind")
        return (
            f"sam-gov-opps/archived/fy={fy}/snapshot={snapshot_date.isoformat()}/"
            f"data.parquet"
        )
    raise ValueError(f"unknown kind: {kind}")


def _upload_to_r2(local_path: Path, r2_key: str) -> None:
    import boto3
    endpoint = os.environ["R2_ENDPOINT"]
    access_key = os.environ["R2_ACCESS_KEY_ID"]
    secret_key = os.environ["R2_SECRET_ACCESS_KEY"]
    client = boto3.client(
        "s3", endpoint_url=endpoint,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        region_name="auto",
    )
    # ContentType only — do NOT set ContentEncoding=zstd. RW's S3 reader
    # interprets that header as "decompress the body before parsing,"
    # which mangles Parquet files whose ZSTD is internal-column-only.
    # Per L42 (2026-05-09).
    client.upload_file(
        str(local_path), R2_BUCKET, r2_key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )


# ──────────────────────────────────────────────────────────────────────────
# Mode runners
# ──────────────────────────────────────────────────────────────────────────

def run_active(snapshot_date: date | None = None) -> dict[str, Any]:
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date()
    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    url = ACTIVE_URL
    observed = _head_last_modified(url)
    feed_slice = "snapshot"  # active feed has only one slice; date in r2 key
    last_observed = _last_completed_observed_at(
        source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
        feed_slice=feed_slice,
    )
    if observed and last_observed and observed == last_observed:
        # Probe says no change — skip download.
        logger.info(
            "active feed unchanged since last successful run (Last-Modified=%s)",
            observed,
        )
        evidence = {
            "feed_slice": feed_slice,
            "source_observed_at": observed,
            "skip_reason": "head_last_modified_match",
        }
        # Still record the run so the heartbeat shows the job ran.
        r2_key = _r2_object_key(
            kind="active", snapshot_date=snapshot_date, run_id=run_id,
        )
        _record_run_pending(
            run_id=run_id, source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
            feed_date=snapshot_date, r2_object_key=r2_key, started_at=started_at,
            evidence=evidence,
        )
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
            feed_date=snapshot_date, status="completed",
            outcome="probe_said_no_change",
            started_at=started_at, rows_loaded=0, payload_bytes=0,
            r2_object_key=None, error_class=None, error_message=None,
            evidence=evidence,
        )
        return {
            "run_id": str(run_id), "outcome": "probe_said_no_change",
            "rows_loaded": 0, "payload_bytes": 0,
        }

    r2_key = _r2_object_key(
        kind="active", snapshot_date=snapshot_date, run_id=run_id,
    )
    evidence = {
        "feed_slice": feed_slice,
        "source_observed_at": observed,
        "source_url": url,
        "snapshot_date": snapshot_date.isoformat(),
    }
    _record_run_pending(
        run_id=run_id, source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
        feed_date=snapshot_date, r2_object_key=r2_key, started_at=started_at,
        evidence=evidence,
    )

    csv_path: Path | None = None
    out_path: Path | None = None
    try:
        csv_path = _download_to_tmp(url)
        fd, out_path_str = tempfile.mkstemp(suffix=".parquet", prefix="sam_opps_")
        os.close(fd)
        out_path = Path(out_path_str)
        rows_loaded, bytes_out = _csv_to_parquet_zst(
            csv_path=csv_path, out_path=out_path, run_id=run_id,
            snapshot_date=snapshot_date, source_url=url,
            source_observed_at=observed,
        )
        logger.info(
            "wrote %s rows / %s bytes to %s",
            f"{rows_loaded:,}", f"{bytes_out:,}", out_path,
        )
        _upload_to_r2(out_path, r2_key)
        outcome = (
            "succeeded_with_changes" if rows_loaded > 0
            else "succeeded_with_zero_new_rows"
        )
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
            feed_date=snapshot_date, status="completed", outcome=outcome,
            started_at=started_at, rows_loaded=rows_loaded, payload_bytes=bytes_out,
            r2_object_key=r2_key, error_class=None, error_message=None,
            evidence=evidence,
        )
        return {
            "run_id": str(run_id), "outcome": outcome,
            "rows_loaded": rows_loaded, "payload_bytes": bytes_out,
            "r2_object_key": r2_key,
        }
    except Exception as exc:
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ACTIVE, feed_name=FEED_NAME_ACTIVE,
            feed_date=snapshot_date, status="failed", outcome="failed",
            started_at=started_at, rows_loaded=None, payload_bytes=None,
            r2_object_key=None,
            error_class=_classify_exception(exc),
            error_message=str(exc)[:4000], evidence=evidence,
        )
        raise
    finally:
        for p in (csv_path, out_path):
            if p and p.exists():
                p.unlink(missing_ok=True)


def run_archived(fy: int, snapshot_date: date | None = None) -> dict[str, Any]:
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date()
    # The bulk_ingest singleton-inflight unique constraint is on
    # (source_id, feed_name, feed_date). Use the FY's end-of-fiscal-year
    # date (Sep 30 of the FY's calendar-year) as feed_date so each FY's
    # backfill row is distinct in the ledger and the constraint doesn't
    # block parallel/sequential FY backfills.
    feed_date = date(fy, 9, 30)
    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    url = archived_url(fy)
    observed = _head_last_modified(url)
    feed_slice = f"fy={fy}"
    last_observed = _last_completed_observed_at(
        source_id=SOURCE_ID_ARCHIVED, feed_name=FEED_NAME_ARCHIVED,
        feed_slice=feed_slice,
    )
    if observed and last_observed and observed == last_observed:
        logger.info(
            "FY%d archived feed unchanged since last run (Last-Modified=%s)",
            fy, observed,
        )
        evidence = {
            "feed_slice": feed_slice,
            "source_observed_at": observed,
            "fy": fy,
            "skip_reason": "head_last_modified_match",
        }
        r2_key = _r2_object_key(
            kind="archived", snapshot_date=snapshot_date, run_id=run_id, fy=fy,
        )
        _record_run_pending(
            run_id=run_id, source_id=SOURCE_ID_ARCHIVED,
            feed_name=FEED_NAME_ARCHIVED, feed_date=feed_date,
            r2_object_key=r2_key, started_at=started_at, evidence=evidence,
        )
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ARCHIVED,
            feed_name=FEED_NAME_ARCHIVED, feed_date=feed_date,
            status="completed", outcome="probe_said_no_change",
            started_at=started_at, rows_loaded=0, payload_bytes=0,
            r2_object_key=None, error_class=None, error_message=None,
            evidence=evidence,
        )
        return {
            "run_id": str(run_id), "outcome": "probe_said_no_change",
            "rows_loaded": 0, "payload_bytes": 0, "fy": fy,
        }

    r2_key = _r2_object_key(
        kind="archived", snapshot_date=snapshot_date, run_id=run_id, fy=fy,
    )
    evidence = {
        "feed_slice": feed_slice,
        "source_observed_at": observed,
        "source_url": url,
        "snapshot_date": snapshot_date.isoformat(),
        "fy": fy,
    }
    _record_run_pending(
        run_id=run_id, source_id=SOURCE_ID_ARCHIVED, feed_name=FEED_NAME_ARCHIVED,
        feed_date=snapshot_date, r2_object_key=r2_key, started_at=started_at,
        evidence=evidence,
    )

    csv_path: Path | None = None
    out_path: Path | None = None
    try:
        csv_path = _download_to_tmp(url)
        fd, out_path_str = tempfile.mkstemp(
            suffix=".parquet", prefix=f"sam_opps_fy{fy}_",
        )
        os.close(fd)
        out_path = Path(out_path_str)
        rows_loaded, bytes_out = _csv_to_parquet_zst(
            csv_path=csv_path, out_path=out_path, run_id=run_id,
            snapshot_date=snapshot_date, source_url=url,
            source_observed_at=observed,
        )
        logger.info(
            "FY%d wrote %s rows / %s bytes",
            fy, f"{rows_loaded:,}", f"{bytes_out:,}",
        )
        _upload_to_r2(out_path, r2_key)
        outcome = (
            "succeeded_with_changes" if rows_loaded > 0
            else "succeeded_with_zero_new_rows"
        )
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ARCHIVED,
            feed_name=FEED_NAME_ARCHIVED, feed_date=feed_date,
            status="completed", outcome=outcome, started_at=started_at,
            rows_loaded=rows_loaded, payload_bytes=bytes_out,
            r2_object_key=r2_key, error_class=None, error_message=None,
            evidence=evidence,
        )
        return {
            "run_id": str(run_id), "outcome": outcome,
            "rows_loaded": rows_loaded, "payload_bytes": bytes_out,
            "r2_object_key": r2_key, "fy": fy,
        }
    except Exception as exc:
        _record_run_terminal(
            run_id=run_id, source_id=SOURCE_ID_ARCHIVED,
            feed_name=FEED_NAME_ARCHIVED, feed_date=feed_date,
            status="failed", outcome="failed", started_at=started_at,
            rows_loaded=None, payload_bytes=None, r2_object_key=None,
            error_class=_classify_exception(exc),
            error_message=str(exc)[:4000], evidence=evidence,
        )
        raise
    finally:
        for p in (csv_path, out_path):
            if p and p.exists():
                p.unlink(missing_ok=True)


def _classify_exception(exc: BaseException) -> str:
    msg = str(exc).lower()
    typ = type(exc).__name__.lower()
    mod = (type(exc).__module__ or "").lower()
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "boto" in mod or "s3" in mod:
        return "r2_failure"
    if "psycopg" in mod or "operationalerror" in typ:
        return "db_failure"
    if "httpx" in mod or "connection" in typ:
        return "download_failure"
    if typ in {"valueerror", "keyerror", "typeerror", "runtimeerror"}:
        return "parse_failure"
    return "unknown"


# ──────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["active", "archived"])
    p.add_argument("--fy", type=int, help="Fiscal year for --mode archived")
    p.add_argument(
        "--backfill-historical", action="store_true",
        help="Loop --mode archived for --start-fy through --end-fy",
    )
    p.add_argument("--start-fy", type=int, default=2018)
    p.add_argument("--end-fy", type=int, default=2025)
    args = p.parse_args()

    if args.backfill_historical:
        results = []
        for fy in range(args.start_fy, args.end_fy + 1):
            logger.info("=" * 70)
            logger.info("BACKFILL: FY%d", fy)
            try:
                r = run_archived(fy=fy)
                results.append(r)
                logger.info("FY%d done: %s", fy, r["outcome"])
            except Exception as exc:
                logger.error("FY%d FAILED: %s", fy, exc)
                results.append({"fy": fy, "outcome": "failed", "error": str(exc)})
        logger.info("=" * 70)
        logger.info("Backfill summary:")
        for r in results:
            logger.info(
                "  FY%s | outcome=%s | rows=%s",
                r.get("fy"), r.get("outcome"), r.get("rows_loaded"),
            )
        return

    if not args.mode:
        p.error("must specify --mode active|archived OR --backfill-historical")

    if args.mode == "active":
        result = run_active()
    else:
        if args.fy is None:
            p.error("--mode archived requires --fy YYYY")
        result = run_archived(fy=args.fy)

    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
