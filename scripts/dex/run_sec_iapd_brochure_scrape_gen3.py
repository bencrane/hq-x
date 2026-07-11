#!/usr/bin/env python3
"""SEC IAPD Form ADV Part 2 brochure scrape — Phase 1 raw R2 preservation.

For each active SEC-registered, non-Part-2-exempt CRD enumerated from
``s3://dex-raw-landing-zone/sec-adv/year=2024/table=base_a/*.parquet``:

  1. Fetch the IAPD manifest:
       GET https://api.adviserinfo.sec.gov/search/firm/{CRD}?...&wt=json
       → brochures.brochuredetails[] (brochureVersionID, brochureName, dateSubmitted)
  2. For each brochure version, fetch the PDF:
       GET https://files.adviserinfo.sec.gov/IAPD/Content/Common/crd_iapd_Brochure.aspx?BRCHR_VRSN_ID={vid}
       → application/pdf binary
  3. Land original bytes verbatim at:
       s3://data-sink/active/_sec_iapd_brochure_pdfs_raw/crd={CRD}/{ISO_filing_date}/{vid}.pdf

NO parsing. NO Parquet output. NO ZSTD compression at the R2 layer (PDFs are
already compressed; transport-layer wrapping breaks Phase 2 pypdf parsing).

Idempotency: per (CRD, brochureVersionID) audit ledger row in
ops.sec_iapd_form_adv_part_2_brochure_scrape_runs. Re-runs check existing
'completed' rows in the ledger and skip — no R2 list-prefix probe per item.

Resumability: per-CRD checkpoint state file at --state-file. Re-runs with the
same state file pick up where the prior run left off.

Usage:
  doppler run --project hq-all --config prd -- \\
    /tmp/iapd-recon-venv/bin/python apps/data-engine-x/scripts/run_sec_iapd_form_adv_part_2_brochure_scrape.py \\
        --max-crds 100 --state-file /tmp/iapd-smoke.json
  doppler run --project hq-all --config prd -- \\
    /tmp/iapd-recon-venv/bin/python apps/data-engine-x/scripts/run_sec_iapd_form_adv_part_2_brochure_scrape.py \\
        --state-file /tmp/iapd-full.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import boto3
import duckdb
import httpx
import psycopg
from psycopg.rows import tuple_row

sys.path.insert(0, str(Path(__file__).parent))
from _lib.sec_iapd_brochure_client import (  # noqa: E402
    BrochureManifestEntry,
    FirmManifest,
    IapdBrochureClient,
    RpsLimiter,
)


R2_BUCKET = "data-sink"
R2_PREFIX = "active/_sec_iapd_brochure_pdfs_raw"
BASE_A_PARQUET = None  # Gen-3: enumeration removed; CRDs fed via --crds from
# s3://data-sink/active/_crd_worklist_private_credit/ (see run_sec_iapd_brochure_scrape_gen3 driver).

# Per Stage 0: api.adviserinfo.sec.gov + files.adviserinfo.sec.gov are
# CDN-fronted (CloudFlare in front, AWS API Gateway behind). 50-CRD smoke at
# 5 RPS produced zero 429s; 4 RPS keeps comfortable headroom.
TARGET_RPS = 4.0
HTTP_CONCURRENCY = 8
PROGRESS_LOG_EVERY = 100


def _logger() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    return logging.getLogger("sec-iapd-form-adv-part-2-scrape")


log = _logger()


def _required_env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"{name} is not set in the environment.")
    return v


def _r2_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=_required_env("R2_ENDPOINT"),
        aws_access_key_id=_required_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_required_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def _database_url() -> str | None:
    return os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DEX_DB_URL_POOLED")


def enumerate_crds_from_r2(limit: int | None = None) -> list[int]:
    """Pull distinct active CRDs from sec-adv/year=2024/table=base_a parquet."""
    endpoint = _required_env("R2_ENDPOINT").replace("https://", "").replace("http://", "").rstrip("/")
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute(f"SET s3_endpoint='{endpoint}'")
    con.execute(f"SET s3_access_key_id='{_required_env('R2_ACCESS_KEY_ID')}'")
    con.execute(f"SET s3_secret_access_key='{_required_env('R2_SECRET_ACCESS_KEY')}'")
    con.execute("SET s3_url_style='path'")
    con.execute("SET s3_use_ssl=true")

    sql = f"""
      SELECT DISTINCT crd_number
        FROM read_parquet('{BASE_A_PARQUET}')
       WHERE crd_number IS NOT NULL
       ORDER BY crd_number
    """
    if limit is not None:
        sql += f" LIMIT {int(limit)}"

    rows = con.execute(sql).fetchall()
    return [int(r[0]) for r in rows]


def load_existing_ledger_versions(db_url: str | None) -> set[tuple[int, int]]:
    """Pull (crd, version_id) pairs already at status='completed' in the ledger."""
    if db_url is None:
        return set()
    out: set[tuple[int, int]] = set()
    with psycopg.connect(db_url, row_factory=tuple_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT crd_number, version_id
                  FROM ops.sec_iapd_form_adv_part_2_brochure_scrape_runs
                 WHERE status = 'completed'
            """)
            for crd, vid in cur.fetchall():
                try:
                    out.add((int(crd), int(vid)))
                except (TypeError, ValueError):
                    continue
    return out


