#!/usr/bin/env python3
"""NCUA Credit Union officer / CEO registry → R2 ingest.

Companion to the existing NCUA quarterly call-report ingest
(`scripts/run_ncua_call_report_r2_ingest.py`). The quarterly bulk ZIPs ship
FOICU + FS220 series tables but contain NO officer / CEO / manager fields.
The CU Locator API at
`https://mapping.ncua.gov/api/CreditUnionDetails/GetCreditUnionDetails/{charter}`
is the ONLY public NCUA source that surfaces the current CEO of each CU.

This script:
  1. Reads the active-CU charter universe from R2 — `s3://dex-raw-landing-zone/
     ncua/year=2024/quarter=Q4/foicu.parquet` `cu_number` (~4.5K active CUs).
  2. Fetches per-CU detail (CEO + full address + assets + region) from the
     CU Locator API, with bounded concurrency.
  3. Normalizes officer name + address fields via
     `scripts._lib.ncua_officers_normalize`.
  4. Writes a single ZSTD-compressed Parquet to R2 under
     `ncua-officers/snapshot=YYYY-MM-DD/data.parquet`.
  5. Records an audit row in `ops.ncua_officers_r2_ingest_runs`.

RisingWave wiring is **deferred** to a separate directive — this is the R2
landing-zone half only.

Usage:
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ncua_officers_r2_ingest.py
  doppler run --project hq-all --config prd -- \\
    uv run python3 scripts/run_ncua_officers_r2_ingest.py --max-rows 20 --dry-run

See directive ~/Desktop/hq/directives/2026-05-08-ncua-credit-union-officers-r2-ingest.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.types.json import Jsonb

# Ensure scripts/_lib is on sys.path regardless of invocation cwd.
_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))
from scripts._lib import ncua_officers_normalize as nm  # noqa: E402

R2_BUCKET = "dex-raw-landing-zone"
SOURCE_ROOT = "https://mapping.ncua.gov/api"
SOURCE_DETAIL = SOURCE_ROOT + "/CreditUnionDetails/GetCreditUnionDetails/{charter}"
ACTIVE_CHARTER_R2_KEY = (
    "s3://dex-raw-landing-zone/ncua/year=2024/quarter=Q4/foicu.parquet"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
PER_CHARTER_TIMEOUT_S = 30.0


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("ncua-officers-r2-ingest")


log = _logger()


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> "boto3.client":
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str:
    return _required_env("DEX_DB_URL_POOLED")


# --------------------------------------------------------------------------- #
# Charter universe — read FOICU active list from R2.
# --------------------------------------------------------------------------- #


def load_active_charters() -> list[str]:
    """Return cu_number list from the most-recent NCUA FOICU snapshot in R2."""
    con = duckdb.connect(":memory:")
    endpoint = _required_env("R2_ENDPOINT").replace("https://", "")
    con.execute(f"SET s3_endpoint='{endpoint}';")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}';")
    con.execute(
        f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}';"
    )
    con.execute("SET s3_url_style='path';")
    con.execute("SET s3_use_ssl=true;")
    rows = con.execute(
        f"SELECT DISTINCT cu_number FROM read_parquet('{ACTIVE_CHARTER_R2_KEY}') "
        f"WHERE cu_number IS NOT NULL ORDER BY cu_number;"
    ).fetchall()
    con.close()
    return [r[0] for r in rows if r[0]]


# --------------------------------------------------------------------------- #
# CU Locator detail fetch.
# --------------------------------------------------------------------------- #


def fetch_detail(client: httpx.Client, charter: str) -> dict[str, Any] | None:
    """GET per-CU detail. Returns parsed JSON dict or None on terminal failure."""
    url = SOURCE_DETAIL.format(charter=charter)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = client.get(
                url,
                timeout=PER_CHARTER_TIMEOUT_S,
                headers={
                    "Accept": "application/json",
                    "Origin": "https://mapping.ncua.gov",
                    "Referer": "https://mapping.ncua.gov/",
                },
            )
            if r.status_code == 404:
                return None
            if r.status_code in RETRY_STATUSES:
                wait = min(2 ** attempt, 30)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            if data.get("isError"):
                return None
            return data
        except (httpx.RequestError, httpx.HTTPStatusError, json.JSONDecodeError) as exc:
            last_exc = exc
            wait = min(2 ** attempt, 30)
            time.sleep(wait)
    log.warning("charter=%s detail failed after %d retries: %s",
                charter, MAX_RETRIES, last_exc)
    return None


def fetch_all_details(
    charters: list[str], *, concurrency: int, log_every: int = 250,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch detail for each charter concurrently. Returns (rows, fail_count)."""
    rows: list[dict[str, Any]] = []
    failed = 0
    started = time.monotonic()

    def _one(charter: str) -> dict[str, Any] | None:
        with httpx.Client(http2=False) as client:
            return fetch_detail(client, charter)

    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        futures = {ex.submit(_one, c): c for c in charters}
        completed = 0
        for fut in as_completed(futures):
            charter = futures[fut]
            try:
                detail = fut.result()
            except Exception as exc:
                log.warning("charter=%s worker error: %s", charter, exc)
                detail = None
            if detail is None:
                failed += 1
            else:
                detail["__charter_input__"] = charter
                rows.append(detail)
            completed += 1
            if completed % log_every == 0:
                rate = completed / max(0.001, time.monotonic() - started)
                log.info(
                    "  detail progress: %d/%d (%.0f/s, %d failed)",
                    completed, len(charters), rate, failed,
                )
    return rows, failed


