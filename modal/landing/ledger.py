"""Canonical writer for `bulk_ingest.feed_ingest_runs`.

Every Modal cron that writes to this ledger MUST go through :func:`record_run`
here. Per ``apps/data-engine-x/modal/SECRETS.md`` and the 2026-05-25 systemic
critique (``reports/2026-05-25-modal-setup-systemic-critique-via-mined-harness-
reports.md §P0-2``), the previous shape — 13 apps each carrying their own
inline ``_record_run`` block — produced silent drift across sister crons. PR
#698 was the canonical instance: a kwarg added in one app's writer broke its
sister crons because the writer wasn't a single source of truth.

This module is the single source of truth.

Design notes:
- :func:`compute_outcome` emits the **post-PR-C** outcome label space (14
  values; CHECK constraint extended in migration
  20260525040000_extend_feed_ingest_outcomes_and_dry_run.sql). The audit's
  full decision tree is implemented — each (rows_loaded, upstream_probe,
  fanout, dry_run, skipped) combination maps to a distinct outcome label.
- :class:`RunResult` carries the structured run-state that the decision tree
  routes on: ``rows_loaded``, ``upstream_probe_returned_data``,
  ``fanout_total``, ``fanout_failed``, ``is_dry_run``, ``skipped_idempotent``.

Caller contract:
- ``result=None`` + ``error_class=None``  → status='running', outcome='never_ran'
  (the initial pre-record before work starts).
- ``result=RunResult(...)``               → status='completed' (or 'skipped'),
  outcome derived from :func:`compute_outcome`.
- ``error_class=<str>`` + ``error_message=<str>`` → status='failed', outcome
  routed via ``_FAILURE_BUCKET_TO_OUTCOME`` (failed_db_error, failed_r2_error,
  failed_upstream_error, or failed_unknown).

Never pass an ``outcome`` string directly — the helper computes it.

Exception classification: :func:`classify_exception` carries the legacy bucket
logic verbatim from the 3 USAspending sister apps (``timeout``, ``r2_failure``,
``db_failure``, ``download_failure``, ``parse_failure``, ``unknown``). The
failure-class → outcome map at ``_FAILURE_BUCKET_TO_OUTCOME`` converts those
buckets into the new ``failed_*`` outcome labels.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any

# psycopg is the canonical Postgres client across DEX; imported lazily inside
# record_run so the module can be unit-tested without a DB connection.


@dataclass(frozen=True)
class RunResult:
    """Structured result of one cron run, for ledger outcome computation.

    Field semantics:
    - ``rows_loaded``: final rows persisted to the landing zone (Lance / R2 /
      Postgres). 0 = nothing landed.
    - ``upstream_probe_returned_data``: ``True`` if the upstream returned >=1
      row when probed during this run; ``False`` if the upstream was probed and
      returned 0 rows (confirms genuine emptiness); ``None`` if the script did
      not probe (we can't distinguish "upstream empty" from "our query missed").
    - ``fanout_total``: number of workers/batches dispatched via
      ``modal.Function.map`` or equivalent fan-out. 0 if no fan-out shape.
    - ``fanout_failed``: number of workers/batches that raised or returned no
      data. Used to compute ``fanout_failure_rate``.
    - ``is_dry_run``: ``True`` if the run was invoked with ``--dry-run`` (no
      writes to production sinks). PR-C will route these to a separate outcome
      label so the daily-verify cron + audience-freshness checks can filter them.
    - ``skipped_idempotent``: ``True`` if the ingest detected a prior
      ``status='succeeded'`` run for the same feed_date AND short-circuited
      without re-running. Distinct from rows_loaded=0: a skip means we DIDN'T
      try, an empty-load means we DID try and got nothing. ``status='skipped'``
      is the legacy bulk_ingest convention.
    """

    rows_loaded: int
    upstream_probe_returned_data: bool | None = None
    fanout_total: int = 0
    fanout_failed: int = 0
    is_dry_run: bool = False
    skipped_idempotent: bool = False


# Threshold above which the fanout failure rate triggers the
# `succeeded_partial_fanout_degraded` outcome label. Per audit §"P0-2"
# decision tree exact value.
PARTIAL_FANOUT_FAILURE_RATE_THRESHOLD = 0.05


def compute_outcome(r: RunResult) -> tuple[str, str]:
    """Compute ``(status, outcome)`` from a :class:`RunResult`.

    Emits the **post-PR-C** outcome label space (14 values; CHECK constraint
    extended in migration 20260525040000). The decision tree distinguishes
    every state the audit identified as load-bearing:

    - ``skipped_idempotent`` (work was already done; we short-circuited)
      → ('skipped', 'succeeded_with_zero_new_rows')  -- legacy label kept for
        the skip case so existing readers continue to recognize it.
    - ``is_dry_run`` (the run was invoked with --dry-run; no production writes)
      → ('completed', 'succeeded_dry_run')
    - ``rows_loaded == 0`` AND ``upstream_probe_returned_data is False``
      (we asked the upstream and it confirmed zero rows)
      → ('completed', 'succeeded_with_zero_new_rows_upstream_confirmed_empty')
    - ``rows_loaded == 0`` AND ``upstream_probe_returned_data is None or True``
      (we didn't probe, or we did and it had data but we missed it)
      → ('completed', 'succeeded_with_zero_new_rows_upstream_unknown')
    - ``rows_loaded > 0`` AND fanout failure rate ≥
      ``PARTIAL_FANOUT_FAILURE_RATE_THRESHOLD`` (5% by default)
      → ('completed', 'succeeded_partial_fanout_degraded')
    - ``rows_loaded > 0`` (clean success)
      → ('completed', 'succeeded_with_changes')
    """
    if r.skipped_idempotent:
        return "skipped", "succeeded_with_zero_new_rows"
    if r.is_dry_run:
        return "completed", "succeeded_dry_run"
    if r.rows_loaded == 0:
        if r.upstream_probe_returned_data is False:
            return "completed", "succeeded_with_zero_new_rows_upstream_confirmed_empty"
        return "completed", "succeeded_with_zero_new_rows_upstream_unknown"
    # rows_loaded > 0
    if r.fanout_total > 0:
        fail_rate = r.fanout_failed / r.fanout_total
        if fail_rate >= PARTIAL_FANOUT_FAILURE_RATE_THRESHOLD:
            return "completed", "succeeded_partial_fanout_degraded"
    return "completed", "succeeded_with_changes"


# Mapping from `classify_exception` bucket → the specific failure outcome label.
# Buckets not listed fall back to ``failed_unknown``. The legacy ``'failed'``
# outcome label is reserved for ``error_class=None`` (no classification).
_FAILURE_BUCKET_TO_OUTCOME: dict[str, str] = {
    "timeout":          "failed_upstream_error",
    "download_failure": "failed_upstream_error",
    "r2_failure":       "failed_r2_error",
    "db_failure":       "failed_db_error",
    "parse_failure":    "failed_unknown",
    "unknown":          "failed_unknown",
}


def classify_exception(exc: BaseException) -> str:
    """Bucket an exception into one of: timeout, r2_failure, db_failure,
    download_failure, parse_failure, unknown.

    Carries the legacy logic from the 3 USAspending sister apps verbatim.
    """
    msg = (str(exc) or "").lower()
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    name = type(exc).__name__.lower()
    mod = (type(exc).__module__ or "").lower()
    if "boto" in mod or "s3" in mod or "r2" in msg:
        return "r2_failure"
    if "psycopg" in mod or "operationalerror" in name:
        return "db_failure"
    if "httpx" in mod or "connection" in name or "requests" in mod:
        return "download_failure"
    if name in {"valueerror", "keyerror", "typeerror"}:
        return "parse_failure"
    return "unknown"


def _resolve_status_outcome(
    *,
    result: RunResult | None,
    error_class: str | None,
) -> tuple[str, str]:
    """Resolve the (status, outcome) pair from caller inputs.

    - ``result=None, error_class=None``  → ('running', 'never_ran')
    - ``error_class`` set, mapped via _FAILURE_BUCKET_TO_OUTCOME
                                          → ('failed', 'failed_<class>')
    - ``error_class`` set, unmapped       → ('failed', 'failed_unknown')
    - ``result=RunResult``                → :func:`compute_outcome`
    """
    if error_class is not None:
        return "failed", _FAILURE_BUCKET_TO_OUTCOME.get(error_class, "failed_unknown")
    if result is None:
        return "running", "never_ran"
    return compute_outcome(result)


def record_run(
    *,
    source_id: str,
    feed_name: str,
    run_id: str,
    feed_date: date,
    started_at: str,
    completed_at: str | None,
    result: RunResult | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    duration_seconds: float | None = None,
    landing_zone: str = "r2",
    r2_bucket: str | None = None,
    r2_object_key: str | None = None,
    payload_format: str | None = None,
    payload_bytes: int | None = None,
    evidence: dict[str, Any] | None = None,
) -> None:
    """UPSERT one row into ``bulk_ingest.feed_ingest_runs``.

    The canonical ledger writer. Derives ``(status, outcome)`` from the
    ``result`` / ``error_class`` inputs — callers do NOT pass an outcome
    string directly.

    Connection:
    - Reads ``$DEX_DB_URL_POOLED`` (pgbouncer); falls back to
      ``$DATABASE_URL`` for callers that haven't migrated yet.
    - Raises ``RuntimeError`` if neither env var is set.
    """
    import psycopg  # imported lazily for unit-testability

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DEX_DB_URL_POOLED / DATABASE_URL not set")

    status, outcome = _resolve_status_outcome(result=result, error_class=error_class)

    # Merge result fields into evidence for forensic traceability — even when
    # the (status, outcome) collapses richer state into a legacy label.
    evidence_merged: dict[str, Any] = dict(evidence or {})
    if result is not None:
        evidence_merged.setdefault("rows_loaded", result.rows_loaded)
        evidence_merged.setdefault("upstream_probe_returned_data", result.upstream_probe_returned_data)
        evidence_merged.setdefault("fanout_total", result.fanout_total)
        evidence_merged.setdefault("fanout_failed", result.fanout_failed)
        evidence_merged.setdefault("is_dry_run", result.is_dry_run)
        evidence_merged.setdefault("skipped_idempotent", result.skipped_idempotent)
        if result.fanout_total > 0:
            evidence_merged.setdefault(
                "fanout_failure_rate",
                round(result.fanout_failed / result.fanout_total, 4),
            )

    rows_loaded = result.rows_loaded if result is not None else None
    is_dry_run = bool(result.is_dry_run) if result is not None else False

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bulk_ingest.feed_ingest_runs (
                    run_id, source_id, feed_name, feed_date, attempt,
                    status, outcome, started_at, completed_at, duration_seconds,
                    rows_loaded, landing_zone, r2_bucket, r2_object_key,
                    payload_format, payload_bytes,
                    error_class, error_message, evidence, is_dry_run
                ) VALUES (
                    %s, %s, %s, %s, 1,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s::jsonb, %s
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
                    is_dry_run = EXCLUDED.is_dry_run,
                    updated_at = NOW()
                """,
                (
                    run_id,
                    source_id,
                    feed_name,
                    feed_date.isoformat(),
                    status,
                    outcome,
                    started_at,
                    completed_at,
                    duration_seconds,
                    rows_loaded,
                    landing_zone,
                    r2_bucket,
                    r2_object_key,
                    payload_format,
                    payload_bytes,
                    error_class,
                    error_message,
                    json.dumps(evidence_merged, default=str),
                    is_dry_run,
                ),
            )
        conn.commit()


# --------------------------------------------------------------------------- #
# Heartbeats — P1-1 per-orchestrator liveness signal.
# --------------------------------------------------------------------------- #


def heartbeat(
    *,
    cron_app: str,
    cron_function: str,
    run_id: str,
    stage: str | None = None,
    progress: dict[str, Any] | None = None,
    container_id: str | None = None,
) -> None:
    """Fire-and-forget heartbeat row write to `ops.cron_heartbeats`.

    Catches DB errors and logs them rather than raising — the orchestrator's
    primary work must NEVER depend on heartbeat write success. The alerter
    cron flags stale heartbeats; a missed heartbeat is a quality-of-service
    blip, not a correctness break.

    The DB connection is made + closed per call; cheap (~10-50ms typical) and
    avoids any long-lived connection state in the orchestrator. For loops
    inside orchestrators, prefer :class:`HeartbeatLoop` which threads the
    cadence + catches the DB-write into a daemon thread.
    """
    import logging

    import psycopg

    log = logging.getLogger(__name__)

    db_url = os.environ.get("DEX_DB_URL_POOLED") or os.environ.get("DATABASE_URL")
    if not db_url:
        log.warning("heartbeat: DEX_DB_URL_POOLED/DATABASE_URL not set; skipping write")
        return

    try:
        with psycopg.connect(db_url, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO ops.cron_heartbeats
                        (cron_app, cron_function, run_id, container_id,
                         stage, progress_json)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        cron_app,
                        cron_function,
                        run_id,
                        container_id,
                        stage,
                        json.dumps(progress or {}, default=str),
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        log.warning("heartbeat write failed: %s: %s", type(exc).__name__, exc)


class HeartbeatLoop:
    """Context manager that fires a heartbeat every ``interval_seconds`` from
    a daemon thread for the lifetime of the ``with`` block.

    Usage::

        with HeartbeatLoop(
            cron_app="data-engine-x-foo",
            cron_function="run_foo",
            run_id=run_id,
            interval_seconds=60,
        ) as hb:
            hb.set_stage("stage_1_search")
            transactions = fetch_search_transactions(...)
            hb.set_stage("stage_2_fanout", {"batches_total": N})
            batch_results = fetch_award_batch.map(batches, ...)
            hb.set_stage("stage_5_lance_write")
            ...
        # daemon thread auto-stops on __exit__

    The thread catches ALL exceptions from heartbeat() so it never crashes
    the orchestrator. The thread is `daemon=True` so it never blocks Python
    process exit.

    Recommended for orchestrators with wall-clock > 5 min. Short-lived
    crons don't need it (their final `record_run` call IS their heartbeat).
    """

    def __init__(
        self,
        *,
        cron_app: str,
        cron_function: str,
        run_id: str,
        interval_seconds: int = 60,
        container_id: str | None = None,
    ) -> None:
        self._cron_app = cron_app
        self._cron_function = cron_function
        self._run_id = run_id
        self._interval = interval_seconds
        self._container_id = container_id
        self._stage: str | None = None
        self._progress: dict[str, Any] = {}
        self._stop_event = None  # initialized in __enter__
        self._thread = None

    def set_stage(self, stage: str, progress: dict[str, Any] | None = None) -> None:
        """Update the current stage label + progress payload reported by
        subsequent heartbeats. Safe to call from any thread."""
        self._stage = stage
        self._progress = dict(progress or {})

    def update_progress(self, **kwargs: Any) -> None:
        """Merge kwargs into the progress payload (without changing stage)."""
        self._progress.update(kwargs)

    def __enter__(self) -> "HeartbeatLoop":
        import threading

        self._stop_event = threading.Event()

        def _loop() -> None:
            # Fire one heartbeat immediately so the alerter sees liveness ASAP.
            heartbeat(
                cron_app=self._cron_app,
                cron_function=self._cron_function,
                run_id=self._run_id,
                stage=self._stage,
                progress=self._progress,
                container_id=self._container_id,
            )
            while not self._stop_event.wait(self._interval):
                heartbeat(
                    cron_app=self._cron_app,
                    cron_function=self._cron_function,
                    run_id=self._run_id,
                    stage=self._stage,
                    progress=self._progress,
                    container_id=self._container_id,
                )

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)


__all__ = [
    "HeartbeatLoop",
    "RunResult",
    "classify_exception",
    "compute_outcome",
    "heartbeat",
    "record_run",
]
