"""FMCSA ingest pipeline (Modal v2 — parallel fan-out).

Orchestration model:

    schedule_heartbeat (cron */15 m, ET)
        └── ingest_run_orchestrator(feed_names=<due_feeds>)
              ├── INSERT pending manifest rows
              ├── ingest_one_feed.starmap(...)   ← bounded parallel
              │     └── per-feed: download → parse → COPY into entities.*
              │           with manifest UPDATE on running/completed/failed
              └── reaper: flip lingering 'running' rows to 'timed_out'

Backward-compat entry points retained for ad-hoc operator use:

    ingest_single_feed(feed_name, feed_date)   # no manifest
    ingest_feed_batch(feed_names, feed_date)   # DEPRECATED — forwards to
                                                # ingest_run_orchestrator

Retry path:

    retry_failed_feeds(prior_run_id)            # only failed/timed_out feeds

The per-feed pipeline (download/parse/persist) is unchanged — see
modal/fmcsa/{stream_parse,postgres_writer,mappings,feed_catalog}.py.
This module owns orchestration only.

Manifest contract: bulk_ingest.feed_ingest_runs scoped to source_id='fmcsa'
(rewired from entities.fmcsa_ingest_runs per the FMCSA backend rewire).
Directive 135's refresh DAG reads from this table; do not change the
schema or run_id semantics without coordinating.
"""

from __future__ import annotations

import json
import os
import re
import sys
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo

import modal

from fmcsa.feed_catalog import FEED_BY_NAME, FEED_CATALOG, FeedConfig
from fmcsa.freshness import (
    ALL_STRATEGIES,
    STRATEGY_DISABLED,
    ProbeResult,
    probe_feed_freshness,
)
from fmcsa.manifest import (
    classify_exception,
    insert_pending_row,
    record_dup_dispatch,
    list_feeds_with_status,
    load_failed_feed_names,
    mark_completed,
    mark_failed,
    mark_running,
    status_rollup,
)
from fmcsa.postgres_writer import connect_db, persist_all_chunks
from fmcsa.stream_parse import get_last_header_evidence, stream_feed_rows

TZ_ET = ZoneInfo("America/New_York")
SCHEDULE_TABLE = "bulk_ingest.feed_schedule_config"
SOURCE_ID = "fmcsa"
HEARTBEAT_MINUTES = 15
HEARTBEAT_CRON_EXPRESSION = "*/15 * * * *"

# Per-feed timeout for the "normal" worker. The 12-hour sequential batch
# cap (legacy March-2026 model) was the root cause of partial ingests — one
# slow feed timed out the whole batch. A 90-minute per-feed cap on
# ingest_one_feed keeps the blast radius to one feed; the docstring
# convention is "if a feed legitimately needs more, escalate via FeedConfig
# worker_size, don't widen the global cap."
PER_FEED_TIMEOUT_SECONDS = 60 * 90

# Per-feed timeout for the "xl" worker (ingest_one_feed_xl). FMCSA's largest
# bulk files (Vehicle Inspection File, Vehicle Inspections and Violations,
# AuthHist + InsHist - All With History, Crash File, Company Census File)
# routinely exceed 90 min end-to-end (download → stream-parse → COPY into
# entities.*). Tagged via FeedConfig.worker_size="xl" in feed_catalog.
XL_PER_FEED_TIMEOUT_SECONDS = 60 * 60 * 6

# Orchestrator timeout. Sized to comfortably fit the slowest XL feed
# (6h budget) plus the normal-bucket starmap that runs after, with ~1h
# headroom. Bumped from 4h when XL routing landed.
ORCHESTRATOR_TIMEOUT_SECONDS = 60 * 60 * 8

DEFAULT_ORCHESTRATOR_CONCURRENCY = 6
DEFAULT_RETRY_CONCURRENCY = 4

app = modal.App("data-engine-x-fmcsa-ingest")
image = (
    modal.Image.debian_slim(python_version="3.11")
    # Repo migrated from requirements.txt to pyproject.toml during the monorepo
    # bootstrap. Modal 1.4+ reads [project.dependencies] directly. Run
    # `modal deploy` from apps/data-engine-x/ so the relative path resolves.
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("modal/fmcsa", remote_path="/root/fmcsa")
    .add_local_dir("modal/landing", remote_path="/root/landing")
)
# Named Modal secret. Create once via:
#   doppler run -- bash -c 'modal secret create dex-db DATABASE_URL="$DEX_DB_URL_POOLED"'
# Update via `modal secret delete dex-db` + recreate. The previous
# `Secret.from_dict({"DATABASE_URL": os.environ[...]})` pattern was fragile:
# Modal's deploy machinery did not reliably propagate the local DATABASE_URL
# into the deployed Function, leading to silent failures where every
# heartbeat tick raised RuntimeError("DATABASE_URL is not set").
FUNCTION_SECRETS = [
    modal.Secret.from_name("hqx-db"),
    modal.Secret.from_name("bulk-ingest-r2"),
]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _default_feed_date_et() -> str:
    return datetime.now(TZ_ET).date().isoformat()


def _utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _source_run_metadata(
    feed: FeedConfig, feed_date: str, trigger: str, started_at: str
) -> dict[str, Any]:
    return {
        "platform": "modal",
        "trigger": trigger,
        "feed_name": feed.feed_name,
        "feed_date": feed_date,
        "scheduled_time_et": feed.schedule_time_et.strftime("%H:%M"),
        "source_kind": feed.source_kind,
        "size_class": feed.size_class,
        "started_at": started_at,
    }


# In-flight lookback for the heartbeat's "is this feed already running?"
# check. Must exceed the longest possible per-feed timeout (XL = 6h) plus
# a small buffer; otherwise an XL worker still legitimately running at
# T+2h gets re-dispatched as a duplicate. 7h covers the 6h XL cap with
# 1h headroom for queue wait + cold start + container teardown.
IN_FLIGHT_LOOKBACK_HOURS = 7

PROBE_RESULT_FRESH = "fresh"
PROBE_RESULT_STALE = "stale"
PROBE_RESULT_ERROR = "error"
PROBE_RESULT_IN_FLIGHT = "in_flight"
PROBE_RESULT_GATED_TOO_EARLY = "gated_too_early"
PROBE_RESULT_ALREADY_INGESTED_TODAY = "already_ingested_today"


