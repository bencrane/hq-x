"""Standalone reaper for orphaned 'running' rows in the bulk_ingest ledgers.

Runs every 15 minutes. Identifies rows where status='running' AND
started_at + REAP_THRESHOLD < NOW(), flips them to status='timed_out'
with outcome='failed', error_class='timeout'. /admin/ingest then
classifies them as RETRYING (or LATE if past expected cadence).

Why externalized (per Backend Harness Architectural Standards #5):
  The previous in-orchestrator reaper (manifest.mark_lingering_as_timed_out)
  only fires at the end of the orchestrator's window. If the orchestrator
  container itself crashes mid-run — the exact failure mode this is meant
  to catch — the lingering 'running' rows survive forever, and Modal-retry
  duplicate writes can corrupt downstream state. Decoupling reaping from
  the orchestrator's lifecycle eliminates that hazard.

Why 15-minute cadence: matches the FMCSA heartbeat. A faster reaper
ensures that if a Modal container hits a memory limit or timeouts, the
LATE/RETRYING surface in the dashboard reflects within one heartbeat
cycle, preventing orphan writes from hitting the database under a
re-dispatched run_id.

Threshold (REAP_THRESHOLD_HOURS=7): Modal's xl bucket has a 6h hard
timeout, so a row 'running' for 7+ hours is definitively orphaned —
Modal would have killed the worker by then.

Touches both ledgers:
  - entities.fmcsa_ingest_runs (legacy FMCSA ledger)
  - bulk_ingest.feed_ingest_runs (cross-source primitive)

Modal secret 'dex-db' carries DATABASE_URL pooled to data-engine-x;
the same secret used by fmcsa_ingest_app.py.

Deploy:
    cd ~/Desktop/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal deploy modal/reap_orphans_app.py

One-shot run (without waiting for the next 15-min tick):
    cd ~/Desktop/hq-all/apps/data-engine-x && \\
        doppler run --project hq-all --config prd -- \\
        modal run modal/reap_orphans_app.py::reap_orphans
"""

from __future__ import annotations

import os
from typing import Any

import modal

app = modal.App("data-engine-x-reap-orphans")

image = modal.Image.debian_slim(python_version="3.11").pip_install_from_pyproject(
    "pyproject.toml"
)

REAP_THRESHOLD_HOURS = 7
PENDING_REAP_THRESHOLD_MINUTES = 30

REAPER_SQL_FMCSA = """
UPDATE entities.fmcsa_ingest_runs
SET
    status = 'timed_out',
    outcome = 'failed',
    completed_at = NOW(),
    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
    error_class = 'timeout',
    error_message = COALESCE(
        error_message,
        'orphan reaper: container crashed or Modal scheduler dropped the run'
    ),
    evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object(
        'reaped_by', 'orphan_reaper_cron',
        'reaped_at', NOW()::text,
        'reap_threshold_hours', %s
    ),
    updated_at = NOW()
WHERE status = 'running'
  AND started_at IS NOT NULL
  AND started_at < NOW() - make_interval(hours => %s)
RETURNING run_id::text, feed_name, feed_date::text
"""

REAPER_SQL_PENDING_BULK_INGEST = """
UPDATE bulk_ingest.feed_ingest_runs
SET
    status = 'timed_out',
    outcome = 'failed',
    completed_at = NOW(),
    duration_seconds = 0,
    error_class = 'orchestrator_dropped',
    error_message = COALESCE(
        error_message,
        'orphan reaper: pending row never transitioned to running (orchestrator dispatch dropped before mark_running fired)'
    ),
    evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object(
        'reaped_by', 'orphan_reaper_cron_pending_branch',
        'reaped_at', NOW()::text,
        'pending_threshold_minutes', %s
    ),
    updated_at = NOW()
WHERE status = 'pending'
  AND started_at IS NULL
  AND created_at < NOW() - make_interval(mins => %s)
RETURNING source_id, feed_name, feed_date::text, run_id::text
"""

REAPER_SQL_BULK_INGEST = """
UPDATE bulk_ingest.feed_ingest_runs
SET
    status = 'timed_out',
    outcome = 'failed',
    completed_at = NOW(),
    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
    error_class = 'timeout',
    error_message = COALESCE(
        error_message,
        'orphan reaper: container crashed or Modal scheduler dropped the run'
    ),
    evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object(
        'reaped_by', 'orphan_reaper_cron',
        'reaped_at', NOW()::text,
        'reap_threshold_hours', %s
    ),
    updated_at = NOW()
WHERE status = 'running'
  AND started_at IS NOT NULL
  AND started_at < NOW() - make_interval(hours => %s)
RETURNING source_id, feed_name, feed_date::text
"""


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=[modal.Secret.from_name("hqx-db")],
    timeout=300,
    # [migrated 2026-05-30 -> Trigger.dev (derived/bridge/infra)] schedule=modal.Cron("*/15 * * * *"),
)
def reap_orphans() -> dict[str, Any]:
    """Reap any 'running' row past the threshold across both ledgers."""
    import psycopg
    from psycopg.rows import dict_row

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is not set")

    fmcsa_reaped: list[dict[str, Any]] = []
    bulk_reaped: list[dict[str, Any]] = []
    pending_reaped: list[dict[str, Any]] = []

    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                REAPER_SQL_FMCSA, (REAP_THRESHOLD_HOURS, REAP_THRESHOLD_HOURS)
            )
            fmcsa_reaped = [dict(row) for row in cur.fetchall()]
            cur.execute(
                REAPER_SQL_BULK_INGEST, (REAP_THRESHOLD_HOURS, REAP_THRESHOLD_HOURS)
            )
            bulk_reaped = [dict(row) for row in cur.fetchall()]
            # Pending sweep — orphans where the orchestrator's spawn dropped
            # between insert_pending_row and mark_running. These rows have
            # no started_at, so the running-branch reaper above never sees
            # them. Adversarial audit 2026-05-08 confirmed 25 such orphans
            # accumulated in ~1h. Without this branch they live forever and
            # the dispatch view classifies them as RETRYING — fictional state.
            cur.execute(
                REAPER_SQL_PENDING_BULK_INGEST,
                (PENDING_REAP_THRESHOLD_MINUTES, PENDING_REAP_THRESHOLD_MINUTES),
            )
            pending_reaped = [dict(row) for row in cur.fetchall()]
        conn.commit()

    summary = {
        "reap_threshold_hours": REAP_THRESHOLD_HOURS,
        "pending_reap_threshold_minutes": PENDING_REAP_THRESHOLD_MINUTES,
        "fmcsa_reaped_count": len(fmcsa_reaped),
        "fmcsa_reaped": fmcsa_reaped,
        "bulk_ingest_reaped_count": len(bulk_reaped),
        "bulk_ingest_reaped": bulk_reaped,
        "pending_reaped_count": len(pending_reaped),
        "pending_reaped": pending_reaped,
    }
    print(
        f"[reap_orphans] fmcsa={len(fmcsa_reaped)} "
        f"bulk_ingest_running={len(bulk_reaped)} "
        f"bulk_ingest_pending={len(pending_reaped)} "
        f"thresholds=running:{REAP_THRESHOLD_HOURS}h "
        f"pending:{PENDING_REAP_THRESHOLD_MINUTES}m"
    )
    return summary