# --------------------------------------------------------------------------- #
# Row projection — raw + normalized columns.
# --------------------------------------------------------------------------- #


def project_row(detail: dict[str, Any], snapshot: date) -> dict[str, Any]:
    """Map one Locator-API detail dict → flat row (raw + normalized columns)."""
    cu_charter = (detail.get("creditUnionCharter") or "").strip() or None
    cu_name_raw = detail.get("creditUnionName")
    ceo_raw = detail.get("creditUnionCeo")
    ceo_first, ceo_last = nm.normalize_officer_name(ceo_raw)

    return {
        # Raw fields preserved verbatim (VARCHAR-friendly).
        "cu_charter_number": cu_charter,
        "cu_name": cu_name_raw,
        "cu_type": detail.get("creditUnionType"),
        "cu_status": detail.get("creditUnionStatus"),
        "cu_corp": detail.get("creditUnionCorp"),
        "cu_year_chartered": detail.get("creditUnionYear"),
        "cu_issued_date": detail.get("creditUnionIssuedDate"),
        "cu_insured_date": detail.get("creditUnionInsuredDate"),
        "cu_charter_state": detail.get("creditUnionCharterState"),
        "cu_region": detail.get("creditUnionRegion"),
        "cu_fom": detail.get("creditUnionFom"),
        "cu_lowincome_designation": detail.get("creditUnionIli"),
        "cu_fhlb_member": detail.get("creditUnionFhlb"),
        "cu_assets": detail.get("creditUnionAssets"),
        "cu_assets_formatted": detail.get("assetsFormatted"),
        "cu_members": detail.get("membersFormatted"),
        "cu_peer_group": detail.get("creditUnionPeerGroup"),
        "cu_nom": detail.get("creditUnionNom"),
        "cu_office_address": detail.get("creditUnionAddress"),
        "cu_office_address_2": detail.get("creditUnionAddress2"),
        "cu_office_city": detail.get("creditUnionCity"),
        "cu_office_state": detail.get("creditUnionState"),
        "cu_office_zip": detail.get("creditUnionZip"),
        "cu_office_country": detail.get("creditUnionCountry"),
        "cu_office_county": detail.get("creditUnionCounty"),
        "cu_office_phone": detail.get("creditUnionPhone"),
        "cu_office_phone_formatted": detail.get("phoneFormatted"),
        "cu_website": detail.get("creditUnionWebsite"),
        "cu_ceo_raw": ceo_raw,
        # Normalized identity-spine columns.
        "cu_charter_number_normalized": cu_charter,
        "cu_name_normalized": nm.normalize_cu_name(cu_name_raw),
        "ceo_first_normalized": ceo_first,
        "ceo_last_normalized": ceo_last,
        # Manager / chair placeholders — Locator API does not surface these;
        # populated as NULL until a separate per-CU board feed lands.
        "manager_first_normalized": None,
        "manager_last_normalized": None,
        "board_chair_first_normalized": None,
        "board_chair_last_normalized": None,
        "cu_office_zip5": nm.zip5(detail.get("creditUnionZip")),
        "cu_office_state_normalized": nm.normalize_state(detail.get("creditUnionState")),
        "cu_size_class_normalized": nm.classify_cu_size_class(
            detail.get("creditUnionAssets")
        ),
        # Snapshot stamp.
        "ncua_officers_snapshot_date": snapshot,
        "ncua_officers_source_url": SOURCE_DETAIL.format(charter=cu_charter or ""),
    }