def _execute_ingest(
    feed_name: str,
    feed_date: str | None,
    trigger: str,
    *,
    manifest_run_id: str | None = None,
    manifest_attempt: int = 1,
) -> dict[str, Any]:
    """Run the per-feed download → parse → persist pipeline.

    If manifest_run_id is provided, ALSO writes status updates to
    bulk_ingest.feed_ingest_runs scoped to source_id='fmcsa'
    (mark_running before, mark_completed after). Without manifest_run_id,
    behaves like the legacy single-feed path (no manifest writes) —
    keeps ingest_single_feed ad-hoc-friendly.
    """
    if feed_name not in FEED_BY_NAME:
        raise ValueError(f"Unknown feed: {feed_name}")

    feed = FEED_BY_NAME[feed_name]
    selected_feed_date = feed_date or _default_feed_date_et()
    started_at = datetime.utcnow().isoformat() + "Z"
    source_task_id = f"modal-{_slug(feed.feed_name)}-{uuid.uuid4().hex[:10]}"
    schedule_id = f"modal:{_slug(feed.feed_name)}" if trigger == "schedule" else None

    if manifest_run_id:
        mark_running(
            run_id=manifest_run_id,
            feed_name=feed_name,
            attempt=manifest_attempt,
            source_task_id=source_task_id,
        )

    started = perf_counter()
    rows_downloaded = 0
    chunks_processed = 0
    landing_zone = _get_feed_landing_zone(feed_name)

    def _chunk_generator():
        nonlocal rows_downloaded, chunks_processed
        for chunk in stream_feed_rows(feed=feed, chunk_size=feed.chunk_size):
            rows_downloaded += len(chunk)
            chunks_processed += 1
            yield chunk

    landing_metadata: dict[str, Any]
    rows_written: int

    if landing_zone == "r2":
        # R2 landing path — payload bypasses Postgres entirely. Streaming
        # parquet+zstd to dex-raw-landing-zone with object key
        # fmcsa/{feed_name}/{feed_date}/{run_id}.parquet.zst. Metadata
        # still lands in bulk_ingest.feed_ingest_runs via mark_completed.
        import pyarrow as pa
        from datetime import date as _date
        from uuid import UUID as _UUID, uuid4 as _uuid4

        from landing import R2Landing

        def _record_batch_generator():
            nonlocal rows_downloaded, chunks_processed
            # stream_feed_rows yields ParsedSourceRow dataclass instances;
            # raw_fields is the FMCSA-native column-name → string-value dict.
            # We land the raw form (matching dex-raw-landing-zone semantics);
            # downstream consumers can apply typing via mappings.build_typed_row.
            for chunk in stream_feed_rows(feed=feed, chunk_size=feed.chunk_size):
                rows_downloaded += len(chunk)
                chunks_processed += 1
                if chunk:
                    yield pa.RecordBatch.from_pylist([row.raw_fields for row in chunk])

        r2 = R2Landing()
        try:
            run_id_uuid = _UUID(manifest_run_id) if manifest_run_id else _uuid4()
        except (TypeError, ValueError):
            run_id_uuid = _uuid4()
        landing_result = r2.write_streaming(
            source_id=SOURCE_ID,
            feed_name=feed_name,
            feed_date=_date.fromisoformat(selected_feed_date),
            run_id=run_id_uuid,
            batches=_record_batch_generator(),
        )
        rows_written = landing_result.rows_loaded
        landing_metadata = landing_result.to_ledger_columns()
    else:
        persist_result = persist_all_chunks(
            feed=feed,
            chunks=_chunk_generator(),
            feed_date=selected_feed_date,
            source_task_id=source_task_id,
            source_observed_at=started_at,
            source_schedule_id=schedule_id,
            source_run_metadata=_source_run_metadata(
                feed=feed,
                feed_date=selected_feed_date,
                trigger=trigger,
                started_at=started_at,
            ),
        )
        rows_written = persist_result["rows_written"]
        landing_metadata = {"landing_zone": "postgres"}

    elapsed_seconds = round(perf_counter() - started, 2)
    result = {
        "feed_name": feed.feed_name,
        "feed_date": selected_feed_date,
        "rows_downloaded": rows_downloaded,
        "rows_written": rows_written,
        "chunks_processed": chunks_processed,
        "elapsed_seconds": elapsed_seconds,
        "trigger": trigger,
        "source_task_id": source_task_id,
        "landing_zone": landing_zone,
    }
    if landing_zone == "r2":
        result["r2_object_key"] = landing_metadata.get("r2_object_key")
        result["payload_bytes"] = landing_metadata.get("payload_bytes")

    if manifest_run_id:
        mark_completed(
            run_id=manifest_run_id,
            feed_name=feed_name,
            attempt=manifest_attempt,
            rows_loaded=rows_written,
            bytes_downloaded=None,
            source_task_id=source_task_id,
            evidence={
                "rows_downloaded": rows_downloaded,
                "rows_written": rows_written,
                "chunks_processed": chunks_processed,
                "elapsed_seconds": elapsed_seconds,
                "trigger": trigger,
                "feed_url": feed.url if hasattr(feed, "url") else None,
                "landing_zone": landing_zone,
                # F-09 from 2026-05-08 audit: capture the observed CSV
                # header so column-shift drift is detectable across runs
                # via SQL even for feeds without an explicit header_row
                # config (operator can compare evidence->>'observed_header_sha256').
                **(get_last_header_evidence(feed_name) or {}),
            },
            **landing_metadata,
        )
    # Stamp per-day idempotency on the schedule_config row. The next
    # heartbeat will see last_successful_feed_date >= today (ET) and
    # skip this feed. Failures fall through this stamp on purpose —
    # only successful ingests update the per-day cursor.
    try:
        _mark_successful_ingest_date(
            feed_name=feed_name,
            ingested_feed_date=selected_feed_date,
        )
    except Exception as exc:
        # Non-fatal — the manifest is still authoritative. Worst case
        # the next tick re-probes the feed, sees the upstream signal is
        # the same, and skips on probe-not-fresh.
        print(f"[ingest] last_successful_feed_date stamp failed: {exc}")
    return result


# ----------------------------------------------------------------------
# Per-feed worker
# ----------------------------------------------------------------------


