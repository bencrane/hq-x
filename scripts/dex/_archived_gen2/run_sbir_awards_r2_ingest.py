#!/usr/bin/env python3
"""SBIR + STTR awards bulk-CSV → R2 ZSTD Parquet ingest.

Single static URL — sbir.gov publishes one bulk CSV containing the full
historical SBIR+STTR universe (~219K awards, ~87 MB).

  https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv

Streams the CSV to disk, runs DuckDB read_csv with all_varchar=TRUE, writes
ZSTD Parquet to R2 at
  s3://dex-raw-landing-zone/sbir/snapshot={YYYY-MM-DD}/awards.parquet

Adds three provenance columns (snapshot_date, source_etag, source_last_modified)
to every Parquet row — 44 columns out vs 41 source columns in.

Audit: one row per ingest in ops.sbir_r2_ingest_runs.

Idempotency at snapshot-key level: re-running for the same UTC date overwrites
the same R2 key with identical content. Monthly Modal cron writes a fresh
date-stamped key so RW S3_V2 (future) match_pattern='snapshot=*' picks each up.

See directive ~/Desktop/hq/directives/2026-05-10-sbir-sttr-awards-r2-ingest.md.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run --with httpx --with duckdb --with "psycopg[binary]" --with boto3 \\
      python apps/data-engine-x/scripts/run_sbir_awards_r2_ingest.py
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
import duckdb
import httpx
import psycopg


# ------------------------------------------------------------------ #
# Constants
# ------------------------------------------------------------------ #

SBIR_URL = (
    "https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/"
    "award_data_no_abstract.csv"
)

R2_BUCKET = "dex-raw-landing-zone"
R2_PREFIX_BASE = "sbir"

USER_AGENT = (
    "data-engine-x/sbir-awards-r2-ingest "
    "tools@substrate.build "
    "(operational research)"
)

DOWNLOAD_TIMEOUT_SECONDS = 600
CHUNK_BYTES = 8 * 1024 * 1024  # 8 MB

# Smoke-gate ranges per directive §8.
MIN_ROW_COUNT = 200_000
MAX_ROW_COUNT = 240_000
EXPECTED_PROGRAM_COUNT = 2
MIN_PI_EMAIL_FILLED = 100_000
MIN_CONTACT_EMAIL_FILLED = 20_000


# ------------------------------------------------------------------ #
# Logging
# ------------------------------------------------------------------ #

def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sbir-awards-r2-ingest")


log = _logger()


# ------------------------------------------------------------------ #
# Env helpers
# ------------------------------------------------------------------ #

def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client():
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str | None:
    return os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DEX_DB_URL_DIRECT")


def _git_sha() -> str | None:
    """Best-effort git SHA for run provenance."""
    import subprocess
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip() or None
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return None


# ------------------------------------------------------------------ #
# Audit ledger
# ------------------------------------------------------------------ #

def insert_run_row(
    conn: psycopg.Connection,
    *,
    snapshot_date: _dt.date,
    source_url: str,
    invoked_via: str,
    git_sha: str | None,
) -> str:
    sql = """
    INSERT INTO ops.sbir_r2_ingest_runs
      (snapshot_date, source_url, status, invoked_via, git_sha)
    VALUES (%s, %s, 'running', %s, %s)
    RETURNING run_id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (snapshot_date, source_url, invoked_via, git_sha))
        run_id = cur.fetchone()[0]
    conn.commit()
    return str(run_id)