# --------------------------------------------------------------------------- #
# Parquet write + R2 upload.
# --------------------------------------------------------------------------- #


_DATE_COLS = {"ncua_officers_snapshot_date"}


def write_parquet(rows: list[dict[str, Any]], out_path: Path) -> tuple[int, int]:
    """Write rows to ZSTD Parquet via DuckDB. Returns (row_count, bytes).

    Columns whose values are entirely None still need to land as VARCHAR
    (or DATE for snapshot) — pyarrow's auto-inferred null-only column type
    is `null`, which downstream RW source DDL would have to special-case.
    Force string typing for everything except the snapshot DATE column.
    """
    if not rows:
        raise RuntimeError("no rows to write")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    columns = list(rows[0].keys())

    con = duckdb.connect(":memory:")
    con.execute("PRAGMA threads=2;")

    import pyarrow as pa
    fields: list[pa.Field] = []
    arrays: dict[str, list[Any]] = {}
    for col in columns:
        values = [r.get(col) for r in rows]
        if col in _DATE_COLS:
            fields.append(pa.field(col, pa.date32()))
        else:
            fields.append(pa.field(col, pa.string()))
            values = [None if v is None else str(v) for v in values]
        arrays[col] = values
    schema = pa.schema(fields)
    table = pa.Table.from_pydict(arrays, schema=schema)
    con.register("rows_arrow", table)

    con.execute(
        f"COPY (SELECT * FROM rows_arrow) TO '{out_path}' "
        f"(FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000);"
    )
    con.unregister("rows_arrow")

    confirmed = con.execute(
        f"SELECT count(*) FROM read_parquet('{out_path}');"
    ).fetchone()[0]
    con.close()
    return int(confirmed), out_path.stat().st_size


def upload_to_r2(parquet_path: Path, *, key: str) -> int:
    s3 = _r2_client()
    s3.upload_file(
        str(parquet_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )
    return parquet_path.stat().st_size


# --------------------------------------------------------------------------- #
# Audit ledger.
# --------------------------------------------------------------------------- #


def get_prior_source_last_modified(
    conn: psycopg.Connection, snapshot: date,
) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT source_last_modified
              FROM ops.ncua_officers_r2_ingest_runs
             WHERE snapshot_date = %s AND status = 'completed'
             ORDER BY started_at DESC LIMIT 1;
            """,
            (snapshot,),
        )
        row = cur.fetchone()
    return row[0] if row else None


def insert_run_row(
    conn: psycopg.Connection, snapshot: date, *,
    source_url: str, source_last_modified: datetime | None,
    prior_source_last_modified: datetime | None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ops.ncua_officers_r2_ingest_runs (
                snapshot_date, status, source_url, source_path_taken,
                source_last_modified, prior_source_last_modified
            ) VALUES (%s, 'running', %s, 'cu_locator_api', %s, %s)
            RETURNING id;
            """,
            (snapshot, source_url, source_last_modified, prior_source_last_modified),
        )
        row_id = cur.fetchone()[0]
    conn.commit()
    return str(row_id)


