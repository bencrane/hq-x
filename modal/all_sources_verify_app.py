"""All-sources verify — Modal cron monitoring every active data source.

Cycle: usaspending-poison-file-class-fix-v1 (2026-05-13).

NEW SIBLING to usaspending_daily_verify_app.py — does NOT modify the
existing per-source cron, which remains as a source-specific escalation
path. This generalizes the verify pattern to iterate ALL ops.data_sources
rows with status='active', checking:

  (a) An ingest run fired within the cadence + grace window
      (daily→48h, weekly→192h, monthly→840h, etc.)
  (b) The source's most-recent R2 objects in the last 7 partitions
      contain no 0-byte poison files.

One ops.data_source_ingest_runs row is inserted per source per tick
(status='succeeded' or 'failed', writer='all-sources-verify'). This
is ~3,750 rows/day for ~39 active sources × 4 ticks/hour, acceptable
for an observability table. Retention prune is a follow-up cycle if needed.

On failure for any source, also inserts an ops.alert_emissions row if
an active ingest_failed Telegram subscription exists for that source.

Schedule: every 15 minutes — satisfies "dashboard sees breaks within
minutes, not next-day." The directive said: "Run frequently enough that
dashboard sees breaks within minutes."

Secrets required (Modal, shared with other DEX crons):
    dex-db  — DEX_DB_URL_DIRECT for ops writes
    bulk-ingest-r2   — R2 endpoint + keys for poison-file scan

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/all_sources_verify_app.py

Manual invocation:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/all_sources_verify_app.py

Rollback:
    git revert <merge-SHA> && doppler run --project hq-all --config prd -- \\
        modal app stop data-engine-x-all-sources-verify
    (Stopping is NOT automatic on git-revert — operator must stop manually.)
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import modal

app = modal.App("data-engine-x-all-sources-verify")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install("psycopg[binary]", "boto3")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

FUNCTION_TIMEOUT_SECONDS = 10 * 60  # 10 min generous for ~40 sources
WRITER_LABEL = "all-sources-verify"
R2_BUCKET = "dex-raw-landing-zone"

# Cadence → grace window in hours (first match wins; 'None' = no staleness check)
_GRACE_HOURS: dict[str, int | None] = {
    "daily": 48,
    "weekly": 168 + 24,       # 7d + 1d grace
    "monthly": 24 * 35,       # 35d grace
    "quarterly": 24 * 100,
    "annual": 24 * 380,
    "biennial": 24 * 760,
    "one-shot": None,
    "on-demand": None,
}

logger = logging.getLogger(__name__)


def _bridge_database_url() -> None:
    """Modal 'dex-db' secret carries DATABASE_URL; bridge to DEX vars."""
    if "DEX_DB_URL_POOLED" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]
    if "DEX_DB_URL_DIRECT" not in os.environ and "DATABASE_URL" in os.environ:
        os.environ["DEX_DB_URL_DIRECT"] = os.environ["DATABASE_URL"]


def _get_db_url() -> str:
    url = os.environ.get("DEX_DB_URL_DIRECT") or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("no DB URL (need DEX_DB_URL_DIRECT or DATABASE_URL)")
    return url


def _get_s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _get_active_sources(db_url: str) -> list[dict]:
    """Return list of active sources with cadence + r2_prefix."""
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.source_id::text,
                    s.display_name,
                    s.source_slug,
                    s.refresh_cadence,
                    c.r2_prefix
                FROM ops.data_sources s
                LEFT JOIN ops.data_source_catalog c
                    ON c.source_slug = s.source_slug
                WHERE s.status = 'active'
                ORDER BY s.display_name
                """
            )
            rows = cur.fetchall()
            cols = ["source_id", "display_name", "source_slug", "refresh_cadence", "r2_prefix"]
            return [dict(zip(cols, r)) for r in rows]


