"""SAM.gov API UEI enrichment for Award Notices.

The bulk Contract Opportunities CSV does NOT carry UEI on Award Notices.
The SAM.gov Opportunities API v2 does (`award.awardee.ueiSAM`). This
script fetches Award Notice records via the API and writes a narrow
enrichment Parquet to R2 keyed by notice_id → uei.

Pattern C trivial bridge MV (mv_sam_opps_archived_with_uei) joins this
enrichment to source_sam_opps_archived on notice_id and to
source_sam_entities on uei.

API endpoint: https://api.sam.gov/opportunities/v2/search
  Required: postedFrom + postedTo (MM/dd/yyyy), max 1-year window.
  Filter: ptype=a (Award Notice).
  Returns latest active version of each opportunity (so this only
  enriches Award Notices that haven't yet archived; capture them in the
  ACTIVE window before archive_date passes).

Rate limit: 1,000 req/day for federal-tier API key (the operator's
SAM_API_KEY). Each page = 1,000 records. At ~200 Award Notices/day
nationally, daily cron uses 1 call. Backfill of currently-active corpus
(~13k Award Notices) takes ~13-15 calls = trivial.

Modes:
  --mode daily         postedFrom = yesterday, postedTo = today
                       (called by Modal cron at 13:00 UTC daily; uses
                       1-2 paginated calls)

  --mode backfill      postedFrom/postedTo as a 1-year window via
                       --window-from / --window-to flags. For currently-
                       active corpus, run with --window-from=2025-05-09
                       --window-to=2026-05-09 (last 12 months).

Usage:
  cd ~/hq-all && doppler run --project hq-all --config prd --command \\
    'uv run --with "psycopg[binary]" --with httpx --with boto3 --with pyarrow \\
     python apps/data-engine-x/scripts/run_sam_opps_api_uei_enrichment.py \\
     --mode daily'

  cd ~/hq-all && doppler run --project hq-all --config prd --command \\
    'uv run --with "psycopg[binary]" --with httpx --with boto3 --with pyarrow \\
     python apps/data-engine-x/scripts/run_sam_opps_api_uei_enrichment.py \\
     --mode backfill --window-from=2025-05-09 --window-to=2026-05-09'
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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_sam_opps_api_uei_enrichment")

R2_BUCKET = "dex-raw-landing-zone"
SOURCE_ID = "sam_opps_api_uei"
FEED_NAME = "enrichment"

API_URL = "https://api.sam.gov/opportunities/v2/search"
PAGE_SIZE = 1000
PTYPE_AWARD_NOTICE = "a"  # SAM API procurement-type filter for Award Notices

# Throttle for backfill — SAM API's daily quota is generous for federal
# tier (1000/day) but it ALSO enforces a per-minute burst limit that's
# stricter than documented (observed 2026-05-09: 429 after ~5 quick
# requests). Sleep 3s between pages to stay below the burst limit.
INTER_PAGE_SLEEP_S = 3.0

# 429 retry policy: exponential backoff respecting Retry-After header.
MAX_429_RETRIES = 6
RETRY_BACKOFF_BASE_S = 30.0  # 30s, 60s, 120s, 240s, 480s, 960s


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
    *, run_id: uuid.UUID, feed_date: date, r2_object_key: str,
    started_at: datetime, evidence: dict[str, Any],
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
                    evidence = EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    str(run_id), SOURCE_ID, FEED_NAME, feed_date.isoformat(),
                    started_at, R2_BUCKET, r2_object_key,
                    json.dumps(evidence, default=str),
                ),
            )
        conn.commit()


def _record_run_terminal(
    *, run_id: uuid.UUID, feed_date: date, status: str, outcome: str,
    started_at: datetime, rows_loaded: int | None,
    payload_bytes: int | None, r2_object_key: str | None,
    error_class: str | None, error_message: str | None,
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
                    status = %s, outcome = %s,
                    completed_at = %s, duration_seconds = %s,
                    rows_loaded = %s, payload_bytes = %s,
                    r2_object_key = COALESCE(%s, r2_object_key),
                    error_class = %s, error_message = %s,
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
                    str(run_id), SOURCE_ID, FEED_NAME,
                ),
            )
        conn.commit()


def _fetch_page(
    *, api_key: str, posted_from: date, posted_to: date, offset: int,
) -> dict[str, Any]:
    """Fetch one page with 429 retry-with-backoff. Honors Retry-After
    header when present; otherwise exponential backoff from
    RETRY_BACKOFF_BASE_S. Other 4xx/5xx errors raise immediately."""
    import httpx
    params = {
        "api_key": api_key,
        "postedFrom": posted_from.strftime("%m/%d/%Y"),
        "postedTo": posted_to.strftime("%m/%d/%Y"),
        "limit": PAGE_SIZE,
        "offset": offset,
        "ptype": PTYPE_AWARD_NOTICE,
    }
    with httpx.Client(timeout=60.0) as client:
        for attempt in range(MAX_429_RETRIES + 1):
            r = client.get(API_URL, params=params)
            if r.status_code != 429:
                r.raise_for_status()
                return r.json()
            # Distinguish quota-exhausted 429 (code 900804, daily quota
            # gone, recovers at midnight UTC) from burst-limit 429 (per-
            # minute throttle, recovers in seconds). Quota 429: bail
            # immediately — retrying for hours is pointless and burns
            # subsequent quota when reset hits.
            try:
                body = r.json()
            except Exception:
                body = {}
            if body.get("code") == "900804":
                next_access = body.get("nextAccessTime", "next UTC midnight")
                logger.error(
                    "DAILY QUOTA EXHAUSTED (code 900804). Quota resets at %s. "
                    "If this is your federal-tier key, the daily cap is 1000/day; "
                    "if x-api-roles: SI-NONFED in response headers, the cap is "
                    "10/day and the key needs to be upgraded via SAM.gov account.",
                    next_access,
                )
                r.raise_for_status()
            if attempt >= MAX_429_RETRIES:
                logger.error("429 backoff exhausted after %d attempts", attempt)
                r.raise_for_status()
            retry_after_hdr = r.headers.get("retry-after") or r.headers.get("Retry-After")
            try:
                wait_s = float(retry_after_hdr) if retry_after_hdr else 0.0
            except ValueError:
                wait_s = 0.0
            if wait_s <= 0:
                wait_s = RETRY_BACKOFF_BASE_S * (2 ** attempt)
            logger.warning(
                "429 received (attempt %d, burst-limit) — sleeping %.0fs before retry",
                attempt + 1, wait_s,
            )
            time.sleep(wait_s)
        # Unreachable
        raise RuntimeError("429 retry loop exited without return")


def _extract_enrichment_rows(
    page: dict[str, Any], snapshot_date: date, run_id: uuid.UUID,
    source_url: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()
    snap_iso = snapshot_date.isoformat()
    run_id_str = str(run_id)
    for r in page.get("opportunitiesData") or []:
        notice_id = r.get("noticeId")
        if not notice_id:
            continue
        award = r.get("award") or {}
        awardee = award.get("awardee") or {}
        rows.append({
            "notice_id": str(notice_id),
            "uei": str(awardee.get("ueiSAM") or "") or None,
            "awardee_legal_name_from_api": str(awardee.get("name") or "") or None,
            "awardee_cage_code_from_api": str(awardee.get("cageCode") or "") or None,
            "award_amount_from_api": str(award.get("amount") or "") or None,
            "award_date_from_api": str(award.get("date") or "") or None,
            "api_response_status": "ok",
            "_ingest_run_id": run_id_str,
            "_snapshot_date": snap_iso,
            "_ingested_at": now_iso,
            "_source_url": source_url,
        })
    return rows


def _write_parquet(rows: list[dict[str, Any]], out_path: Path) -> int:
    import pyarrow as pa, pyarrow.parquet as pq
    if not rows:
        return 0
    # Force every column to string at write time per L2 path #1
    schema = pa.schema([(k, pa.string()) for k in rows[0].keys()])
    cols: dict[str, list[str | None]] = {k: [] for k in rows[0].keys()}
    for r in rows:
        for k in cols:
            v = r.get(k)
            cols[k].append(None if v is None else str(v))
    table = pa.Table.from_pydict(cols, schema=schema)
    pq.write_table(table, str(out_path), compression="zstd", compression_level=3)
    return out_path.stat().st_size


def _upload_to_r2(local_path: Path, key: str) -> None:
    import boto3
    c = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    # ContentType only — NO ContentEncoding=zstd per L42.
    c.upload_file(
        str(local_path), R2_BUCKET, key,
        ExtraArgs={"ContentType": "application/x-parquet"},
    )


def _r2_object_key(*, snapshot_date: date, run_id: uuid.UUID) -> str:
    # Canonical Hive-style snapshot={date}/data.parquet filename. run_id retained
    # in LandingResult metadata + ledger only, not in path. Note: each invocation
    # of run_window() is parameterized by a (posted_from, posted_to) window, so
    # multiple intra-day invocations covering different windows will atomic-replace
    # last-write-wins. If multi-window-per-snapshot semantics are required in
    # production, refactor the path to include `window=...` as an explicit
    # partition; today's daily-cron pattern is a single sweep per snapshot.
    del run_id  # silence "unused" lint
    return f"sam-gov-opps/uei-enrichment/snapshot={snapshot_date.isoformat()}/data.parquet"


def run_window(
    *, posted_from: date, posted_to: date,
    snapshot_date: date | None = None,
) -> dict[str, Any]:
    """Fetch all Award Notices in [posted_from, posted_to] window via
    paginated API calls; write enrichment Parquet to R2; record ledger row."""
    snapshot_date = snapshot_date or datetime.now(timezone.utc).date()
    api_key = os.environ.get("SAM_API_KEY")
    if not api_key:
        raise RuntimeError("SAM_API_KEY not in env (Doppler hq-all/prd)")

    run_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    r2_key = _r2_object_key(snapshot_date=snapshot_date, run_id=run_id)
    source_url = (
        f"{API_URL}?postedFrom={posted_from.strftime('%m/%d/%Y')}"
        f"&postedTo={posted_to.strftime('%m/%d/%Y')}&ptype={PTYPE_AWARD_NOTICE}"
    )
    evidence = {
        "posted_from": posted_from.isoformat(),
        "posted_to": posted_to.isoformat(),
        "snapshot_date": snapshot_date.isoformat(),
        "feed_slice": f"window={posted_from.isoformat()}_{posted_to.isoformat()}",
    }

    _record_run_pending(
        run_id=run_id, feed_date=snapshot_date, r2_object_key=r2_key,
        started_at=started_at, evidence=evidence,
    )

    out_path: Path | None = None
    api_calls = 0
    rows: list[dict[str, Any]] = []
    try:
        offset = 0
        total = None
        while True:
            page = _fetch_page(
                api_key=api_key, posted_from=posted_from,
                posted_to=posted_to, offset=offset,
            )
            api_calls += 1
            if total is None:
                total = int(page.get("totalRecords") or 0)
                logger.info(
                    "[%s..%s] totalRecords=%s, paginating at limit=%d",
                    posted_from.isoformat(), posted_to.isoformat(),
                    f"{total:,}", PAGE_SIZE,
                )
            page_rows = _extract_enrichment_rows(
                page, snapshot_date=snapshot_date, run_id=run_id,
                source_url=source_url,
            )
            rows.extend(page_rows)
            n_in_page = len(page.get("opportunitiesData") or [])
            logger.info(
                "  offset=%d -> page_rows=%d (after extract: %d, cumulative: %d)",
                offset, n_in_page, len(page_rows), len(rows),
            )
            if n_in_page < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            if offset >= (total or 0):
                break
            time.sleep(INTER_PAGE_SLEEP_S)

        # Write
        fd, out_path_str = tempfile.mkstemp(suffix=".parquet", prefix="sam_uei_")
        os.close(fd)
        out_path = Path(out_path_str)
        bytes_out = _write_parquet(rows, out_path)
        if bytes_out == 0:
            logger.warning("zero rows extracted; skipping R2 upload")
            _record_run_terminal(
                run_id=run_id, feed_date=snapshot_date, status="completed",
                outcome="succeeded_with_zero_new_rows",
                started_at=started_at, rows_loaded=0, payload_bytes=0,
                r2_object_key=None, error_class=None, error_message=None,
                evidence={**evidence, "api_calls": api_calls},
            )
            return {
                "run_id": str(run_id), "outcome": "succeeded_with_zero_new_rows",
                "rows_loaded": 0, "api_calls": api_calls,
            }
        _upload_to_r2(out_path, r2_key)
        outcome = "succeeded_with_changes"
        _record_run_terminal(
            run_id=run_id, feed_date=snapshot_date, status="completed",
            outcome=outcome, started_at=started_at,
            rows_loaded=len(rows), payload_bytes=bytes_out,
            r2_object_key=r2_key, error_class=None, error_message=None,
            evidence={**evidence, "api_calls": api_calls},
        )
        logger.info(
            "completed: %s rows / %s bytes / %d api calls -> %s",
            f"{len(rows):,}", f"{bytes_out:,}", api_calls, r2_key,
        )
        return {
            "run_id": str(run_id), "outcome": outcome,
            "rows_loaded": len(rows), "payload_bytes": bytes_out,
            "r2_object_key": r2_key, "api_calls": api_calls,
        }
    except Exception as exc:
        _record_run_terminal(
            run_id=run_id, feed_date=snapshot_date, status="failed",
            outcome="failed", started_at=started_at,
            rows_loaded=None, payload_bytes=None, r2_object_key=None,
            error_class=_classify_exception(exc),
            error_message=str(exc)[:4000],
            evidence={**evidence, "api_calls": api_calls},
        )
        raise
    finally:
        if out_path and out_path.exists():
            out_path.unlink(missing_ok=True)


def run_daily() -> dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return run_window(posted_from=yesterday, posted_to=today)


# ──────────────────────────────────────────────────────────────────────────
# Smart auto-walking enrichment
# ──────────────────────────────────────────────────────────────────────────
# Each daily cron fire does TWO things:
#   1. Forward: (forward_pos, today] — captures yesterday's new Award
#      Notices. Almost always 1 API call (~200 records).
#   2. Backward: (backward_pos - BACKWARD_STEP_DAYS, backward_pos] —
#      walks history one chunk per day until backward_pos <= floor_date.
#      Each backward chunk is ~3-4 API calls.
#
# State persists in bulk_ingest.feed_schedule_config.config JSON. Operator
# never fires manually. Walk completes in ~20 days for the 10-year corpus
# at the SAM_API_KEY non-fed tier (10/day cap, ~5 calls/day used).

BACKWARD_STEP_DAYS = 180  # walk 6 months per fire — fits 3-4 paginated calls
DEFAULT_FLOOR_DATE = "2016-01-01"  # earliest date in the active feed
WALK_STATE_KEY = "backfill_walk_state"


def _read_walk_state() -> dict[str, Any]:
    """Load walk state from bulk_ingest.feed_schedule_config.config JSON.
    Initialize on first call (today as both watermarks; floor 2016-01-01)."""
    import psycopg
    today_iso = datetime.now(timezone.utc).date().isoformat()
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT config->%s
                FROM bulk_ingest.feed_schedule_config
                WHERE source_id = %s AND feed_name = %s
                """,
                (WALK_STATE_KEY, SOURCE_ID, FEED_NAME),
            )
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
    return {
        "forward_pos": today_iso,
        "backward_pos": today_iso,
        "backward_floor": DEFAULT_FLOOR_DATE,
        "walk_complete": False,
        "initialized_at": today_iso,
    }