def _run_one_feed_impl(
    run_id: str,
    feed_name: str,
    feed_date: str | None,
    attempt: int,
) -> dict[str, Any]:
    """Shared body for ingest_one_feed (90m) and ingest_one_feed_xl (6h).

    Catches all exceptions and reports via the manifest — the worker
    NEVER raises on per-feed failure. Modal-level timeout is the one
    path the worker can't catch: if the container is SIGKILLed, the
    manifest row stays 'running' and the orchestrator's reaper flips it
    to 'timed_out'. See _modal_starmap_dispatcher for the routing logic
    that decides which Modal function (and therefore which timeout)
    handles each feed.
    """
    # Phase 0c atomic ingest: wrap the per-feed work in atomic_ingest_run.
    # Ledger-only mode (no R2 staging — the FMCSA per-feed pipeline owns
    # its own R2 write via R2Landing). atomic_ingest provides:
    #   - advisory-lock serialization on (source_id) so concurrent fmcsa
    #     invocations of the same feed wait rather than racing
    #   - idempotency check on the per-feed run_id+attempt key (replays
    #     after a stuck retry exit 'skipped' instead of double-ingesting)
    #   - transactional ledger commit (row INSERT + UPDATE in one txn)
    #   - automatic ledger 'failed' write on uncaught exceptions
    # The legacy best-effort observability_ledger calls (Phase 0a) are
    # superseded by this single helper invocation.
    display_name = f"fmcsa_{feed_name.lower().replace('-', '_').replace(' ', '_')}"
    # FMCSA's per-feed run_id is a UUID; combine with feed_name + attempt via
    # uuid5 so the idempotency key is itself a UUID per atomic_ingest contract.
    import uuid as _uuid
    idem_key = str(_uuid.uuid5(_uuid.NAMESPACE_OID, f"{run_id}/{feed_name}/{attempt}"))
    captured: dict[str, Any] = {}

    def _execute_finalize_capture() -> dict[str, Any]:
        result = _execute_ingest(
            feed_name=feed_name,
            feed_date=feed_date,
            trigger="orchestrator",
            manifest_run_id=run_id,
            manifest_attempt=attempt,
        )
        captured["result"] = result
        return result

    # Phase-0c atomic_ingest is OPTIONAL. It lives in app.services, which is NOT
    # bundled into this ingest image (only modal/fmcsa + modal/landing are added),
    # so the import has never been satisfiable here — atomic_ingest never actually
    # ran in prod. When it is unavailable we ingest directly via _execute_ingest
    # (the proven pre-phase-0c path), which still performs the full FMCSA manifest
    # lifecycle (mark_running -> mark_completed) and is gated by dispatch-level
    # once-per-ET-day idempotency. Do NOT let an unimportable optional layer block
    # ingestion (the 2026-05-29 "No module named 'app'" outage).
    try:
        from app.services import atomic_ingest  # type: ignore[import]
    except ImportError:
        atomic_ingest = None  # type: ignore[assignment]

    atomic_result: dict[str, Any] | None = None
    try:
        if atomic_ingest is not None:
            atomic_result = atomic_ingest.atomic_ingest_run(
                source_display_name=display_name,
                idempotency_run_id=idem_key,
                format="r2_parquet",
                finalize_callable=_execute_finalize_capture,
                finalize_kwargs={},
                dest_bucket=None,
                dest_key=None,
                rows_ingested_callable=lambda r: (r or {}).get("rows_written"),
                ledger_metadata={
                    "writer": "fmcsa_ingest_app",
                    "feed_name": feed_name,
                    "manifest_run_id": run_id,
                    "attempt": attempt,
                },
                storage_uri=f"s3://dex-raw-landing-zone/fmcsa-derived/{feed_name}/",
            )
        else:
            # Direct ingest: _execute_ingest handles mark_running/mark_completed.
            _execute_finalize_capture()
    except Exception as exc:
        # Mirror to the FMCSA manifest so the existing dispatch view shows it.
        # (atomic path: atomic_ingest also wrote its own ledger 'failed'.)
        error_class = classify_exception(exc)
        error_message = str(exc) or repr(exc)
        try:
            mark_failed(
                run_id=run_id,
                feed_name=feed_name,
                attempt=attempt,
                error_message=error_message,
                error_class=error_class,
                evidence={
                    "exception_type": type(exc).__name__,
                    "exception_module": (type(exc).__module__ or ""),
                    "atomic_ingest": atomic_ingest is not None,
                },
            )
        except Exception as inner_exc:
            print(f"[_run_one_feed_impl] manifest write failed: {inner_exc}")
        return {
            "feed_name": feed_name,
            "status": "failed",
            "error_class": error_class,
            "error_message": error_message[:500],
        }

    if atomic_result is not None and atomic_result["status"] == "skipped":
        print(f"[_run_one_feed_impl] feed={feed_name} idempotent skip "
              f"(existing_run_id={atomic_result.get('existing_run_id')})")
        return {"status": "skipped", "feed_name": feed_name,
                "atomic_ingest_run_id": atomic_result.get("run_id")}
    result = captured.get("result", {})
    return {"status": "completed", **result}


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=PER_FEED_TIMEOUT_SECONDS,
    memory=4096,
    cpu=2,
)
def ingest_one_feed(
    run_id: str,
    feed_name: str,
    feed_date: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Normal-bucket worker (90-min Modal timeout). Default for any feed
    whose FeedConfig.worker_size != "xl"."""
    return _run_one_feed_impl(run_id, feed_name, feed_date, attempt)


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=XL_PER_FEED_TIMEOUT_SECONDS,
    memory=4096,
    cpu=2,
)
def ingest_one_feed_xl(
    run_id: str,
    feed_name: str,
    feed_date: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """XL-bucket worker (6-hour Modal timeout). Routed to by the
    dispatcher when FeedConfig.worker_size == "xl". Same body as
    ingest_one_feed; only the @app.function timeout differs. Modal binds
    timeouts statically per @app.function in v1.4 (Function.with_options
    is not implemented), so we maintain two sibling functions instead."""
    return _run_one_feed_impl(run_id, feed_name, feed_date, attempt)


# ----------------------------------------------------------------------
# Orchestrator (pure-logic core + Modal entry point)
# ----------------------------------------------------------------------


WorkerDispatcher = Callable[
    [list[tuple[str, str, str | None, int]]],
    Iterable[Any],
]
"""Callable that takes a list of (run_id, feed_name, feed_date, attempt)
tuples and yields per-feed result dicts (or BaseException when a worker
raised). Production uses ingest_one_feed.starmap(...); tests inject a
fake."""


def _orchestrate(
    *,
    feed_names: list[str] | None,
    feed_date: str | None,
    max_concurrency: int,
    dispatcher: WorkerDispatcher,
    new_run_id: str | None = None,
    base_attempts: dict[str, int] | None = None,
    cron_function: str = "_orchestrate",
) -> dict[str, Any]:
    """Pure-logic orchestrator. Manifest writes + dispatch + reaper.

    Args:
        feed_names: subset to run, or None for FEED_CATALOG.
        feed_date: optional ISO date; worker resolves to today (ET) if
            None.
        max_concurrency: bound passed to dispatcher.
        dispatcher: see WorkerDispatcher. Typically the Modal starmap
            wrapper; tests inject a fake to drive results without
            spinning up Modal.
        new_run_id: optional explicit run_id (tests + retry path); a
            fresh uuid is generated otherwise.
        base_attempts: optional {feed_name: attempt} overrides (retry
            path; fresh runs default to 1).
    """
    selected_feed_names = (
        feed_names if feed_names else [feed.feed_name for feed in FEED_CATALOG]
    )
    base_attempts = base_attempts or {}
    run_id = new_run_id or str(uuid.uuid4())

    # Resolve feed_date BEFORE manifest writes. The DB-side singleton-cancel
    # partial unique index `ux_bulk_ingest_runs_singleton_inflight` is keyed
    # on (source_id, feed_name, feed_date) WHERE status IN ('pending',
    # 'running'). Postgres treats NULL ≠ NULL, so passing feed_date=None
    # silently defeats the index and admits duplicate dispatches. Adversarial
    # audit 2026-05-08 confirmed 4 FMCSA feeds were ingested twice in a
    # single hour today (Vehicle Inspections / Crash File / AuthHist - All
    # With History / InsHist - All With History) with identical row counts
    # and distinct R2 keys — doubled compute + storage + downstream MV
    # contamination. Resolve here so every manifest write carries a date.
    if feed_date is None:
        feed_date = _default_feed_date_et()

    print(
        f"[orchestrator] run_id={run_id} feeds_requested={len(selected_feed_names)} "
        f"max_concurrency={max_concurrency}"
    )

    # Resolve known feeds and probe the manifest for any that are still
    # in-flight under a DIFFERENT run_id within the IN_FLIGHT_LOOKBACK_HOURS
    # window. Skip those — re-dispatching a still-running feed would race
    # with the live worker and produce dup-key failures at COPY time.
    # Modal's at-least-once .spawn() semantics can re-invoke the
    # orchestrator (preempted spot, network blip on the client RPC, etc.)
    # so this idempotency check is the orchestrator-side safety net even
    # though the heartbeat already does its own pre-dispatch in-flight gate.
    candidate_feed_names: list[str] = []
    for name in selected_feed_names:
        if name not in FEED_BY_NAME:
            print(f"[orchestrator] skipping unknown feed: {name}")
            continue
        candidate_feed_names.append(name)

    in_flight = _check_in_flight_feeds(feed_names=candidate_feed_names)
    skipped_in_flight: list[str] = []
    valid_feed_names: list[str] = []
    for name in candidate_feed_names:
        if name in in_flight:
            # _check_in_flight_feeds returns set[str], not a {name → winning
            # run_id} dict. winning_run_id is therefore always None at the
            # orchestrator layer; surfacing the actual winner requires
            # widening _check_in_flight_feeds' return shape (rank-2
            # follow-up, harmless to leave None for now).
            winning_run_id: str | None = None
            print(
                f"[orchestrator] skipping {name} — already in-flight under "
                f"another run_id within {IN_FLIGHT_LOOKBACK_HOURS}h lookback"
            )
            skipped_in_flight.append(name)
            try:
                record_dup_dispatch(
                    run_id=run_id,
                    feed_name=name,
                    feed_date=feed_date,
                    attempt=base_attempts.get(name, 1),
                    winning_run_id=winning_run_id,
                    evidence={
                        "lookback_hours": IN_FLIGHT_LOOKBACK_HOURS,
                        "max_concurrency": max_concurrency,
                    },
                )
            except Exception as exc:
                print(f"[orchestrator] record_dup_dispatch failed for {name}: {exc}")
            continue
        valid_feed_names.append(name)
        attempt = base_attempts.get(name, 1)
        insert_pending_row(
            run_id=run_id,
            feed_name=name,
            feed_date=feed_date,
            attempt=attempt,
            worker_concurrency_at_dispatch=max_concurrency,
        )

    if not valid_feed_names:
        return {
            "run_id": run_id,
            "total_feeds": 0,
            "skipped_in_flight": skipped_in_flight,
            "status_rollup": status_rollup(run_id=run_id),
            "failures": [],
        }

    map_args = [
        (run_id, name, feed_date, base_attempts.get(name, 1))
        for name in valid_feed_names
    ]

    sys.path.insert(0, "/root")
    from landing.ledger import HeartbeatLoop  # noqa: E402

    dispatched_count = 0
    with HeartbeatLoop(
        cron_app=app.name,
        cron_function=cron_function,
        run_id=run_id,
    ) as hb:
        hb.set_stage(
            "dispatching",
            {
                "feeds_total": len(valid_feed_names),
                "max_concurrency": max_concurrency,
                "feed_date": feed_date,
            },
        )
        try:
            for _ in dispatcher(map_args):
                dispatched_count += 1
                hb.update_progress(dispatched_count=dispatched_count)
        except Exception as exc:
            print(f"[orchestrator] dispatcher errored: {exc}")

    # Reaping is externalized — see modal/reap_orphans_app.py (15-min cron).
    # The orchestrator no longer reaps inline because the orchestrator
    # container itself is one of the failure modes the reaper has to cover.

    rollup = status_rollup(run_id=run_id)
    failures = list_feeds_with_status(
        run_id=run_id, statuses=("failed", "timed_out")
    )
    return {
        "run_id": run_id,
        "total_feeds": len(valid_feed_names),
        "skipped_in_flight": skipped_in_flight,
        "dispatched_count": dispatched_count,
        "status_rollup": rollup,
        "failures": [
            {
                "feed_name": row["feed_name"],
                "attempt": row["attempt"],
                "status": row["status"],
                "error_class": row.get("error_class"),
                "error_message": (row.get("error_message") or "")[:200],
            }
            for row in failures
        ],
    }


def _modal_starmap_dispatcher(
    max_concurrency: int,
) -> WorkerDispatcher:
    """Return a dispatcher that buckets by FeedConfig.worker_size and
    fans out via two sibling Modal functions: ingest_one_feed_xl
    (6-hour timeout) for "xl" feeds, ingest_one_feed (90-min) for the
    rest. XL bucket runs FIRST so that if it dominates wall-clock the
    orchestrator's 8-hour timeout still leaves room for the normal
    bucket — the inverse ordering would let one slow normal feed slip
    the budget.

    Both buckets use .starmap(..., return_exceptions=True). starmap
    blocks until each bucket fully drains, so the reaper at the end of
    _orchestrate runs only after every worker has reported (or been
    Modal-killed). max_concurrency is advisory only — Modal v1.4
    .starmap() takes no max_concurrency kwarg; the per-bucket cap is
    set at decoration time on each @app.function.
    """
    del max_concurrency  # advisory; see docstring

    def _dispatch(
        map_args: list[tuple[str, str, str | None, int]],
    ) -> Iterable[Any]:
        xl_args: list[tuple[str, str, str | None, int]] = []
        normal_args: list[tuple[str, str, str | None, int]] = []
        for args in map_args:
            feed_name = args[1]
            feed = FEED_BY_NAME.get(feed_name)
            if feed is not None and feed.worker_size == "xl":
                xl_args.append(args)
            else:
                normal_args.append(args)

        # XL first — see docstring rationale.
        if xl_args:
            try:
                yield from ingest_one_feed_xl.starmap(
                    xl_args, return_exceptions=True
                )
            except Exception as exc:
                print(
                    f"[orchestrator] xl-bucket starmap raised: {exc} "
                    f"(reaper will reconcile manifest)"
                )
        if normal_args:
            try:
                yield from ingest_one_feed.starmap(
                    normal_args, return_exceptions=True
                )
            except Exception as exc:
                print(
                    f"[orchestrator] normal-bucket starmap raised: {exc} "
                    f"(reaper will reconcile manifest)"
                )

    return _dispatch


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def ingest_run_orchestrator(
    feed_names: list[str] | None = None,
    feed_date: str | None = None,
    max_concurrency: int = DEFAULT_ORCHESTRATOR_CONCURRENCY,
) -> dict[str, Any]:
    """Top-level: fan out feed work via parallel ingest_one_feed workers.

    Returns a summary dict: run_id, status_rollup, failure list. The
    run_id is the key directive 135's refresh DAG uses to track ingest
    completion.
    """
    return _orchestrate(
        feed_names=feed_names,
        feed_date=feed_date,
        max_concurrency=max_concurrency,
        dispatcher=_modal_starmap_dispatcher(max_concurrency),
        cron_function="ingest_run_orchestrator",
    )


# ----------------------------------------------------------------------
# Retry-failed-feeds entry point
# ----------------------------------------------------------------------


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def retry_failed_feeds(
    prior_run_id: str,
    max_concurrency: int = DEFAULT_RETRY_CONCURRENCY,
) -> dict[str, Any]:
    """Re-run only the failed/timed_out feeds from a prior run.

    Generates a NEW run_id and dispatches a fresh orchestrator pass over
    the failed-feed subset. attempt = prior_attempt + 1 per feed.

    Default concurrency is intentionally lower than a fresh run:
    failures often cluster around capacity issues, so ramp gently.
    """
    failed = load_failed_feed_names(prior_run_id=prior_run_id)
    if not failed:
        return {
            "run_id": None,
            "prior_run_id": prior_run_id,
            "feeds_retried": 0,
            "message": "no failed/timed_out feeds in prior run",
        }

    feed_names = [name for name, _ in failed if name in FEED_BY_NAME]
    base_attempts = {
        name: prior_attempt + 1
        for name, prior_attempt in failed
        if name in FEED_BY_NAME
    }

    summary = _orchestrate(
        feed_names=feed_names,
        feed_date=None,
        max_concurrency=max_concurrency,
        dispatcher=_modal_starmap_dispatcher(max_concurrency),
        base_attempts=base_attempts,
        cron_function="retry_failed_feeds",
    )
    summary["prior_run_id"] = prior_run_id
    summary["feeds_retried"] = len(feed_names)
    return summary


# ----------------------------------------------------------------------
# Backward-compat entry points
# ----------------------------------------------------------------------


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=60 * 60 * 6,
    memory=8192,
    cpu=4,
)
def ingest_single_feed(
    feed_name: str,
    feed_date: str | None = None,
) -> dict[str, Any]:
    """Ad-hoc single-feed run (no manifest tracking).

    Backward-compat for operator scripts; not used by the orchestrator.
    """
    return _execute_ingest(feed_name=feed_name, feed_date=feed_date, trigger="manual")


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def ingest_feed_batch(
    feed_names: list[str],
    feed_date: str | None = None,
) -> dict[str, Any]:
    """DEPRECATED — use ingest_run_orchestrator.

    Forwards through the new orchestrator at default concurrency. Will
    be removed in a future directive.
    """
    print(
        "[ingest_feed_batch] DEPRECATED: forwarding to ingest_run_orchestrator. "
        "Migrate callers to ingest_run_orchestrator(feed_names=..., max_concurrency=...)."
    )
    return _orchestrate(
        feed_names=feed_names,
        feed_date=feed_date,
        max_concurrency=DEFAULT_ORCHESTRATOR_CONCURRENCY,
        dispatcher=_modal_starmap_dispatcher(DEFAULT_ORCHESTRATOR_CONCURRENCY),
    )


# ----------------------------------------------------------------------
# Schedule heartbeat
# ----------------------------------------------------------------------


def _fetch_schedule_rows() -> list[dict[str, Any]]:
    """Read FMCSA's feed schedule from bulk_ingest.feed_schedule_config.

    FMCSA-specific columns (earliest_check_time_et, freshness_check_strategy,
    last_successful_feed_date) are stored under source_metadata JSONB; we
    project them back to top-level dict keys so the caller's signature
    doesn't change. Generic columns (last_probe_signal, landing_zone) read
    direct.
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT
                    feed_name,
                    enabled,
                    feed_group,
                    size_class,
                    landing_zone,
                    (source_metadata->>'earliest_check_time_et')::time
                        AS earliest_check_time_et,
                    source_metadata->>'freshness_check_strategy'
                        AS freshness_check_strategy,
                    NULLIF(source_metadata->>'last_successful_feed_date', '')::date
                        AS last_successful_feed_date,
                    last_probe_signal
                FROM {SCHEDULE_TABLE}
                WHERE source_id = %s
                ORDER BY feed_name
                """,
                (SOURCE_ID,),
            )
            rows = cursor.fetchall()
    return rows or []


def _get_feed_landing_zone(feed_name: str) -> str:
    """Return the landing_zone for a single FMCSA feed.

    Used by _execute_ingest to branch payload routing (R2 vs Postgres).
    Defaults to 'postgres' if no row exists (legacy fallback).
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT landing_zone
                FROM {SCHEDULE_TABLE}
                WHERE source_id = %s AND feed_name = %s
                """,
                (SOURCE_ID, feed_name),
            )
            row = cursor.fetchone()
    if row is None:
        return "postgres"
    return row.get("landing_zone") or "postgres"


def _check_in_flight_feeds(*, feed_names: list[str]) -> set[str]:
    """Return the subset of feed_names with a pending/running manifest row
    in the lookback window. Used to short-circuit re-dispatch when an
    earlier heartbeat tick's worker is still in flight.
    """
    if not feed_names:
        return set()
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT feed_name
                FROM bulk_ingest.feed_ingest_runs
                WHERE source_id = 'fmcsa'
                  AND feed_name = ANY(%s)
                  AND status IN ('pending', 'running')
                  AND COALESCE(started_at, created_at)
                      >= NOW() - make_interval(hours => %s)
                """,
                (feed_names, IN_FLIGHT_LOOKBACK_HOURS),
            )
            rows = cursor.fetchall() or []
    return {row["feed_name"] for row in rows}