def _compute_r2_key(entry: BrochureManifestEntry) -> str:
    iso = entry.date_submitted.isoformat()
    return (
        f"{R2_PREFIX}/crd={entry.crd_number}/{iso}/"
        f"{entry.brochure_version_id}.pdf"
    )


@dataclass
class ScrapeState:
    """Per-run live counters."""
    manifest_attempted: int = 0
    manifest_success: int = 0
    manifest_failed: int = 0
    manifest_skipped: int = 0
    download_attempted: int = 0
    download_success: int = 0
    download_failed: int = 0
    download_skipped: int = 0
    bytes_landed: int = 0
    completed_crds: set[int] = field(default_factory=set)


def insert_pending_row(
    conn: psycopg.Connection,
    *,
    entry: BrochureManifestEntry,
) -> int:
    """Insert a 'running' row for one (CRD, version_id). Returns row id."""
    sql = """
    INSERT INTO ops.sec_iapd_form_adv_part_2_brochure_scrape_runs
      (crd_number, version_id, brochure_type, filing_date, original_filename,
       source_url, r2_key, status)
    VALUES (%s, %s, %s, %s, %s, %s, %s, 'running')
    ON CONFLICT (crd_number, version_id) DO UPDATE
      SET status = 'running',
          started_at = now(),
          error_message = NULL
    RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            entry.crd_number,
            str(entry.brochure_version_id),
            entry.brochure_type,
            entry.date_submitted,
            entry.original_filename,
            entry.source_url,
            _compute_r2_key(entry),
        ))
        row = cur.fetchone()
    conn.commit()
    return int(row[0])


def finalize_row(
    conn: psycopg.Connection,
    *,
    crd: int,
    version_id: int,
    status: str,
    content_type: str | None = None,
    content_bytes: int | None = None,
    content_sha256: str | None = None,
    error_message: str | None = None,
) -> None:
    sql = """
    UPDATE ops.sec_iapd_form_adv_part_2_brochure_scrape_runs
       SET status = %s,
           content_type = %s,
           content_bytes = %s,
           content_sha256 = %s,
           captured_at = CASE WHEN %s = 'completed' THEN now() ELSE captured_at END,
           error_message = %s
     WHERE crd_number = %s AND version_id = %s;
    """
    with conn.cursor() as cur:
        cur.execute(sql, (
            status,
            content_type,
            content_bytes,
            content_sha256,
            status,
            error_message,
            crd,
            str(version_id),
        ))
    conn.commit()


async def process_one_crd(
    crd: int,
    *,
    client_iapd: IapdBrochureClient,
    s3,
    db_url: str | None,
    completed_pairs: set[tuple[int, int]],
    state: ScrapeState,
    state_lock: asyncio.Lock,
) -> None:
    """Discover + download all brochures for one CRD. Updates state in place."""
    async with state_lock:
        state.manifest_attempted += 1

    try:
        manifest = await client_iapd.fetch_manifest(crd)
    except Exception as exc:
        log.warning("CRD %s manifest threw %s", crd, exc)
        async with state_lock:
            state.manifest_failed += 1
        return

    if manifest is None:
        async with state_lock:
            state.manifest_failed += 1
        return

    if not manifest.is_scrapeable:
        async with state_lock:
            state.manifest_skipped += 1
            state.completed_crds.add(crd)
        return

    async with state_lock:
        state.manifest_success += 1

    # Per-version download loop
    for entry in manifest.brochures:
        pair = (entry.crd_number, entry.brochure_version_id)
        if pair in completed_pairs:
            async with state_lock:
                state.download_skipped += 1
            continue

        async with state_lock:
            state.download_attempted += 1

        # Open per-call DB conn (psycopg is sync; keep conns short-lived)
        conn: psycopg.Connection | None = None
        if db_url is not None:
            try:
                conn = psycopg.connect(db_url)
                insert_pending_row(conn, entry=entry)
            except Exception as exc:
                log.warning("ledger pre-insert (%s/%s) failed: %s",
                            entry.crd_number, entry.brochure_version_id, exc)
                if conn is not None:
                    conn.close()
                    conn = None

        try:
            body, ctype = await client_iapd.download_brochure(entry)
            if not body or not body.startswith(b"%PDF"):
                # IAPD sometimes returns a tiny stub or wrong content; treat as failure
                raise RuntimeError(
                    f"non-PDF body (len={len(body)}, head={body[:8]!r})"
                )
            sha = hashlib.sha256(body).hexdigest()
            r2_key = _compute_r2_key(entry)
            s3.put_object(
                Bucket=R2_BUCKET,
                Key=r2_key,
                Body=body,
                ContentType=ctype,
            )
            async with state_lock:
                state.download_success += 1
                state.bytes_landed += len(body)
                completed_pairs.add(pair)

            if conn is not None:
                try:
                    finalize_row(
                        conn,
                        crd=entry.crd_number,
                        version_id=entry.brochure_version_id,
                        status="completed",
                        content_type=ctype,
                        content_bytes=len(body),
                        content_sha256=sha,
                    )
                except Exception as exc:
                    log.warning("ledger finalize-completed failed: %s", exc)
        except Exception as exc:
            async with state_lock:
                state.download_failed += 1
            log.warning("download CRD=%s vid=%s failed: %s",
                        entry.crd_number, entry.brochure_version_id, exc)
            if conn is not None:
                try:
                    finalize_row(
                        conn,
                        crd=entry.crd_number,
                        version_id=entry.brochure_version_id,
                        status="failed",
                        error_message=str(exc)[:1000],
                    )
                except Exception as inner:
                    log.warning("ledger finalize-failed failed: %s", inner)
        finally:
            if conn is not None:
                conn.close()

    async with state_lock:
        state.completed_crds.add(crd)


async def run(
    *,
    state_path: Path,
    db_url: str | None,
    max_crds: int | None,
    crds_override: list[int] | None,
) -> int:
    started_wall = time.monotonic()
    log.info("=" * 70)
    log.info("=== SEC IAPD Form ADV Part 2 brochure scrape — Phase 1 ===")
    log.info("=" * 70)

    if crds_override:
        crds = sorted(set(int(c) for c in crds_override))
        log.info("using %d CRDs from --crds override", len(crds))
    else:
        crds = enumerate_crds_from_r2(limit=max_crds)
        log.info("enumerated %d distinct CRDs from R2 base_a", len(crds))

    # Per-CRD checkpoint
    state_data: dict[str, Any] = {}
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text())
        except (json.JSONDecodeError, OSError):
            state_data = {}
    completed_crds_chk: set[int] = set(int(c) for c in state_data.get("completed_crds", []))
    if completed_crds_chk:
        log.info("resuming: %d CRDs in checkpoint", len(completed_crds_chk))
    crds = [c for c in crds if c not in completed_crds_chk]
    log.info("remaining CRDs to process: %d", len(crds))

    # Per-(CRD, version) idempotency from the ledger
    completed_pairs = load_existing_ledger_versions(db_url)
    log.info("ledger has %d completed (CRD, version_id) pairs already", len(completed_pairs))

    state = ScrapeState(completed_crds=completed_crds_chk)
    state_lock = asyncio.Lock()
    limiter = RpsLimiter(TARGET_RPS)
    s3 = _r2_client()

    # asyncio + httpx — single pooled client across all workers
    async with httpx.AsyncClient(http2=False) as http_client:
        client_iapd = IapdBrochureClient(http_client, limiter)
        sem = asyncio.Semaphore(HTTP_CONCURRENCY)

        async def _worker(crd: int) -> None:
            async with sem:
                await process_one_crd(
                    crd,
                    client_iapd=client_iapd,
                    s3=s3,
                    db_url=db_url,
                    completed_pairs=completed_pairs,
                    state=state,
                    state_lock=state_lock,
                )
                if state.manifest_attempted % PROGRESS_LOG_EVERY == 0:
                    log.info(
                        "progress: man=%d (ok=%d fail=%d skip=%d) "
                        "dl=%d (ok=%d fail=%d skip=%d) bytes=%.2fMB CRDs_done=%d",
                        state.manifest_attempted, state.manifest_success,
                        state.manifest_failed, state.manifest_skipped,
                        state.download_attempted, state.download_success,
                        state.download_failed, state.download_skipped,
                        state.bytes_landed / (1 << 20),
                        len(state.completed_crds),
                    )

        stop_event = asyncio.Event()

        async def _checkpointer() -> None:
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    pass
                async with state_lock:
                    state_data["completed_crds"] = sorted(state.completed_crds)
                    state_data["ts"] = datetime.now(timezone.utc).isoformat()
                    try:
                        state_path.write_text(json.dumps(state_data))
                    except OSError as exc:
                        log.warning("state write failed: %s", exc)

        cp_task = asyncio.create_task(_checkpointer())
        try:
            await asyncio.gather(*[_worker(c) for c in crds])
        finally:
            stop_event.set()
            await cp_task

        async with state_lock:
            state_data["completed_crds"] = sorted(state.completed_crds)
            state_data["ts"] = datetime.now(timezone.utc).isoformat()
            try:
                state_path.write_text(json.dumps(state_data))
            except OSError as exc:
                log.warning("final state write failed: %s", exc)

    wall = time.monotonic() - started_wall
    log.info("=" * 70)
    log.info(
        "DONE manifest_attempted=%d success=%d failed=%d skipped=%d | "
        "download_attempted=%d success=%d failed=%d skipped=%d | "
        "bytes_landed=%.2fMB | wall=%.1fs",
        state.manifest_attempted, state.manifest_success,
        state.manifest_failed, state.manifest_skipped,
        state.download_attempted, state.download_success,
        state.download_failed, state.download_skipped,
        state.bytes_landed / (1 << 20), wall,
    )

    if state.manifest_attempted == 0:
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--max-crds", type=int, default=None,
                   help="Cap on CRDs to enumerate (smoke).")
    p.add_argument("--crds", default=None,
                   help="Comma-separated CRD list override (smoke).")
    p.add_argument(
        "--state-file",
        default="/tmp/sec_iapd_form_adv_part_2_state.json",
        help="Resume checkpoint file.",
    )
    p.add_argument("--no-audit", action="store_true",
                   help="Skip ledger writes.")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    state_path = Path(args.state_file)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = None if args.no_audit else _database_url()
    if not args.no_audit and not db_url:
        log.warning("no DEX_DB_URL_* set; ledger writes will be skipped")

    crds_override: list[int] | None = None
    if args.crds:
        crds_override = [int(c.strip()) for c in args.crds.split(",") if c.strip()]

    try:
        return asyncio.run(run(
            state_path=state_path,
            db_url=db_url,
            max_crds=args.max_crds,
            crds_override=crds_override,
        ))
    except KeyboardInterrupt:
        log.warning("interrupted; checkpoint preserved")
        return 130


if __name__ == "__main__":
    sys.exit(main())