def _write_walk_state(state: dict[str, Any]) -> None:
    import psycopg
    with psycopg.connect(_db_url()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE bulk_ingest.feed_schedule_config
                SET config = COALESCE(config, '{}'::jsonb)
                          || jsonb_build_object(%s, %s::jsonb),
                    updated_at = NOW()
                WHERE source_id = %s AND feed_name = %s
                """,
                (WALK_STATE_KEY, json.dumps(state, default=str),
                 SOURCE_ID, FEED_NAME),
            )
        conn.commit()


def run_smart_walking() -> dict[str, Any]:
    """One-shot daily fire: forward step + (if not complete) backward step.
    Idempotent — re-runnable any number of times per day; state advances
    only on successful runs."""
    today = datetime.now(timezone.utc).date()
    state = _read_walk_state()
    logger.info("walk state: %s", state)

    results: dict[str, Any] = {"today": today.isoformat(), "fires": []}

    # ── Forward step: from forward_pos → today ──
    forward_pos = date.fromisoformat(state["forward_pos"])
    if forward_pos < today:
        logger.info("FORWARD step: (%s, %s]", forward_pos, today)
        try:
            r = run_window(posted_from=forward_pos, posted_to=today)
            state["forward_pos"] = today.isoformat()
            results["fires"].append({"direction": "forward", **r})
        except Exception as exc:
            logger.error("forward fire failed: %s", str(exc)[:300])
            results["fires"].append({
                "direction": "forward", "error": str(exc)[:300],
            })
            # Don't advance state — try again tomorrow
    else:
        logger.info("FORWARD: forward_pos already at today; skipping")

    # ── Backward step ──
    if state.get("walk_complete"):
        logger.info("BACKWARD: walk_complete=true; skipping")
    else:
        backward_pos = date.fromisoformat(state["backward_pos"])
        floor = date.fromisoformat(state["backward_floor"])
        next_start = max(backward_pos - timedelta(days=BACKWARD_STEP_DAYS), floor)
        if next_start >= backward_pos:
            logger.info("BACKWARD: backward_pos already at/below floor; marking complete")
            state["walk_complete"] = True
        else:
            logger.info("BACKWARD step: (%s, %s]", next_start, backward_pos)
            try:
                r = run_window(posted_from=next_start, posted_to=backward_pos)
                state["backward_pos"] = next_start.isoformat()
                if next_start <= floor:
                    state["walk_complete"] = True
                    logger.info("BACKWARD: floor reached (%s); walk_complete=true", floor)
                results["fires"].append({"direction": "backward", **r})
            except Exception as exc:
                logger.error("backward fire failed: %s", str(exc)[:300])
                results["fires"].append({
                    "direction": "backward", "error": str(exc)[:300],
                })
                # Don't advance state — try again tomorrow

    _write_walk_state(state)
    results["walk_state_after"] = state
    logger.info("walk state after: %s", state)
    return results


def _classify_exception(exc: BaseException) -> str:
    msg = str(exc).lower()
    typ = type(exc).__name__.lower()
    mod = (type(exc).__module__ or "").lower()
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    if "boto" in mod:
        return "r2_failure"
    if "psycopg" in mod or "operationalerror" in typ:
        return "db_failure"
    if "httpx" in mod:
        return "download_failure"
    return "parse_failure"


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--mode", choices=["daily", "backfill", "smart"], required=True,
        help="daily=yesterday-only; backfill=manual window; smart=auto-walking",
    )
    p.add_argument("--window-from", type=str, help="ISO date for backfill window start")
    p.add_argument("--window-to", type=str, help="ISO date for backfill window end")
    args = p.parse_args()

    if args.mode == "smart":
        result = run_smart_walking()
        print(json.dumps(result, default=str, indent=2))
        return
    if args.mode == "daily":
        result = run_daily()
    else:
        if not (args.window_from and args.window_to):
            p.error("--mode backfill requires --window-from and --window-to")
        wf = date.fromisoformat(args.window_from)
        wt = date.fromisoformat(args.window_to)
        # API rejects ranges of exactly 1 year (365 days inclusive trips
        # "max 1 year" enforcement); chunk in 180-day windows for safety.
        if (wt - wf).days > 180:
            results = []
            cur = wf
            while cur < wt:
                next_end = min(cur + timedelta(days=180), wt)
                logger.info("backfill chunk: %s -> %s", cur, next_end)
                results.append(run_window(posted_from=cur, posted_to=next_end))
                cur = next_end + timedelta(days=1)
            print(json.dumps({"chunks": results, "total_chunks": len(results)}, default=str, indent=2))
            return
        result = run_window(posted_from=wf, posted_to=wt)

    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