def _get_last_good_run_at(db_url: str, source_id: str) -> datetime | None:
    """Return the most recent succeeded run timestamp for this source, or None."""
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT MAX(completed_at)
                FROM ops.data_source_ingest_runs
                WHERE source_id = %s::uuid
                  AND status = 'succeeded'
                """,
                (source_id,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None


def _scan_poison_files(s3, r2_prefix: str | None) -> list[str]:
    """Return list of 0-byte R2 keys in the most recent 7 partitions of r2_prefix.

    Implements the bounded scan described in audit s6:
      1. LIST objects/sub-prefixes under r2_prefix.
      2. If sub-prefixes present (partitioned source), select 7 most-recent
         by lexicographic sort (descending), then LIST each sub-prefix.
      3. Collect all keys with Size == 0.
    """
    if not r2_prefix:
        return []

    try:
        resp = s3.list_objects_v2(
            Bucket=R2_BUCKET,
            Prefix=r2_prefix,
            MaxKeys=100,
            Delimiter="/",
        )
    except Exception as exc:
        logger.warning("R2 LIST failed for prefix=%s: %s", r2_prefix, exc)
        return []

    poison: list[str] = []

    # Direct objects in the prefix (non-partitioned sources)
    for obj in resp.get("Contents", []):
        if obj.get("Size", -1) == 0:
            poison.append(obj["Key"])

    # Sub-prefixes (partitioned sources like usaspending/contracts/api-delta/date=*/)
    sub_prefixes = [p["Prefix"] for p in resp.get("CommonPrefixes", [])]
    if sub_prefixes:
        # Sort descending (lexicographic; date=YYYY-MM-DD sorts correctly)
        sub_prefixes.sort(reverse=True)
        for sub in sub_prefixes[:7]:
            try:
                sub_resp = s3.list_objects_v2(
                    Bucket=R2_BUCKET,
                    Prefix=sub,
                    MaxKeys=10,
                )
            except Exception as exc:
                logger.warning("R2 LIST failed for sub=%s: %s", sub, exc)
                continue
            for obj in sub_resp.get("Contents", []):
                if obj.get("Size", -1) == 0:
                    poison.append(obj["Key"])

    return poison


def _check_one_source(
    source: dict,
    db_url: str,
    s3,
    now: datetime,
) -> dict:
    """Verify one source. Returns a result dict with status and fail_reasons."""
    source_id = source["source_id"]
    display_name = source["display_name"]
    refresh_cadence = (source.get("refresh_cadence") or "").lower()
    r2_prefix = source.get("r2_prefix")

    fail_reasons: list[str] = []
    poison_files: list[str] = []

    # (a) Staleness check: last good run within cadence+grace
    grace_hours = _GRACE_HOURS.get(refresh_cadence)  # None = no staleness check
    if grace_hours is not None:
        last_good = _get_last_good_run_at(db_url, source_id)
        if last_good is None:
            fail_reasons.append(f"stale_no_run_ever (cadence={refresh_cadence})")
        else:
            age_hours = (now - last_good.replace(tzinfo=timezone.utc if last_good.tzinfo is None else last_good.tzinfo)).total_seconds() / 3600
            if age_hours > grace_hours:
                fail_reasons.append(
                    f"stale_no_recent_success (last_good={last_good.isoformat()[:19]}Z, "
                    f"age={age_hours:.1f}h, grace={grace_hours}h, cadence={refresh_cadence})"
                )

    # (b) Poison-file scan: any 0-byte objects in last 7 partitions
    poison_files = _scan_poison_files(s3, r2_prefix)
    if poison_files:
        fail_reasons.append(
            f"poison_file_present ({len(poison_files)} 0-byte object(s): "
            f"{poison_files[:3]}{'...' if len(poison_files) > 3 else ''})"
        )

    status = "failed" if fail_reasons else "succeeded"
    return {
        "source_id": source_id,
        "display_name": display_name,
        "status": status,
        "fail_reasons": fail_reasons,
        "poison_files": poison_files,
    }


def _record_source_run(
    db_url: str,
    source_id: str,
    status: str,
    fail_reasons: list[str],
    poison_files: list[str],
    started_at: str,
) -> str:
    """Insert one ops.data_source_ingest_runs row. Returns run_id."""
    import psycopg

    completed_at = datetime.now(timezone.utc).isoformat()
    run_metadata = json.dumps(
        {
            "writer": WRITER_LABEL,
            "fail_reasons": fail_reasons,
            "poison_files": poison_files,
            "started_at": started_at,
        }
    )
    error_message = "; ".join(fail_reasons) if fail_reasons else None

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ops.data_source_ingest_runs
                    (source_id, started_at, completed_at, status, run_metadata, error_message)
                VALUES (%s::uuid, %s::timestamptz, %s::timestamptz,
                        %s::data_source_run_status, %s::jsonb, %s)
                RETURNING run_id
                """,
                (source_id, started_at, completed_at, status, run_metadata, error_message),
            )
            row = cur.fetchone()
            conn.commit()
            return str(row[0]) if row else "unknown"


