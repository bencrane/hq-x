"""FDIC Call Report Quarterly — Modal-hosted institution + financials ingest.

Pulls FDIC BankFind Suite API (api.fdic.gov/banks/{institutions,financials}),
streams to ZSTD-compressed Parquet under R2, and records each slice in
bulk_ingest.feed_ingest_runs (source_id='fdic_call_report').

Object key layout (Hive-partitioned for RisingWave match_pattern globbing):

    s3://dex-raw-landing-zone/fdic/institutions/institutions.parquet
    s3://dex-raw-landing-zone/fdic/financials/year=YYYY/financials_YYYY.parquet

The matching RisingWave SOURCEs are documented in risingwave/fdic_call_report.sql
and applied by scripts/apply_fdic_call_report_rw.py.

Schedule: 15th of Feb/May/Aug/Nov at 06:00 UTC. ~45 days after each fiscal
quarter close, when bank Call Reports are aggregated into the FDIC API.

Each cron firing dispatches three slices:
    1. institutions          (full snapshot, ~28K rows)
    2. financials year=Y     (current calendar year, captures the freshly-filed quarter)
    3. financials year=Y-1   (prior calendar year, captures retroactive amendments + Q4 late filings)

Each slice writes:
    - one ops.fdic_r2_ingest_runs row (existing CLI script's audit ledger; unchanged)
    - one bulk_ingest.feed_ingest_runs row (canonical fleet ledger)

Secrets required (Modal):
    dex-db   — DATABASE_URL pooled to data-engine-x. Reused from FMCSA
                        because the bulk_ingest ledger is shared.
    bulk-ingest-r2    — R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY.

Deploy:
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/fdic_call_report_app.py

Manual run (e.g. smoke test or backfill after Modal cron drop):
    cd ~/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/fdic_call_report_app.py::run_quarterly_ingest
"""

from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timezone
from typing import Any

import modal

app = modal.App("data-engine-x-fdic-call-report-ingest")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ca-certificates")
    .run_commands("update-ca-certificates")
    .pip_install_from_pyproject("modal/pyproject.toml")
    .pip_install("certifi>=2024.7.4")
    .add_local_dir("scripts/dex", remote_path="/root/scripts")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)

FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]

# Sized for the financials slice (largest at ~25K rows × 41 years backfill;
# steady-state quarterly is current+prior year only). 4 GB is comfortable for
# DuckDB Parquet writes with ZSTD compression. 2-hour timeout covers the
# rare case of FDIC API slowness or Modal cold-start lag.
INGEST_MEMORY_MB = 4096
INGEST_TIMEOUT_SECONDS = 2 * 60 * 60

SOURCE_ID = "fdic_call_report"
R2_BUCKET = "dex-raw-landing-zone"


def _bridge_database_url() -> None:
    """Modal secret carries DATABASE_URL; bulk_ingest writers + the CLI ingest
    script both expect DEX_DB_URL_POOLED. Mirror it across so both readers work."""
    if "DATABASE_URL" in os.environ and "DEX_DB_URL_POOLED" not in os.environ:
        os.environ["DEX_DB_URL_POOLED"] = os.environ["DATABASE_URL"]


def _record_run(
    *,
    run_id: str,
    feed_name: str,
    feed_date: date,
    status: str,
    outcome: str,
    started_at: str,
    completed_at: str | None,
    duration_seconds: float | None,
    rows_loaded: int | None,
    r2_object_key: str | None,
    payload_bytes: int | None,
    error_class: str | None,
    error_message: str | None,
    evidence: dict[str, Any],
) -> None:
    """UPSERT a single row into bulk_ingest.feed_ingest_runs.

    Keyed on (run_id, source_id, feed_name, attempt) — last write wins on
    conflict, with evidence merged via JSONB || rather than replaced.
    """
    import json

    import psycopg

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL/DEX_DB_URL_POOLED not set")

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bulk_ingest.feed_ingest_runs (
                    run_id, source_id, feed_name, feed_date, attempt,
                    status, outcome, started_at, completed_at, duration_seconds,
                    rows_loaded, landing_zone, r2_bucket, r2_object_key,
                    payload_format, payload_bytes,
                    error_class, error_message, evidence
                ) VALUES (
                    %s, %s, %s, %s, 1,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s::jsonb
                )
                ON CONFLICT (run_id, source_id, feed_name, attempt) DO UPDATE
                SET
                    status = EXCLUDED.status,
                    outcome = EXCLUDED.outcome,
                    completed_at = EXCLUDED.completed_at,
                    duration_seconds = EXCLUDED.duration_seconds,
                    rows_loaded = EXCLUDED.rows_loaded,
                    landing_zone = EXCLUDED.landing_zone,
                    r2_bucket = EXCLUDED.r2_bucket,
                    r2_object_key = EXCLUDED.r2_object_key,
                    payload_format = EXCLUDED.payload_format,
                    payload_bytes = EXCLUDED.payload_bytes,
                    error_class = EXCLUDED.error_class,
                    error_message = EXCLUDED.error_message,
                    evidence = COALESCE(bulk_ingest.feed_ingest_runs.evidence, '{}'::jsonb)
                               || EXCLUDED.evidence,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    SOURCE_ID,
                    feed_name,
                    feed_date.isoformat(),
                    status,
                    outcome,
                    started_at,
                    completed_at,
                    duration_seconds,
                    rows_loaded,
                    "r2",
                    R2_BUCKET,
                    r2_object_key,
                    "parquet_zstd",
                    payload_bytes,
                    error_class,
                    error_message,
                    json.dumps(evidence, default=str),
                ),
            )
        conn.commit()