def _feeds_with_manifest_evidence(*, feed_names: list[str]) -> set[str]:
    """Return the subset of feed_names with at least one terminal-success
    manifest row.

    "Terminal success" = outcome IN ('succeeded_with_changes',
    'succeeded_with_zero_new_rows'). Both imply the per-feed pipeline ran
    end-to-end (R2 write + mark_completed). 'probe_said_no_change' is
    intentionally excluded — it can be set by the dup-dispatch backfill
    on a feed that never actually ingested (audit PR β, 2026-05-08).

    Used by _evaluate_feed_for_dispatch to harden the never-ingested
    carve-out: a STALE probe should only short-circuit dispatch when we
    actually have data behind the cursor. Defends against migration-state-
    handover artifacts where a cursor survives a cutover that didn't
    bring forward the underlying data — the failure mode that deadlocked
    the 7 SMS feeds since 2026-05-07 (Postgres-era last_successful_feed_date
    survived the R2 cutover even though no R2 partitions existed).
    """
    if not feed_names:
        return set()
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT feed_name
                FROM bulk_ingest.feed_ingest_runs
                WHERE source_id = 'fmcsa'
                  AND feed_name = ANY(%s)
                  AND outcome IN (
                      'succeeded_with_changes',
                      'succeeded_with_zero_new_rows'
                  )
                """,
                (feed_names,),
            )
            rows = cursor.fetchall() or []
    return {row["feed_name"] for row in rows}


def _record_probe_outcome(
    *,
    feed_name: str,
    probe_at_iso: str,
    probe_result: str,
    probe: ProbeResult | None,
) -> None:
    observed_modified_at_iso: str | None = None
    if probe is not None and probe.observed_modified_at is not None:
        observed_modified_at_iso = probe.observed_modified_at.isoformat()
    observed_signal = probe.observed_signal if probe is not None else None
    probe_error = probe.probe_error if probe is not None else None
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {SCHEDULE_TABLE}
                SET
                    last_probe_at = %s::timestamptz,
                    last_probe_signal = COALESCE(%s::text, last_probe_signal),
                    source_metadata = source_metadata || jsonb_build_object(
                        'last_probe_result', %s::text,
                        'last_probe_observed_modified_at',
                            COALESCE(%s::text, source_metadata->>'last_probe_observed_modified_at'),
                        'last_probe_error', %s::text
                    ),
                    updated_at = NOW()
                WHERE source_id = %s AND feed_name = %s
                """,
                (
                    probe_at_iso,
                    observed_signal,
                    probe_result,
                    observed_modified_at_iso,
                    probe_error,
                    SOURCE_ID,
                    feed_name,
                ),
            )
        connection.commit()