def finalize_run_row(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    source_etag: str | None,
    source_last_modified: str | None,
    source_md5: str | None,
    source_byte_count: int | None,
    row_count: int | None,
    contact_email_filled: int | None,
    pi_email_filled: int | None,
    r2_key: str | None,
    r2_byte_count: int | None,
    error_message: str | None,
) -> None:
    sql = """
    UPDATE ops.sbir_r2_ingest_runs
       SET status = %s,
           finished_at = now(),
           source_etag = %s,
           source_last_modified = %s,
           source_md5 = %s,
           source_byte_count = %s,
           row_count = %s,
           contact_email_filled = %s,
           pi_email_filled = %s,
           r2_key = %s,
           r2_byte_count = %s,
           error_message = %s
     WHERE run_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            status, source_etag, source_last_modified, source_md5,
            source_byte_count, row_count, contact_email_filled, pi_email_filled,
            r2_key, r2_byte_count, error_message, run_id,
        ))
    conn.commit()


# ------------------------------------------------------------------ #
# Stages
# ------------------------------------------------------------------ #

def download_csv(local_path: Path) -> tuple[str | None, str | None, int]:
    """Stream the SBIR bulk CSV to disk. Returns (etag, last_modified, bytes)."""
    log.info("download: GET %s", SBIR_URL)
    started = time.monotonic()
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    with httpx.stream(
        "GET", SBIR_URL, headers=headers, timeout=DOWNLOAD_TIMEOUT_SECONDS,
        follow_redirects=True,
    ) as r:
        r.raise_for_status()
        etag = r.headers.get("etag") or None
        last_modified = r.headers.get("last-modified") or None
        with local_path.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=CHUNK_BYTES):
                f.write(chunk)
    elapsed = time.monotonic() - started
    size = local_path.stat().st_size
    log.info(
        "download: %d bytes in %.1fs (%.1f MB/s) etag=%s last_modified=%s",
        size, elapsed, size / (1 << 20) / max(elapsed, 0.001), etag, last_modified,
    )
    return etag, last_modified, size


def compute_md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK_BYTES)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def csv_to_parquet(
    *, csv_path: Path, parquet_path: Path, snapshot_date: _dt.date,
    source_etag: str | None, source_last_modified: str | None,
) -> None:
    """DuckDB read_csv → COPY ZSTD Parquet, with 3 provenance cols appended."""
    log.info("transform: DuckDB read_csv → ZSTD Parquet")
    started = time.monotonic()
    con = duckdb.connect()
    try:
        snapshot_iso = snapshot_date.isoformat()
        etag_literal = (source_etag or "").replace("'", "''")
        last_mod_literal = (source_last_modified or "").replace("'", "''")
        sql = f"""
        COPY (
          SELECT
            CAST("Company" AS VARCHAR)                                  AS company,
            CAST("Award Title" AS VARCHAR)                              AS award_title,
            CAST("Agency" AS VARCHAR)                                   AS agency,
            CAST("Branch" AS VARCHAR)                                   AS branch,
            CAST("Phase" AS VARCHAR)                                    AS phase,
            CAST("Program" AS VARCHAR)                                  AS program,
            CAST("Agency Tracking Number" AS VARCHAR)                   AS agency_tracking_number,
            CAST("Contract" AS VARCHAR)                                 AS contract,
            CAST("Proposal Award Date" AS VARCHAR)                      AS proposal_award_date,
            CAST("Contract End Date" AS VARCHAR)                        AS contract_end_date,
            CAST("Solicitation Number" AS VARCHAR)                      AS solicitation_number,
            CAST("Solicitation Year" AS VARCHAR)                        AS solicitation_year,
            CAST("Solicitation Close Date" AS VARCHAR)                  AS solicitation_close_date,
            CAST("Proposal Receipt Date" AS VARCHAR)                    AS proposal_receipt_date,
            CAST("Date of Notification" AS VARCHAR)                     AS date_of_notification,
            CAST("Topic Code" AS VARCHAR)                               AS topic_code,
            CAST("Award Year" AS VARCHAR)                               AS award_year,
            CAST("Award Amount" AS VARCHAR)                             AS award_amount,
            CAST("UEI" AS VARCHAR)                                      AS uei,
            CAST("Duns" AS VARCHAR)                                     AS duns,
            CAST("HUBZone Owned" AS VARCHAR)                            AS hubzone_owned,
            CAST("Socially and Economically Disadvantaged" AS VARCHAR)  AS socially_economically_disadvantaged,
            CAST("Woman Owned" AS VARCHAR)                              AS woman_owned,
            CAST("Number Employees" AS VARCHAR)                         AS number_employees,
            CAST("Company Website" AS VARCHAR)                          AS company_website,
            CAST("Address1" AS VARCHAR)                                 AS address1,
            CAST("Address2" AS VARCHAR)                                 AS address2,
            CAST("City" AS VARCHAR)                                     AS city,
            CAST("State" AS VARCHAR)                                    AS state,
            CAST("Zip" AS VARCHAR)                                      AS zip,
            CAST("Contact Name" AS VARCHAR)                             AS contact_name,
            CAST("Contact Title" AS VARCHAR)                            AS contact_title,
            CAST("Contact Phone" AS VARCHAR)                            AS contact_phone,
            CAST("Contact Email" AS VARCHAR)                            AS contact_email,
            CAST("PI Name" AS VARCHAR)                                  AS pi_name,
            CAST("PI Title" AS VARCHAR)                                 AS pi_title,
            CAST("PI Phone" AS VARCHAR)                                 AS pi_phone,
            CAST("PI Email" AS VARCHAR)                                 AS pi_email,
            CAST("RI Name" AS VARCHAR)                                  AS ri_name,
            CAST("RI POC Name" AS VARCHAR)                              AS ri_poc_name,
            CAST("RI POC Phone" AS VARCHAR)                             AS ri_poc_phone,
            CAST('{snapshot_iso}' AS VARCHAR)                           AS snapshot_date,
            CAST('{etag_literal}' AS VARCHAR)                           AS source_etag,
            CAST('{last_mod_literal}' AS VARCHAR)                       AS source_last_modified
          FROM read_csv('{csv_path}', all_varchar=TRUE, ignore_errors=TRUE, header=TRUE)
        ) TO '{parquet_path}' (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);
        """
        con.execute(sql)
    finally:
        con.close()
    elapsed = time.monotonic() - started
    size = parquet_path.stat().st_size
    log.info(
        "transform: wrote %d bytes (%.1f MB) in %.1fs",
        size, size / (1 << 20), elapsed,
    )


def smoke_query_parquet(parquet_path: Path) -> dict[str, int]:
    """Local Parquet smoke counts (run pre-upload so we can fail-fast)."""
    con = duckdb.connect()
    try:
        total = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet_path}')"
        ).fetchone()[0]
        programs = con.execute(
            f"SELECT count(DISTINCT program) FROM read_parquet('{parquet_path}')"
        ).fetchone()[0]
        pi_filled = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet_path}') "
            f"WHERE pi_email IS NOT NULL AND pi_email <> ''"
        ).fetchone()[0]
        contact_filled = con.execute(
            f"SELECT count(*) FROM read_parquet('{parquet_path}') "
            f"WHERE contact_email IS NOT NULL AND contact_email <> ''"
        ).fetchone()[0]
    finally:
        con.close()
    return {
        "row_count": int(total),
        "distinct_programs": int(programs),
        "pi_email_filled": int(pi_filled),
        "contact_email_filled": int(contact_filled),
    }


def check_smoke_gates(metrics: dict[str, int]) -> None:
    """Hard-fail if metrics are outside directive §8 ranges."""
    rc = metrics["row_count"]
    if not (MIN_ROW_COUNT <= rc <= MAX_ROW_COUNT):
        raise RuntimeError(
            f"smoke gate failed: row_count={rc} outside [{MIN_ROW_COUNT}, {MAX_ROW_COUNT}]"
        )
    if metrics["distinct_programs"] != EXPECTED_PROGRAM_COUNT:
        raise RuntimeError(
            f"smoke gate failed: distinct_programs={metrics['distinct_programs']} "
            f"!= {EXPECTED_PROGRAM_COUNT}"
        )
    if metrics["pi_email_filled"] < MIN_PI_EMAIL_FILLED:
        raise RuntimeError(
            f"smoke gate failed: pi_email_filled={metrics['pi_email_filled']} "
            f"< {MIN_PI_EMAIL_FILLED}"
        )
    if metrics["contact_email_filled"] < MIN_CONTACT_EMAIL_FILLED:
        raise RuntimeError(
            f"smoke gate failed: contact_email_filled={metrics['contact_email_filled']} "
            f"< {MIN_CONTACT_EMAIL_FILLED}"
        )


def upload_to_r2(s3, *, parquet_path: Path, r2_key: str) -> int:
    """Upload Parquet to R2. Uses put_object so we control ContentType."""
    body = parquet_path.read_bytes()
    log.info("upload: s3://%s/%s (%d bytes)", R2_BUCKET, r2_key, len(body))
    s3.put_object(
        Bucket=R2_BUCKET, Key=r2_key, Body=body,
        ContentType="application/x-parquet",
    )
    return len(body)


# ------------------------------------------------------------------ #
# Orchestration
# ------------------------------------------------------------------ #

def run(*, snapshot_date: _dt.date, invoked_via: str, db_url: str | None) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== SBIR + STTR AWARDS R2 INGEST  snapshot=%s ===", snapshot_date)
    log.info("=" * 70)

    snapshot_iso = snapshot_date.isoformat()
    r2_key = f"{R2_PREFIX_BASE}/snapshot={snapshot_iso}/awards.parquet"
    workdir = Path(f"/tmp/sbir_awards_{snapshot_iso}_{uuid.uuid4().hex[:8]}")
    workdir.mkdir(parents=True, exist_ok=True)
    csv_path = workdir / "awards.csv"
    parquet_path = workdir / "awards.parquet"

    run_id: str | None = None
    if db_url:
        with psycopg.connect(db_url) as conn:
            run_id = insert_run_row(
                conn, snapshot_date=snapshot_date, source_url=SBIR_URL,
                invoked_via=invoked_via, git_sha=_git_sha(),
            )
        log.info("audit-ledger run_id=%s (running)", run_id)

    try:
        etag, last_modified, csv_size = download_csv(csv_path)
        csv_md5 = compute_md5(csv_path)
        log.info("download: md5=%s", csv_md5)

        csv_to_parquet(
            csv_path=csv_path, parquet_path=parquet_path,
            snapshot_date=snapshot_date,
            source_etag=etag, source_last_modified=last_modified,
        )

        metrics = smoke_query_parquet(parquet_path)
        log.info(
            "smoke: row_count=%d distinct_programs=%d pi_email_filled=%d "
            "contact_email_filled=%d",
            metrics["row_count"], metrics["distinct_programs"],
            metrics["pi_email_filled"], metrics["contact_email_filled"],
        )
        check_smoke_gates(metrics)

        s3 = _r2_client()
        r2_byte_count = upload_to_r2(s3, parquet_path=parquet_path, r2_key=r2_key)

        if db_url and run_id is not None:
            with psycopg.connect(db_url) as conn:
                finalize_run_row(
                    conn, run_id, status="success",
                    source_etag=etag, source_last_modified=last_modified,
                    source_md5=csv_md5, source_byte_count=csv_size,
                    row_count=metrics["row_count"],
                    contact_email_filled=metrics["contact_email_filled"],
                    pi_email_filled=metrics["pi_email_filled"],
                    r2_key=r2_key, r2_byte_count=r2_byte_count,
                    error_message=None,
                )

        log.info(
            "DONE snapshot=%s r2_key=%s rows=%d pi_email=%d contact_email=%d "
            "parquet_bytes=%d wall=%.1fs",
            snapshot_date, r2_key, metrics["row_count"],
            metrics["pi_email_filled"], metrics["contact_email_filled"],
            r2_byte_count, time.monotonic() - started_wall,
        )
        return 0
    except Exception as exc:
        log.exception("ingest failed: %s", exc)
        if db_url and run_id is not None:
            try:
                with psycopg.connect(db_url) as conn:
                    finalize_run_row(
                        conn, run_id, status="failed",
                        source_etag=None, source_last_modified=None,
                        source_md5=None, source_byte_count=None,
                        row_count=None, contact_email_filled=None,
                        pi_email_filled=None, r2_key=None, r2_byte_count=None,
                        error_message=str(exc)[:4000],
                    )
            except Exception:
                log.exception("failed to write failure ledger row")
        return 1
    finally:
        for p in (csv_path, parquet_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            workdir.rmdir()
        except OSError:
            pass


# ------------------------------------------------------------------ #
# CLI
# ------------------------------------------------------------------ #

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--snapshot-date", default=None,
                   help="YYYY-MM-DD; defaults to today UTC.")
    p.add_argument("--invoked-via", default="manual",
                   choices=("manual", "modal_cron"),
                   help="Provenance marker for the audit-ledger row.")
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ops.sbir_r2_ingest_runs writes.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.snapshot_date:
        try:
            snapshot_date = _dt.datetime.strptime(
                args.snapshot_date, "%Y-%m-%d",
            ).date()
        except ValueError as exc:
            log.error("--snapshot-date must be YYYY-MM-DD: %s", exc)
            return 2
    else:
        snapshot_date = _dt.datetime.now(_dt.timezone.utc).date()

    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DB URL set; audit ledger writes will be skipped")

    return run(
        snapshot_date=snapshot_date,
        invoked_via=args.invoked_via,
        db_url=db_url,
    )


if __name__ == "__main__":
    sys.exit(main())