def _read_latest_ops_row(*, feed_kind: str, year: int | None) -> dict[str, Any] | None:
    """Read the most recent ops.fdic_r2_ingest_runs row for the given slice.

    The CLI ingest function writes to ops.fdic_r2_ingest_runs; we read it back
    to get the row count + bytes for the canonical bulk_ingest record.
    """
    import psycopg
    from psycopg.rows import dict_row

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL/DEX_DB_URL_POOLED not set")

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            if year is None:
                cur.execute(
                    """
                    SELECT id, status, rows_fetched, parquet_row_count,
                           parquet_bytes_written, r2_key, r2_total_bytes,
                           started_at, finished_at, duration_seconds, error_message
                    FROM ops.fdic_r2_ingest_runs
                    WHERE feed_kind = %s AND year IS NULL
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (feed_kind,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, status, rows_fetched, parquet_row_count,
                           parquet_bytes_written, r2_key, r2_total_bytes,
                           started_at, finished_at, duration_seconds, error_message
                    FROM ops.fdic_r2_ingest_runs
                    WHERE feed_kind = %s AND year = %s
                    ORDER BY started_at DESC LIMIT 1
                    """,
                    (feed_kind, year),
                )
            return cur.fetchone()


def _classify_exception(exc: BaseException) -> str:
    message = (str(exc) or "").lower()
    if "timeout" in message or "timed out" in message:
        return "timeout"
    if "connection" in message or "network" in message or "dns" in message:
        return "download_failure"
    if "ssl" in message or "certificate" in message:
        return "download_failure"
    if "http" in message and any(s in message for s in ("4", "5")):
        return "download_failure"
    if "parse" in message or "json" in message:
        return "parse_failure"
    if "psycopg" in message or "postgres" in message or "database" in message:
        return "db_failure"
    return "unknown"