def _mark_successful_ingest_date(
    *, feed_name: str, ingested_feed_date: str | None
) -> None:
    """Stamp last_successful_feed_date on the schedule_config row.

    Called from the heartbeat after a worker reports completion. Used by
    the next tick to enforce per-day idempotency.
    """
    if not ingested_feed_date:
        return
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {SCHEDULE_TABLE}
                SET
                    last_successful_at = GREATEST(
                        COALESCE(last_successful_at, %s::timestamptz),
                        %s::timestamptz
                    ),
                    source_metadata = source_metadata || jsonb_build_object(
                        'last_successful_feed_date',
                        GREATEST(
                            COALESCE(
                                NULLIF(source_metadata->>'last_successful_feed_date', '')::date,
                                %s::date
                            ),
                            %s::date
                        )::text
                    ),
                    updated_at = NOW()
                WHERE source_id = %s AND feed_name = %s
                """,
                (
                    ingested_feed_date,
                    ingested_feed_date,
                    ingested_feed_date,
                    ingested_feed_date,
                    SOURCE_ID,
                    feed_name,
                ),
            )
        connection.commit()


def _record_heartbeat_evaluated(evaluated_at_iso: str) -> None:
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {SCHEDULE_TABLE}
                SET
                    last_heartbeat_evaluated_at = %s,
                    updated_at = NOW()
                WHERE source_id = %s
                """,
                (evaluated_at_iso, SOURCE_ID),
            )
        connection.commit()