def finalize_run_row(
    conn: psycopg.Connection, run_id: str, *,
    status: str,
    charter_universe_size: int,
    charters_with_detail: int,
    charters_failed: int,
    parquet_row_count: int,
    parquet_bytes_written: int,
    r2_bucket: str | None, r2_key: str | None,
    r2_total_bytes: int,
    started_wall: float,
    error_message: str | None,
    notes: dict[str, Any] | None,
) -> None:
    duration = round(time.monotonic() - started_wall, 3)
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE ops.ncua_officers_r2_ingest_runs
               SET status = %s,
                   charter_universe_size = %s,
                   charters_with_detail = %s,
                   charters_failed = %s,
                   parquet_row_count = %s,
                   parquet_bytes_written = %s,
                   r2_bucket = %s, r2_key = %s, r2_total_bytes = %s,
                   finished_at = now(), duration_seconds = %s,
                   error_message = %s, notes = %s
             WHERE id = %s;
            """,
            (
                status,
                charter_universe_size, charters_with_detail, charters_failed,
                parquet_row_count, parquet_bytes_written,
                r2_bucket, r2_key, r2_total_bytes,
                duration, error_message,
                Jsonb(notes) if notes else None, run_id,
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------- #
# Main flow.
# --------------------------------------------------------------------------- #


def run_ingest(
    *, snapshot: date, dry_run: bool, max_rows: int | None,
    workdir: Path, concurrency: int,
) -> int:
    log_prefix = f"[snapshot={snapshot.isoformat()}]"
    started_wall = time.monotonic()
    source_now = datetime.now(timezone.utc)

    log.info("%s loading active-charter universe from R2 FOICU…", log_prefix)
    charters = load_active_charters()
    if max_rows is not None:
        charters = charters[:max_rows]
    universe = len(charters)
    log.info("%s charter universe size: %d", log_prefix, universe)

    if dry_run:
        log.info("%s DRY RUN — would fetch detail for %d charters; exiting.",
                 log_prefix, universe)
        return 0

    if universe == 0:
        log.error("%s charter universe is empty — refusing to write empty Parquet",
                  log_prefix)
        return 1

    with psycopg.connect(_database_url()) as conn:
        prior = get_prior_source_last_modified(conn, snapshot)
        run_id = insert_run_row(
            conn, snapshot,
            source_url=SOURCE_ROOT,
            source_last_modified=source_now,
            prior_source_last_modified=prior,
        )
        log.info("%s run id: %s", log_prefix, run_id)

        try:
            log.info("%s fetching CU detail (concurrency=%d)…",
                     log_prefix, concurrency)
            details, failed = fetch_all_details(charters, concurrency=concurrency)
            log.info("%s fetched detail for %d/%d charters (%d failed)",
                     log_prefix, len(details), universe, failed)

            log.info("%s projecting rows…", log_prefix)
            rows = [project_row(d, snapshot) for d in details]

            parquet_path = workdir / f"ncua_officers_{snapshot.isoformat()}.parquet"
            log.info("%s writing Parquet → %s", log_prefix, parquet_path)
            row_count, parquet_bytes = write_parquet(rows, parquet_path)
            log.info("%s parquet: %d rows, %.1f KB",
                     log_prefix, row_count, parquet_bytes / 1024)

            r2_key = f"ncua-officers/snapshot={snapshot.isoformat()}/data.parquet"
            log.info("%s uploading to s3://%s/%s …",
                     log_prefix, R2_BUCKET, r2_key)
            uploaded = upload_to_r2(parquet_path, key=r2_key)
            log.info("%s uploaded %.1f KB", log_prefix, uploaded / 1024)

            finalize_run_row(
                conn, run_id, status="completed",
                charter_universe_size=universe,
                charters_with_detail=len(details),
                charters_failed=failed,
                parquet_row_count=row_count,
                parquet_bytes_written=parquet_bytes,
                r2_bucket=R2_BUCKET, r2_key=r2_key,
                r2_total_bytes=uploaded,
                started_wall=started_wall, error_message=None,
                notes={
                    "source_path_taken": "cu_locator_api",
                    "source_root": SOURCE_ROOT,
                    "active_charter_r2_key": ACTIVE_CHARTER_R2_KEY,
                    "concurrency": concurrency,
                    "max_rows": max_rows,
                },
            )
            log.info("%s DONE rows=%d failed=%d wall=%.1fs",
                     log_prefix, row_count, failed,
                     time.monotonic() - started_wall)
            try:
                parquet_path.unlink(missing_ok=True)
            except Exception:
                pass
            return 0

        except Exception as exc:
            log.exception("%s ingest failed", log_prefix)
            finalize_run_row(
                conn, run_id, status="failed",
                charter_universe_size=universe,
                charters_with_detail=0, charters_failed=universe,
                parquet_row_count=0, parquet_bytes_written=0,
                r2_bucket=None, r2_key=None, r2_total_bytes=0,
                started_wall=started_wall,
                error_message=str(exc), notes=None,
            )
            return 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--snapshot-date", default=None,
        help="Snapshot date (YYYY-MM-DD). Defaults to today (UTC).",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-rows", type=int, default=None,
        help="Cap charter list at N (smoke testing).",
    )
    p.add_argument("--workdir", default=None)
    p.add_argument(
        "--concurrency", type=int, default=12,
        help="Parallel CU detail fetches. Default 12.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir or "/tmp/ncua_officers_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    if args.snapshot_date:
        snapshot = date.fromisoformat(args.snapshot_date)
    else:
        snapshot = datetime.now(timezone.utc).date()

    return run_ingest(
        snapshot=snapshot,
        dry_run=args.dry_run,
        max_rows=args.max_rows,
        workdir=workdir,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    sys.exit(main())