def _run_slice(
    *,
    feed_name: str,
    feed_kind: str,
    year: int | None,
    feed_date: date,
    r2_object_key: str,
) -> dict[str, Any]:
    """Dispatch one slice (institutions or financials-year), recording start +
    completion in bulk_ingest.feed_ingest_runs. Returns a summary dict."""
    from pathlib import Path

    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc).isoformat()

    _record_run(
        run_id=run_id,
        feed_name=feed_name,
        feed_date=feed_date,
        status="running",
        outcome="never_ran",
        started_at=started_at,
        completed_at=None,
        duration_seconds=None,
        rows_loaded=None,
        r2_object_key=r2_object_key,
        payload_bytes=None,
        error_class=None,
        error_message=None,
        evidence={
            "feed_kind": feed_kind,
            "year": year,
            "trigger": "schedule",
        },
    )

    sys.path.insert(0, "/root/scripts")
    from run_fdic_call_report_r2_ingest import (  # noqa: E402
        FinancialsSlice,
        ingest_financials_year,
        ingest_institutions,
    )

    workdir = Path("/tmp/fdic_r2_ingest")
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        if feed_kind == "institutions":
            rc = ingest_institutions(workdir=workdir, dry_run=False)
        elif feed_kind == "financials":
            assert year is not None
            rc = ingest_financials_year(
                FinancialsSlice(year=year), workdir=workdir, dry_run=False,
            )
        else:
            raise ValueError(f"unknown feed_kind: {feed_kind}")
    except Exception as exc:  # noqa: BLE001
        completed_at = datetime.now(timezone.utc).isoformat()
        duration_seconds = (
            datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
        ).total_seconds()
        _record_run(
            run_id=run_id,
            feed_name=feed_name,
            feed_date=feed_date,
            status="failed",
            outcome="failed",
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
            rows_loaded=None,
            r2_object_key=r2_object_key,
            payload_bytes=None,
            error_class=_classify_exception(exc),
            error_message=str(exc)[:4000],
            evidence={"feed_kind": feed_kind, "year": year, "rc": "exception"},
        )
        raise

    completed_at = datetime.now(timezone.utc).isoformat()
    duration_seconds = (
        datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at)
    ).total_seconds()

    ops_row = _read_latest_ops_row(feed_kind=feed_kind, year=year)
    rows_loaded = int(ops_row["parquet_row_count"] or 0) if ops_row else 0
    payload_bytes = int(ops_row["r2_total_bytes"] or 0) if ops_row else 0
    ops_status = ops_row["status"] if ops_row else None

    if rc != 0 or ops_status == "failed":
        outcome = "failed"
        status = "failed"
        error_class = _classify_exception(RuntimeError(ops_row["error_message"] or "")) if ops_row else "unknown"
        error_message = (ops_row.get("error_message") if ops_row else None) or f"rc={rc}"
    elif ops_status == "no_change" or rows_loaded == 0:
        outcome = "succeeded_with_zero_new_rows"
        status = "completed"
        error_class = None
        error_message = None
    else:
        outcome = "succeeded_with_changes"
        status = "completed"
        error_class = None
        error_message = None

    _record_run(
        run_id=run_id,
        feed_name=feed_name,
        feed_date=feed_date,
        status=status,
        outcome=outcome,
        started_at=started_at,
        completed_at=completed_at,
        duration_seconds=duration_seconds,
        rows_loaded=rows_loaded,
        r2_object_key=r2_object_key,
        payload_bytes=payload_bytes,
        error_class=error_class,
        error_message=error_message,
        evidence={
            "feed_kind": feed_kind,
            "year": year,
            "ops_run_id": str(ops_row["id"]) if ops_row else None,
            "ops_status": ops_status,
        },
    )

    return {
        "feed_name": feed_name,
        "feed_kind": feed_kind,
        "year": year,
        "run_id": run_id,
        "rows_loaded": rows_loaded,
        "payload_bytes": payload_bytes,
        "outcome": outcome,
        "duration_seconds": duration_seconds,
    }


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=INGEST_TIMEOUT_SECONDS,
    memory=INGEST_MEMORY_MB,
    # [migrated 2026-05-30 -> Trigger.dev (batch A)] schedule=modal.Cron("0 6 15 2,5,8,11 *"),  # 15th of Feb/May/Aug/Nov, 06:00 UTC
)
def run_quarterly_ingest() -> dict[str, Any]:
    """Quarterly FDIC ingest entrypoint. Refreshes:
        1. institutions snapshot
        2. current calendar year financials
        3. prior calendar year financials (catches Q4 late filings + retroactive amendments)
    Each slice writes its own bulk_ingest.feed_ingest_runs row.
    """
    _bridge_database_url()

    today = datetime.now(timezone.utc).date()
    current_year = today.year
    prior_year = current_year - 1

    results: list[dict[str, Any]] = []

    import sys as _sys
    import uuid as _uuid
    _sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    run_id = str(_uuid.uuid4())
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function="run_quarterly_ingest",
        run_id=run_id,
    ) as hb:
        hb.set_stage("slice_institutions")
        results.append(
            _run_slice(
                feed_name="institutions",
                feed_kind="institutions",
                year=None,
                feed_date=today,
                r2_object_key="fdic/institutions/institutions.parquet",
            )
        )

        for year in (current_year, prior_year):
            hb.set_stage(f"slice_financials_{year}")
            results.append(
                _run_slice(
                    feed_name="financials",
                    feed_kind="financials",
                    year=year,
                    feed_date=date(year, 12, 31),
                    r2_object_key=f"fdic/financials/year={year}/financials_{year}.parquet",
                )
            )

    return {
        "run_id": run_id,
        "today": today.isoformat(),
        "slices": results,
    }