def _record_feed_state(
    *,
    feed_name: str,
    run_at_iso: str | None,
    run_status: str,
    run_summary: dict[str, Any],
) -> None:
    # Map FMCSA's 3-value run_status ('success', 'failed', 'running') to
    # bulk_ingest's 5-value last_run_outcome enum so the dispatch view can
    # read either column. The full FMCSA-specific status string is preserved
    # in source_metadata for callers that depended on the legacy taxonomy.
    last_run_outcome: str | None
    if run_status == "success":
        last_run_outcome = "succeeded_with_changes"
    elif run_status == "failed":
        last_run_outcome = "failed"
    else:
        last_run_outcome = None
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {SCHEDULE_TABLE}
                SET
                    last_run_at = COALESCE(%s::timestamptz, last_run_at),
                    last_run_outcome = COALESCE(%s::text, last_run_outcome),
                    source_metadata = source_metadata || jsonb_build_object(
                        'last_run_status', %s::text,
                        'last_run_summary', %s::jsonb
                    ),
                    updated_at = NOW()
                WHERE source_id = %s AND feed_name = %s
                """,
                (
                    run_at_iso,
                    last_run_outcome,
                    run_status,
                    json.dumps(run_summary),
                    SOURCE_ID,
                    feed_name,
                ),
            )
        connection.commit()


@dataclass(frozen=True)
class ProbeDecision:
    feed_name: str
    decision: str  # one of PROBE_RESULT_*
    probe: ProbeResult | None
    feed_date_for_dispatch: str | None


def _evaluate_feed_for_dispatch(
    *,
    row: dict[str, Any],
    now_et: datetime,
    today_et: date,
    in_flight_feeds: set[str],
    feeds_with_manifest_evidence: set[str],
    probe_fn: Callable[..., ProbeResult] = probe_feed_freshness,
) -> ProbeDecision | None:
    """Return a ProbeDecision for an enabled, known feed, or None if the row
    should be silently skipped (disabled / unknown feed)."""
    feed_name = row.get("feed_name")
    if not row.get("enabled") or not isinstance(feed_name, str):
        return None
    if feed_name not in FEED_BY_NAME:
        print(f"[schedule-heartbeat] skipping unknown feed in config: {feed_name}")
        return None

    feed = FEED_BY_NAME[feed_name]
    strategy = row.get("freshness_check_strategy") or "head_redirect_filename"
    if strategy not in ALL_STRATEGIES or strategy == STRATEGY_DISABLED:
        return ProbeDecision(
            feed_name=feed_name,
            decision=PROBE_RESULT_ERROR if strategy not in ALL_STRATEGIES else PROBE_RESULT_STALE,
            probe=ProbeResult(
                is_fresh=False,
                probe_strategy=strategy or "unknown",
                probe_error=(
                    f"unknown strategy: {strategy}" if strategy not in ALL_STRATEGIES else None
                ),
            ),
            feed_date_for_dispatch=None,
        )

    earliest = row.get("earliest_check_time_et")
    if isinstance(earliest, time) and now_et.timetz().replace(tzinfo=None) < earliest:
        return ProbeDecision(
            feed_name=feed_name,
            decision=PROBE_RESULT_GATED_TOO_EARLY,
            probe=None,
            feed_date_for_dispatch=None,
        )

    last_successful = row.get("last_successful_feed_date")
    if isinstance(last_successful, date) and last_successful >= today_et:
        return ProbeDecision(
            feed_name=feed_name,
            decision=PROBE_RESULT_ALREADY_INGESTED_TODAY,
            probe=None,
            feed_date_for_dispatch=None,
        )

    if feed_name in in_flight_feeds:
        return ProbeDecision(
            feed_name=feed_name,
            decision=PROBE_RESULT_IN_FLIGHT,
            probe=None,
            feed_date_for_dispatch=None,
        )

    last_signal = row.get("last_probe_signal")
    probe = probe_fn(
        feed=feed,
        strategy=strategy,
        last_known_signal=last_signal,
    )

    if probe.probe_error:
        return ProbeDecision(
            feed_name=feed_name,
            decision=PROBE_RESULT_ERROR,
            probe=probe,
            feed_date_for_dispatch=None,
        )

    if not probe.is_fresh:
        # Carve-out: STALE only short-circuits dispatch when BOTH of these
        # hold: (a) last_successful_feed_date is non-NULL (cursor exists),
        # AND (b) the manifest carries actual ingest evidence for this feed
        # (a terminal-success row in bulk_ingest.feed_ingest_runs). Either
        # clause failing → fall through to FRESH dispatch.
        #
        # The (a)-only check was the original carve-out. (b) was added
        # 2026-05-10 after 7 SMS feeds deadlocked: the 2026-05-08 03:01
        # cutover migration carried Postgres-era last_successful_feed_date
        # cursors into the new R2-era schedule_config, but R2 was empty.
        # The cursor said "we have it"; the manifest said "no, we don't."
        # The manifest is authoritative — it's where the writer leaves
        # evidence — so we trust it.
        #
        # Consequence: a chronically-failing feed will retry every
        # heartbeat tick (every 15 min) until either it succeeds or the
        # upstream signal moves. Acceptable v0; if retry storms surface,
        # add a `last_run_at + cooldown` backoff to this branch.
        if (isinstance(last_successful, date)
                and feed_name in feeds_with_manifest_evidence):
            return ProbeDecision(
                feed_name=feed_name,
                decision=PROBE_RESULT_STALE,
                probe=probe,
                feed_date_for_dispatch=None,
            )

    feed_date_for_dispatch: str | None = None
    if probe.observed_feed_date is not None:
        feed_date_for_dispatch = probe.observed_feed_date.isoformat()
    return ProbeDecision(
        feed_name=feed_name,
        decision=PROBE_RESULT_FRESH,
        probe=probe,
        feed_date_for_dispatch=feed_date_for_dispatch,
    )


def _select_due_feeds_via_probes(
    *,
    schedule_rows: list[dict[str, Any]],
    now_et: datetime,
    in_flight_feeds: set[str],
    feeds_with_manifest_evidence: set[str],
    probe_fn: Callable[..., ProbeResult] = probe_feed_freshness,
) -> list[ProbeDecision]:
    today_et = now_et.date()
    decisions: list[ProbeDecision] = []
    for row in schedule_rows:
        decision = _evaluate_feed_for_dispatch(
            row=row,
            now_et=now_et,
            today_et=today_et,
            in_flight_feeds=in_flight_feeds,
            feeds_with_manifest_evidence=feeds_with_manifest_evidence,
            probe_fn=probe_fn,
        )
        if decision is not None:
            decisions.append(decision)
    return decisions


# retry-policy: no-retry-orchestrator
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    schedule=modal.Cron(HEARTBEAT_CRON_EXPRESSION, timezone="America/New_York"),
    timeout=ORCHESTRATOR_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def schedule_heartbeat() -> dict[str, Any]:
    """Cron-fired heartbeat: probe each enabled feed's upstream URL and
    dispatch any whose artifact looks newer than what we last ingested.

    The legacy fixed-cron model — three clock-time waves at 10:05/11:15/
    12:05 ET, all of which were guessed publish times — has been replaced
    with per-tick availability polling. Each tick:

        1. Reads the schedule_config and the in-flight manifest snapshot.
        2. For each enabled feed, runs a freshness probe against the
           upstream download_url (HEAD-with-Location for txt feeds,
           GET-headers for sms/csv feeds).
        3. Dispatches a fan-out orchestrator run via .spawn() — fire and
           forget — so the heartbeat returns in seconds rather than
           blocking on a 90-minute worker window. The next tick can run
           probes concurrently with the previous tick's orchestrator.

    Per-day idempotency, earliest-check-time, and in-flight checks are
    enforced on the dispatch side so a feed gets ingested at most once
    per ET calendar day.

    Refresh DAG triggering is a separate concern — see
    fmcsa_refresh_app.refresh_trigger_heartbeat (Option A: daily 23:30
    ET debounce). The heartbeat does NOT chain ingest → refresh.
    """
    now_et = datetime.now(TZ_ET).replace(second=0, microsecond=0)
    evaluated_at_iso = _utc_now_iso()
    schedule_rows = _fetch_schedule_rows()
    _record_heartbeat_evaluated(evaluated_at_iso)

    enabled_feed_names = [
        row.get("feed_name")
        for row in schedule_rows
        if row.get("enabled") and isinstance(row.get("feed_name"), str)
    ]
    in_flight_feeds = _check_in_flight_feeds(feed_names=enabled_feed_names)
    feeds_with_manifest_evidence = _feeds_with_manifest_evidence(
        feed_names=enabled_feed_names,
    )

    decisions = _select_due_feeds_via_probes(
        schedule_rows=schedule_rows,
        now_et=now_et,
        in_flight_feeds=in_flight_feeds,
        feeds_with_manifest_evidence=feeds_with_manifest_evidence,
    )

    probe_at_iso = _utc_now_iso()
    decision_counts: dict[str, int] = {}
    probe_record_errors: list[dict[str, str]] = []
    for decision in decisions:
        decision_counts[decision.decision] = decision_counts.get(decision.decision, 0) + 1
        # Stamp the probe outcome regardless of dispatch — the audit row
        # is the operator's only record that we checked at this tick.
        # Wrap in try/except: a single per-feed audit-write failure (bad
        # SQL coercion, transient DB error) must NOT take down the whole
        # tick. The 2026-05-08 IndeterminateDatatype incident burned 12
        # hours of dispatches because one missing ::text cast crashed
        # every */15-min cron run. Defensive wrap = resilience.
        try:
            _record_probe_outcome(
                feed_name=decision.feed_name,
                probe_at_iso=probe_at_iso,
                probe_result=decision.decision,
                probe=decision.probe,
            )
        except Exception as exc:  # noqa: BLE001
            probe_record_errors.append(
                {"feed_name": decision.feed_name, "error": str(exc)[:200]}
            )
            print(
                f"[heartbeat] _record_probe_outcome failed for "
                f"{decision.feed_name}: {exc}"
            )

    fresh_decisions = [d for d in decisions if d.decision == PROBE_RESULT_FRESH]
    if not fresh_decisions:
        return {
            "evaluated_at": evaluated_at_iso,
            "heartbeat_minutes": HEARTBEAT_MINUTES,
            "total_configured_feeds": len(schedule_rows),
            "decisions": decision_counts,
            "due_feeds": [],
            "run_id": None,
            "spawned": False,
        }

    due_feed_names = [d.feed_name for d in fresh_decisions]

    # Stamp last_run_at on the schedule_config rows we're about to
    # dispatch so the operator dashboard keeps the legacy "running"
    # signal during the worker window. The probe-outcome columns above
    # are additive, not a replacement.
    started_at_iso = _utc_now_iso()
    for feed_name in due_feed_names:
        _record_feed_state(
            feed_name=feed_name,
            run_at_iso=started_at_iso,
            run_status="running",
            run_summary={
                "state": "running",
                "started_at": started_at_iso,
                "trigger": "polling_heartbeat",
            },
        )

    spawned_run_id = str(uuid.uuid4())
    spawn_error: str | None = None
    try:
        ingest_run_orchestrator.spawn(
            feed_names=due_feed_names,
            feed_date=None,
            max_concurrency=DEFAULT_ORCHESTRATOR_CONCURRENCY,
        )
        spawned_ok = True
    except Exception as exc:  # noqa: BLE001
        # Modal spawn failures (queue full, function unreachable, image
        # mismatch) must surface, not get silently dropped. The audit
        # rows in source_metadata.last_run_summary still indicate that
        # the heartbeat thought these feeds were due — the operator's
        # only signal that something went wrong is this print + the
        # absence of subsequent insert_pending_row rows.
        spawned_ok = False
        spawn_error = str(exc)[:500]
        print(f"[heartbeat] orchestrator.spawn failed: {exc}")

    return {
        "evaluated_at": evaluated_at_iso,
        "heartbeat_minutes": HEARTBEAT_MINUTES,
        "total_configured_feeds": len(schedule_rows),
        "decisions": decision_counts,
        "due_feeds": due_feed_names,
        "spawned": spawned_ok,
        "spawned_run_id_hint": spawned_run_id if spawned_ok else None,
        "spawn_error": spawn_error,
        "probe_record_errors": probe_record_errors,
    }


@app.local_entrypoint()
def run(
    feed_name: str = "",
    feed_date: str = "",
    run_all: bool = False,
    limit_feeds: int = 0,
    max_concurrency: int = DEFAULT_ORCHESTRATOR_CONCURRENCY,
) -> None:
    selected_date = feed_date or None
    if run_all:
        feed_names = [feed.feed_name for feed in FEED_CATALOG]
        if limit_feeds > 0:
            feed_names = feed_names[:limit_feeds]
        result = ingest_run_orchestrator.remote(
            feed_names=feed_names,
            feed_date=selected_date,
            max_concurrency=max_concurrency,
        )
        print(result)
        return
    if not feed_name:
        raise ValueError("Provide --feed-name or set --run-all=true")
    result = ingest_single_feed.remote(feed_name=feed_name, feed_date=selected_date)
    print(result)


if __name__ == "__main__":
    # Allows local execution in environments without Modal runner.
    local_feed = os.environ.get("FMCSA_FEED_NAME")
    if local_feed:
        print(
            _execute_ingest(
                feed_name=local_feed,
                feed_date=os.environ.get("FMCSA_FEED_DATE"),
                trigger="local",
            )
        )