def _emit_alert_for_source(db_url: str, source_id: str, display_name: str, run_id: str, fail_reasons: list[str]) -> None:
    """Insert ops.alert_emissions row if an ingest_failed subscription exists."""
    import psycopg

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.alert_id
                  FROM ops.alert_subscriptions a
                 WHERE a.source_id = %s::uuid
                   AND a.alert_kind = 'ingest_failed'
                   AND a.channel = 'telegram'
                   AND a.enabled = true
                 LIMIT 1
                """,
                (source_id,),
            )
            row = cur.fetchone()
            if row is None:
                return
            alert_id = row[0]
            cur.execute(
                """
                INSERT INTO ops.alert_emissions (alert_id, alert_payload, delivery_status)
                VALUES (%s, %s::jsonb, 'sent'::alert_delivery_status)
                """,
                (
                    alert_id,
                    json.dumps(
                        {
                            "writer": WRITER_LABEL,
                            "run_id": run_id,
                            "source_id": source_id,
                            "display_name": display_name,
                            "fail_reasons": fail_reasons,
                            "summary": f"All-sources-verify: {display_name} FAILED — {'; '.join(fail_reasons[:2])}",
                        }
                    ),
                ),
            )
            conn.commit()


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=FUNCTION_TIMEOUT_SECONDS,
    schedule=modal.Cron("*/15 * * * *"),  # every 15 min
)
def run_all_sources_verify() -> dict:
    """Verify every active source. Insert one run row per source. Emit alerts on failure."""
    _bridge_database_url()
    db_url = _get_db_url()
    s3 = _get_s3_client()
    now = datetime.now(timezone.utc)
    started_at = now.isoformat()

    sources = _get_active_sources(db_url)
    logger.info("checking %d active sources", len(sources))

    results: list[dict] = []

    def _check_and_record(source: dict) -> dict:
        result = _check_one_source(source=source, db_url=db_url, s3=s3, now=now)
        run_id = _record_source_run(
            db_url=db_url,
            source_id=result["source_id"],
            status=result["status"],
            fail_reasons=result["fail_reasons"],
            poison_files=result["poison_files"],
            started_at=started_at,
        )
        result["run_id"] = run_id
        if result["status"] == "failed":
            try:
                _emit_alert_for_source(
                    db_url=db_url,
                    source_id=result["source_id"],
                    display_name=result["display_name"],
                    run_id=run_id,
                    fail_reasons=result["fail_reasons"],
                )
            except Exception as exc:
                logger.warning("alert emission failed for %s: %s", result["display_name"], exc)
        return result

    # Parallelize across sources (bounded workers — DB connections are the limit)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_check_and_record, src): src for src in sources}
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                src = futures[fut]
                logger.error("check_one_source raised for %s: %s", src["display_name"], exc)
                results.append({
                    "source_id": src["source_id"],
                    "display_name": src["display_name"],
                    "status": "failed",
                    "fail_reasons": [f"verify_exception: {exc}"],
                    "poison_files": [],
                    "run_id": "error",
                })

    passed = sum(1 for r in results if r["status"] == "succeeded")
    failed = sum(1 for r in results if r["status"] == "failed")

    logger.info(
        "all-sources-verify complete: %d passed, %d failed of %d sources",
        passed, failed, len(sources),
    )

    return {
        "started_at": started_at,
        "total_sources": len(sources),
        "passed": passed,
        "failed": failed,
        "failed_sources": [r["display_name"] for r in results if r["status"] == "failed"],
    }


@app.local_entrypoint()
def main() -> None:
    """Manual entrypoint for one-off testing (`modal run`)."""
    out = run_all_sources_verify.remote()
    print(json.dumps(out, indent=2, default=str))
