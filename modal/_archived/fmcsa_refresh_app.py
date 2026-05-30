"""FMCSA post-ingest refresh DAG (directive 135).

Runs after directive 134's parallel ingest pipeline declares a run
complete. Reads entities.fmcsa_ingest_runs to verify ingest health,
then refreshes the FMCSA "latest" MVs, the DOT-docket bridge, the
derived state MVs (insurance_state, growth_signals), the 8 delta MVs,
calls entities.fmcsa_populate_signal_events(), refreshes the targeting
MV, and finally runs retention pruning over the FMCSA raw tables.

Per-step audit lands in entities.fmcsa_refresh_runs (migration 135).

Critical-vs-non-critical step taxonomy (executor's call):
  CRITICAL (DAG aborts on failure):
    verify_ingest, refresh_carrier_master,
    refresh_dot_docket_bridge, populate_signal_events,
    refresh_carrier_targeting
  NON-CRITICAL (warning logged, DAG continues):
    everything else (incl. each individual delta MV, insurance_state,
    growth_signals, the 9 latest source MVs other than carrier_master,
    retention_prune)

Reasoning: the targeting MV (the customer-facing surface) joins
carrier_master + insurance_state + growth_signals + signal log; if
the bridge or carrier_master is missing, the targeting MV refresh
would fail or produce a stale view. populate_signal_events is the
durable-write step -- if it fails the customer-facing signal log
doesn't grow, which is the entire point of the daily DAG. Everything
else is loss-tolerant: a missing pdl_match refresh degrades enrichment
but doesn't block targeting.

Parallelization: sequential v0. The directive permits sequential and
the cumulative wall-time of all REFRESH CONCURRENTLY calls is
bounded; switching to asyncio + bounded concurrency is a future
optimization (tracked as a follow-up). Sequential keeps the DAG
trivially debuggable and avoids connection-pool sizing headaches.

Database connection: REFRESH MATERIALIZED VIEW CONCURRENTLY cannot run
through pgbouncer transaction-pool, so DATABASE_URL passed to the
Modal Secret MUST point to DEX_DB_URL_DIRECT (NOT DEX_DB_URL_POOLED).
The deploy wrapper (Doppler) is responsible for the env mapping; this
module does not check or remap.
"""

from __future__ import annotations

import json
import os
import time as time_mod
import uuid
from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone
from time import perf_counter
from typing import Any

import modal

from fmcsa.feed_catalog import FEED_CATALOG
from fmcsa.postgres_writer import connect_db

# ---------------------------------------------------------------------------
# Modal app config
# ---------------------------------------------------------------------------

app = modal.App("data-engine-x-fmcsa-refresh")
image = (
    modal.Image.debian_slim(python_version="3.11")
    # The repo migrated from requirements.txt to pyproject.toml as part of the
    # monorepo bootstrap. Modal 1.4+ supports pip_install_from_pyproject which
    # reads [project.dependencies] directly. Run `modal deploy` from
    # apps/data-engine-x/ so the relative path resolves.
    .pip_install_from_pyproject("modal/pyproject.toml")
    .add_local_dir("modal/fmcsa", remote_path="/root/fmcsa")
)
# Named Modal secret pointing at DEX_DB_URL_DIRECT (REFRESH MATERIALIZED
# VIEW CONCURRENTLY cannot run through pgbouncer transaction pool). Create
# once via:
#   doppler run -- bash -c 'modal secret create fmcsa-refresh-db DATABASE_URL="$DEX_DB_URL_DIRECT"'
# Switched off `Secret.from_dict({...os.environ...})` because Modal's deploy
# pipeline did not reliably propagate the local DATABASE_URL env var into
# the deployed Function.
FUNCTION_SECRETS = [modal.Secret.from_name("fmcsa-refresh-db")]

REFRESH_RUNS_TABLE = "entities.fmcsa_refresh_runs"
INGEST_RUNS_TABLE = "entities.fmcsa_ingest_runs"

# Per-step REFRESH timeout. carrier_targeting is the slowest; all others
# finish in seconds. 60min covers the slow case.
PER_REFRESH_STATEMENT_TIMEOUT = "60min"

# verify_ingest polls fmcsa_ingest_runs until no rows are pending/running
# for the given ingest_run_id, OR until this many seconds have elapsed.
VERIFY_INGEST_POLL_INTERVAL_SECONDS = 30
VERIFY_INGEST_MAX_WAIT_SECONDS = 300  # 5 minutes

# Hard caps on whole-DAG runtime.
REFRESH_DAG_TIMEOUT_SECONDS = 60 * 60 * 4  # 4h
FULL_PIPELINE_TIMEOUT_SECONDS = 60 * 60 * 12  # 12h (8h ingest + 4h refresh; ingest grew when XL workers landed)

DEFAULT_RETENTION_DAYS = 14

# Critical-step taxonomy (failure aborts the DAG).
CRITICAL_STEPS = frozenset(
    {
        "verify_ingest",
        "refresh_carrier_master",
        "refresh_dot_docket_bridge",
        "populate_signal_events",
        "refresh_carrier_targeting",
    }
)


# ---------------------------------------------------------------------------
# Step plan -- ordered list of (step_name, step_order, kind, payload)
# ---------------------------------------------------------------------------

# "Latest" source MVs (parallelizable layer in a future optimization;
# v0 runs sequentially in this order). The directive lists 9; carrier_master
# is critical because the bridge + insurance_state + targeting all join it.
LATEST_SOURCE_MVS: tuple[str, ...] = (
    "mv_fmcsa_carrier_master",
    "mv_fmcsa_latest_insurance_policies",
    "mv_fmcsa_latest_safety_percentiles",
    "mv_fmcsa_authority_grants",
    "mv_fmcsa_insurance_cancellations",
    "mv_fmcsa_crash_counts_12mo",
    "mv_fmcsa_new_carriers_90d",
    "mv_fmcsa_pdl_matches",
    "mv_fmcsa_supply_carriers",
    "mv_fmcsa_carrier_territory_fingerprint",
)

# Census + normalized-contact layer. Sourced directly from
# entities.motor_carrier_census_records (the giant ingest table) and the
# entities.raw_entity_*_records lookup tables. mv_fmcsa_carriers_normalized
# transitively depends on mv_fmcsa_latest_census, so latest_census MUST come
# first inside this tuple. All non-critical — these are derivative surfaces;
# their freshness matters for downstream search/match but the targeting MV
# does not require them. (None of these were in the v0 DAG; they were
# orphaned and refreshed by ad-hoc operator commands.)
NORMALIZED_LAYER_MVS: tuple[str, ...] = (
    "mv_fmcsa_latest_census",
    "mv_fmcsa_carrier_people",
    "mv_fmcsa_normalized_emails",
    "mv_fmcsa_normalized_telephones",
    "mv_fmcsa_normalized_cell_phones",
    "mv_fmcsa_non_personal_emails",
    "mv_fmcsa_carriers_normalized",  # depends on mv_fmcsa_latest_census
)

# FMCSA→cross-source bridges. Each depends on FMCSA-side MVs above PLUS
# cross-source MVs (DUNS/UEI xwalk, USAspending recipients, SAM entities)
# that are refreshed on their own non-FMCSA cadences. These steps therefore
# pick up "as-fresh-as-cross-source-allows" data each night. Non-critical.
CROSS_SOURCE_BRIDGE_MVS: tuple[str, ...] = (
    "mv_fmcsa_to_usaspending_recipients",  # depends on carriers_normalized
    "mv_fmcsa_usaspending_sam_bridge",  # depends on to_usaspending_recipients
)

DERIVED_STATE_MVS: tuple[str, ...] = (
    "mv_fmcsa_carrier_insurance_state",  # depends on dot_docket_bridge
    "mv_fmcsa_carrier_growth_signals",   # depends on census MVs only
)

DELTA_MVS: tuple[str, ...] = (
    "mv_fmcsa_signal_delta_cancellations",
    "mv_fmcsa_signal_delta_policies",
    "mv_fmcsa_signal_delta_authority_grants",
    "mv_fmcsa_signal_delta_revocations",
    "mv_fmcsa_signal_delta_oos",
    "mv_fmcsa_signal_delta_census",
    "mv_fmcsa_signal_delta_crashes",
    "mv_fmcsa_signal_delta_oos_inspections",
)

# fmcsa.* canonical-layer tables. Refreshed via fmcsa.refresh_<table>()
# Postgres functions (migration 20260507200000_create_fmcsa_refresh_functions.sql).
# Independent of each other and of the entities.* MVs — sourced directly from
# entities.motor_carrier_census_records (5 of the 8) plus entities.carrier_registrations,
# entities.insurance_policy_*, entities.insurance_filing_rejections,
# entities.carrier_inspections, entities.carrier_safety_basic_percentiles.
# v0 sequential; could parallelize later. All non-critical — failure of a
# canonical refresh shouldn't abort the downstream signal/targeting steps that
# don't depend on it.
CANONICAL_TABLES: tuple[str, ...] = (
    "carrier_records",
    "carrier_registration_records",
    "carrier_authority_records",
    "carrier_authority_event_records",
    "carrier_insurance_policy_records",
    "carrier_insurance_active_policy_records",
    "carrier_insurance_event_records",
    "carrier_officer_records",
    "carrier_inspection_location_records",
    "carrier_safety_basic_records",
    "carrier_inspection_records",
    "carrier_crash_records",
)

# Carrier-health intelligence layer (5 MVs from migration
# 20260510010000_create_carrier_health_intelligence). The
# entities.refresh_carrier_health_intelligence(true) function loops over the 5
# MVs and uses CONCURRENTLY when each has prior data; falls back to
# non-concurrent on first refresh. Non-critical: failure leaves the
# carrier-health surface stale but does not block the targeting MV refresh.
CARRIER_HEALTH_REFRESH_FUNCTION = (
    "entities.refresh_carrier_health_intelligence"
)


def _step_name_for_mv(mv_name: str) -> str:
    """Strip the 'mv_fmcsa_' prefix to derive a step name."""
    prefix = "mv_fmcsa_"
    return "refresh_" + (mv_name[len(prefix):] if mv_name.startswith(prefix) else mv_name)


def _step_name_for_canonical(table_name: str) -> str:
    """Step name for a fmcsa.<table_name> canonical refresh."""
    return f"refresh_fmcsa_{table_name}"


def _build_step_plan() -> list[tuple[str, int, str, dict[str, Any]]]:
    """Return ordered (step_name, step_order, kind, payload) tuples.

    kind is one of: 'verify', 'refresh_mv', 'refresh_canonical_table',
    'carrier_health', 'populate_events', 'retention_prune'.
    """
    plan: list[tuple[str, int, str, dict[str, Any]]] = []
    order = 1

    plan.append(("verify_ingest", order, "verify", {})); order += 1

    for mv in LATEST_SOURCE_MVS:
        plan.append((_step_name_for_mv(mv), order, "refresh_mv", {"mv_name": mv})); order += 1

    for mv in NORMALIZED_LAYER_MVS:
        plan.append((_step_name_for_mv(mv), order, "refresh_mv", {"mv_name": mv})); order += 1

    # fmcsa.* canonical-layer refreshes. Independent of LATEST_SOURCE_MVS and
    # of each other; sourced directly from entities.<source>_records. All
    # non-critical for v0 — failure of a canonical refresh shouldn't abort
    # downstream signal/targeting steps that don't depend on it.
    for table in CANONICAL_TABLES:
        plan.append((_step_name_for_canonical(table), order, "refresh_canonical_table", {"table_name": table})); order += 1

    plan.append(("refresh_carrier_health_intelligence", order, "carrier_health", {})); order += 1

    for mv in CROSS_SOURCE_BRIDGE_MVS:
        plan.append((_step_name_for_mv(mv), order, "refresh_mv", {"mv_name": mv})); order += 1

    plan.append(("refresh_dot_docket_bridge", order, "refresh_mv", {"mv_name": "mv_fmcsa_dot_docket_bridge"})); order += 1

    for mv in DERIVED_STATE_MVS:
        plan.append((_step_name_for_mv(mv), order, "refresh_mv", {"mv_name": mv})); order += 1

    for mv in DELTA_MVS:
        plan.append((_step_name_for_mv(mv), order, "refresh_mv", {"mv_name": mv})); order += 1

    plan.append(("populate_signal_events", order, "populate_events", {})); order += 1
    plan.append(("refresh_carrier_targeting", order, "refresh_mv", {"mv_name": "mv_fmcsa_carrier_targeting"})); order += 1
    plan.append(("retention_prune", order, "retention_prune", {})); order += 1

    return plan


STEP_PLAN = _build_step_plan()


# ---------------------------------------------------------------------------
# Audit-table writers (entities.fmcsa_refresh_runs)
# ---------------------------------------------------------------------------


def insert_step_pending(
    *,
    refresh_run_id: str,
    ingest_run_id: str,
    step: str,
    step_order: int,
) -> None:
    """Insert a 'pending' row. Idempotent on (refresh_run_id, step)."""
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                INSERT INTO {REFRESH_RUNS_TABLE}
                  (refresh_run_id, ingest_run_id, step, step_order, status)
                VALUES (%s, %s, %s, %s, 'pending')
                ON CONFLICT (refresh_run_id, step) DO NOTHING
                """,
                (refresh_run_id, ingest_run_id, step, step_order),
            )
        connection.commit()


def update_step_running(*, refresh_run_id: str, step: str) -> None:
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {REFRESH_RUNS_TABLE}
                SET status = 'running',
                    started_at = COALESCE(started_at, NOW()),
                    updated_at = NOW()
                WHERE refresh_run_id = %s AND step = %s
                """,
                (refresh_run_id, step),
            )
        connection.commit()


def update_step_completed(
    *,
    refresh_run_id: str,
    step: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {REFRESH_RUNS_TABLE}
                SET status = 'completed',
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    details = COALESCE(%s::jsonb, details),
                    updated_at = NOW()
                WHERE refresh_run_id = %s AND step = %s
                """,
                (json.dumps(details) if details is not None else None, refresh_run_id, step),
            )
        connection.commit()


def update_step_failed(
    *,
    refresh_run_id: str,
    step: str,
    error_message: str,
    error_class: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {REFRESH_RUNS_TABLE}
                SET status = 'failed',
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    error_message = %s,
                    error_class = %s,
                    details = COALESCE(%s::jsonb, details),
                    updated_at = NOW()
                WHERE refresh_run_id = %s AND step = %s
                """,
                (
                    (error_message or "")[:4000],
                    error_class,
                    json.dumps(details) if details is not None else None,
                    refresh_run_id,
                    step,
                ),
            )
        connection.commit()


def update_step_skipped(
    *,
    refresh_run_id: str,
    step: str,
    details: dict[str, Any] | None = None,
) -> None:
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {REFRESH_RUNS_TABLE}
                SET status = 'skipped',
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - COALESCE(started_at, NOW()))),
                    details = COALESCE(%s::jsonb, details),
                    updated_at = NOW()
                WHERE refresh_run_id = %s AND step = %s
                """,
                (json.dumps(details) if details is not None else None, refresh_run_id, step),
            )
        connection.commit()


def fetch_existing_steps(*, refresh_run_id: str) -> dict[str, dict[str, Any]]:
    """Return existing step rows for resume support: {step: {status, ...}}."""
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT step, step_order, status, details
                FROM {REFRESH_RUNS_TABLE}
                WHERE refresh_run_id = %s
                """,
                (refresh_run_id,),
            )
            rows = cursor.fetchall() or []
    return {row["step"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# Ingest-manifest reader (entities.fmcsa_ingest_runs)
# ---------------------------------------------------------------------------


def _read_ingest_run_status(*, ingest_run_id: str) -> dict[str, int]:
    """Return per-status counts {pending, running, completed, failed,
    timed_out, skipped} for the ingest_run_id."""
    rollup = {
        "pending": 0,
        "running": 0,
        "completed": 0,
        "failed": 0,
        "timed_out": 0,
        "skipped": 0,
    }
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT status, COUNT(*)::bigint AS cnt
                FROM {INGEST_RUNS_TABLE}
                WHERE run_id = %s
                GROUP BY status
                """,
                (ingest_run_id,),
            )
            rows = cursor.fetchall() or []
    for row in rows:
        rollup[row["status"]] = int(row["cnt"])
    return rollup


def _ingest_run_terminal(rollup: dict[str, int]) -> bool:
    """A run is terminal once no rows are still pending or running."""
    return rollup.get("pending", 0) == 0 and rollup.get("running", 0) == 0


def _ingest_completed_pct(rollup: dict[str, int]) -> float:
    total = sum(rollup.values())
    if total == 0:
        return 0.0
    return (rollup.get("completed", 0) / total) * 100.0


# ---------------------------------------------------------------------------
# Step kind handlers
# ---------------------------------------------------------------------------


def _verify_ingest(
    *,
    ingest_run_id: str,
    mode: str,
    min_completed_pct: float,
    poll_interval_seconds: int = VERIFY_INGEST_POLL_INTERVAL_SECONDS,
    max_wait_seconds: int = VERIFY_INGEST_MAX_WAIT_SECONDS,
    sleep_fn: Callable[[float], None] = time_mod.sleep,
    status_reader: Callable[..., dict[str, int]] = _read_ingest_run_status,
) -> dict[str, Any]:
    """Block until ingest_run is terminal; decide proceed/abort.

    Returns a details dict; raises RuntimeError if ingest gate fails so
    the caller can record the verify_ingest step as failed.
    """
    waited = 0.0
    rollup: dict[str, int] = {}
    while True:
        rollup = status_reader(ingest_run_id=ingest_run_id)
        if _ingest_run_terminal(rollup):
            break
        if waited >= max_wait_seconds:
            raise RuntimeError(
                f"verify_ingest: ingest run {ingest_run_id} still has "
                f"pending/running rows after {max_wait_seconds}s "
                f"(rollup={rollup})"
            )
        sleep_fn(poll_interval_seconds)
        waited += poll_interval_seconds

    completed_pct = _ingest_completed_pct(rollup)
    total = sum(rollup.values())

    if total == 0:
        raise RuntimeError(
            f"verify_ingest: ingest run {ingest_run_id} has no manifest rows"
        )

    decision = "proceed"
    if mode == "strict" and completed_pct < 100.0:
        decision = "abort"
    elif mode == "lenient" and completed_pct < min_completed_pct:
        decision = "abort"

    details = {
        "ingest_run_id": ingest_run_id,
        "rollup": rollup,
        "total": total,
        "completed_pct": round(completed_pct, 2),
        "mode": mode,
        "min_completed_pct": min_completed_pct,
        "decision": decision,
        "wait_seconds": waited,
    }
    if decision == "abort":
        raise RuntimeError(
            f"verify_ingest: aborting (mode={mode}, completed_pct="
            f"{completed_pct:.2f}, min={min_completed_pct})"
        )
    return details


def _refresh_mv(*, mv_name: str) -> dict[str, Any]:
    """REFRESH MATERIALIZED VIEW CONCURRENTLY entities.<mv_name> and
    return post-refresh row count."""
    started = perf_counter()
    qualified = f"entities.{mv_name}"
    with connect_db() as connection:
        # autocommit must be ON for REFRESH CONCURRENTLY (it cannot run
        # inside a transaction block).
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = '{PER_REFRESH_STATEMENT_TIMEOUT}'")
            cursor.execute(f'REFRESH MATERIALIZED VIEW CONCURRENTLY "entities"."{mv_name}"')
            cursor.execute(f"SELECT COUNT(*)::bigint AS row_count FROM {qualified}")
            row = cursor.fetchone() or {}
    return {
        "mv_name": mv_name,
        "row_count": int(row.get("row_count") or 0),
        "duration_seconds": round(perf_counter() - started, 2),
        "statement_timeout": PER_REFRESH_STATEMENT_TIMEOUT,
    }


def _refresh_canonical_table(*, table_name: str) -> dict[str, Any]:
    """Call fmcsa.refresh_<table_name>() and return rows-affected + table count.

    Each fmcsa.refresh_*() function (defined in migration
    20260507200000_create_fmcsa_refresh_functions.sql) wraps the canonical-
    layer upsert. Functions set their own SET LOCAL statement_timeout='60min'
    + work_mem='1GB' inside the function body; we also disable the calling
    session's statement_timeout up front as a safety net so the outer SELECT
    isn't cancelled before the function's SET LOCAL takes effect.

    Returns rows_affected (from the function's RETURN) and post-refresh table
    row count for audit. Idempotent: rows_affected=0 when source feed_date
    hasn't advanced since the last refresh.
    """
    started = perf_counter()
    function_call = f"fmcsa.refresh_{table_name}"
    qualified = f"fmcsa.{table_name}"
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")
            cursor.execute(f'SELECT {function_call}() AS rows_affected')
            rows_affected_row = cursor.fetchone() or {}
            cursor.execute(f"SELECT COUNT(*)::bigint AS row_count FROM {qualified}")
            row_count_row = cursor.fetchone() or {}
        connection.commit()
    return {
        "table_name": table_name,
        "rows_affected": int(rows_affected_row.get("rows_affected") or 0),
        "row_count": int(row_count_row.get("row_count") or 0),
        "duration_seconds": round(perf_counter() - started, 2),
        "statement_timeout": PER_REFRESH_STATEMENT_TIMEOUT,
    }


def _refresh_carrier_health_intelligence() -> dict[str, Any]:
    """Call entities.refresh_carrier_health_intelligence(true) and return the
    per-MV summary jsonb.

    The function refreshes 5 carrier-health MVs in sequence, using
    CONCURRENTLY when each has prior data. On first invocation after the
    migration the MVs are populated, so CONCURRENTLY is the expected path.
    Statement timeout is disabled on the calling session as a safety net;
    the function relies on its own clock budget per MV.
    """
    started = perf_counter()
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = 0")
            cursor.execute(f"SELECT {CARRIER_HEALTH_REFRESH_FUNCTION}(true) AS summary")
            row = cursor.fetchone() or {}
        connection.commit()
    return {
        "summary": row.get("summary"),
        "duration_seconds": round(perf_counter() - started, 2),
    }


def _populate_signal_events() -> dict[str, Any]:
    """Call entities.fmcsa_populate_signal_events() and return per-type
    insert counts."""
    started = perf_counter()
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT signal_type, inserted_count FROM entities.fmcsa_populate_signal_events()")
            rows = cursor.fetchall() or []
        connection.commit()
    per_type = {row["signal_type"]: int(row["inserted_count"]) for row in rows}
    return {
        "per_signal_type": per_type,
        "total_inserted": sum(per_type.values()),
        "duration_seconds": round(perf_counter() - started, 2),
    }


def _retention_prune(
    *,
    retention_days: int,
    table_names: Iterable[str],
) -> dict[str, Any]:
    """Drop feed_dates older than retention_days from each FMCSA raw
    table. Skip the entire step if no table has prunable rows.

    Returns details dict with per-table results (deleted rows + bytes
    freed). Uses regular VACUUM (not VACUUM FULL).
    """
    started = perf_counter()
    table_list = sorted(set(table_names))
    per_table: list[dict[str, Any]] = []
    total_deleted = 0
    total_freed_bytes = 0
    any_pruned = False

    with connect_db() as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(f"SET statement_timeout = '{PER_REFRESH_STATEMENT_TIMEOUT}'")
            for tbl in table_list:
                qualified = f'"entities"."{tbl}"'
                # MIN(feed_date) check -- skip table if no prunable rows.
                cursor.execute(
                    f"SELECT MIN(feed_date) AS min_fd FROM {qualified}"
                )
                row = cursor.fetchone() or {}
                min_fd = row.get("min_fd")
                # Pre-prune size.
                cursor.execute(
                    f"SELECT pg_total_relation_size('{qualified}'::regclass)::bigint AS sz"
                )
                pre_size = int((cursor.fetchone() or {}).get("sz") or 0)

                if min_fd is None:
                    per_table.append(
                        {"table": tbl, "deleted_rows": 0, "freed_bytes": 0,
                         "action": "skipped", "reason": "no rows"}
                    )
                    continue

                cursor.execute(
                    f"SELECT (CURRENT_DATE - %s::interval)::date AS cutoff",
                    (f"{retention_days} days",),
                )
                cutoff = (cursor.fetchone() or {}).get("cutoff")

                if min_fd > cutoff:
                    per_table.append(
                        {"table": tbl, "deleted_rows": 0, "freed_bytes": 0,
                         "action": "skipped", "reason": "no rows older than retention"}
                    )
                    continue

                # Prune.
                cursor.execute(
                    f"DELETE FROM {qualified} "
                    f"WHERE feed_date < CURRENT_DATE - %s::interval",
                    (f"{retention_days} days",),
                )
                deleted = int(cursor.rowcount or 0)
                # Regular VACUUM -- marks space reusable, no exclusive lock.
                cursor.execute(f"VACUUM {qualified}")
                cursor.execute(
                    f"SELECT pg_total_relation_size('{qualified}'::regclass)::bigint AS sz"
                )
                post_size = int((cursor.fetchone() or {}).get("sz") or 0)
                freed_bytes = max(0, pre_size - post_size)

                if deleted > 0:
                    any_pruned = True
                total_deleted += deleted
                total_freed_bytes += freed_bytes
                per_table.append(
                    {"table": tbl, "deleted_rows": deleted, "freed_bytes": freed_bytes,
                     "action": "pruned"}
                )

    return {
        "retention_days": retention_days,
        "per_table": per_table,
        "total_deleted": total_deleted,
        "total_freed_bytes": total_freed_bytes,
        "action": "prune" if any_pruned else "skipped",
        "duration_seconds": round(perf_counter() - started, 2),
    }


def _fmcsa_raw_table_names() -> list[str]:
    """Source of truth: derive from feed_catalog so retention auto-tracks
    new feeds added later."""
    return sorted({feed.table_name for feed in FEED_CATALOG})


# ---------------------------------------------------------------------------
# Pure-logic orchestrator
# ---------------------------------------------------------------------------


def _classify_exception(exc: BaseException) -> str:
    message = (str(exc) or "").lower()
    if "timed out" in message or "timeout" in message:
        return "timeout"
    type_name = type(exc).__name__.lower()
    module_name = (type(exc).__module__ or "").lower()
    if "psycopg" in module_name or "operationalerror" in type_name or "integrityerror" in type_name:
        return "db_failure"
    if type_name in {"valueerror", "keyerror", "typeerror"}:
        return "validation_failure"
    return "unknown"


# Test-injectable handler bundle. Production wires _verify_ingest /
# _refresh_mv / _refresh_canonical_table / _populate_signal_events /
# _retention_prune; tests substitute mocks.
class StepHandlers:
    def __init__(
        self,
        verify_ingest: Callable[..., dict[str, Any]] = _verify_ingest,
        refresh_mv: Callable[..., dict[str, Any]] = _refresh_mv,
        refresh_canonical_table: Callable[..., dict[str, Any]] = _refresh_canonical_table,
        refresh_carrier_health_intelligence: Callable[..., dict[str, Any]] = _refresh_carrier_health_intelligence,
        populate_signal_events: Callable[..., dict[str, Any]] = _populate_signal_events,
        retention_prune: Callable[..., dict[str, Any]] = _retention_prune,
    ) -> None:
        self.verify_ingest = verify_ingest
        self.refresh_mv = refresh_mv
        self.refresh_canonical_table = refresh_canonical_table
        self.refresh_carrier_health_intelligence = refresh_carrier_health_intelligence
        self.populate_signal_events = populate_signal_events
        self.retention_prune = retention_prune


# Test-injectable audit writer (defaults to the real DB writers above).
class AuditWriter:
    def __init__(
        self,
        insert_pending: Callable[..., None] = insert_step_pending,
        update_running: Callable[..., None] = update_step_running,
        update_completed: Callable[..., None] = update_step_completed,
        update_failed: Callable[..., None] = update_step_failed,
        update_skipped: Callable[..., None] = update_step_skipped,
        fetch_existing: Callable[..., dict[str, dict[str, Any]]] = fetch_existing_steps,
    ) -> None:
        self.insert_pending = insert_pending
        self.update_running = update_running
        self.update_completed = update_completed
        self.update_failed = update_failed
        self.update_skipped = update_skipped
        self.fetch_existing = fetch_existing


def _refresh_dag(
    *,
    ingest_run_id: str,
    refresh_run_id: str,
    mode: str,
    min_completed_pct: float,
    retention_days: int,
    handlers: StepHandlers,
    audit: AuditWriter,
    table_names_for_retention: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Pure-logic core. Walks STEP_PLAN and records audit rows."""
    started_at = datetime.now(timezone.utc)
    existing = audit.fetch_existing(refresh_run_id=refresh_run_id) or {}
    table_list = list(table_names_for_retention) if table_names_for_retention is not None else _fmcsa_raw_table_names()

    step_results: dict[str, dict[str, Any]] = {}
    aborted = False
    abort_reason: str | None = None

    for step_name, step_order, kind, payload in STEP_PLAN:
        # Resume: skip already-completed steps from a prior partial run.
        prior = existing.get(step_name)
        if prior and prior.get("status") == "completed":
            step_results[step_name] = {
                "status": "completed",
                "details": prior.get("details") or {},
                "resumed": True,
            }
            continue

        # If the DAG already aborted on a critical step, mark this step
        # as 'skipped' rather than 'pending' to leave a clear paper trail.
        if aborted:
            audit.insert_pending(
                refresh_run_id=refresh_run_id,
                ingest_run_id=ingest_run_id,
                step=step_name,
                step_order=step_order,
            )
            audit.update_skipped(
                refresh_run_id=refresh_run_id,
                step=step_name,
                details={"reason": f"aborted upstream: {abort_reason}"},
            )
            step_results[step_name] = {"status": "skipped", "reason": abort_reason}
            continue

        audit.insert_pending(
            refresh_run_id=refresh_run_id,
            ingest_run_id=ingest_run_id,
            step=step_name,
            step_order=step_order,
        )
        audit.update_running(refresh_run_id=refresh_run_id, step=step_name)

        try:
            if kind == "verify":
                details = handlers.verify_ingest(
                    ingest_run_id=ingest_run_id,
                    mode=mode,
                    min_completed_pct=min_completed_pct,
                )
            elif kind == "refresh_mv":
                details = handlers.refresh_mv(mv_name=payload["mv_name"])
            elif kind == "refresh_canonical_table":
                details = handlers.refresh_canonical_table(table_name=payload["table_name"])
            elif kind == "carrier_health":
                details = handlers.refresh_carrier_health_intelligence()
            elif kind == "populate_events":
                details = handlers.populate_signal_events()
            elif kind == "retention_prune":
                details = handlers.retention_prune(
                    retention_days=retention_days,
                    table_names=table_list,
                )
            else:
                raise RuntimeError(f"unknown step kind: {kind}")

            audit.update_completed(
                refresh_run_id=refresh_run_id,
                step=step_name,
                details=details,
            )
            step_results[step_name] = {"status": "completed", "details": details}
        except Exception as exc:
            error_class = _classify_exception(exc)
            error_message = str(exc) or repr(exc)
            audit.update_failed(
                refresh_run_id=refresh_run_id,
                step=step_name,
                error_message=error_message,
                error_class=error_class,
            )
            step_results[step_name] = {
                "status": "failed",
                "error_class": error_class,
                "error_message": error_message[:500],
            }
            if step_name in CRITICAL_STEPS:
                aborted = True
                abort_reason = f"{step_name}: {error_message[:200]}"
                print(f"[refresh_dag] CRITICAL step failed: {step_name} -- {error_message}")
            else:
                print(f"[refresh_dag] non-critical step failed (continuing): {step_name} -- {error_message}")

    completed_at = datetime.now(timezone.utc)

    # Roll up status. 'completed' = no failed steps; 'completed_with_warnings'
    # = some non-critical failed but no critical failed; 'failed' = any
    # critical failed.
    has_critical_failure = any(
        r.get("status") == "failed" for s, r in step_results.items() if s in CRITICAL_STEPS
    )
    has_any_failure = any(r.get("status") == "failed" for r in step_results.values())
    if has_critical_failure:
        overall = "failed"
    elif has_any_failure:
        overall = "completed_with_warnings"
    else:
        overall = "completed"

    populate_step = step_results.get("populate_signal_events", {})
    populate_details = populate_step.get("details") or {}
    signal_events_inserted = populate_details.get("per_signal_type") or {}

    retention_step = step_results.get("retention_prune", {})
    retention_details = retention_step.get("details") or {}
    retention_freed = retention_details.get("total_freed_bytes")

    return {
        "refresh_run_id": refresh_run_id,
        "ingest_run_id": ingest_run_id,
        "status": overall,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "duration_seconds": (completed_at - started_at).total_seconds(),
        "steps": step_results,
        "signal_events_inserted": signal_events_inserted,
        "retention_freed_bytes": retention_freed,
        "abort_reason": abort_reason,
    }


# ---------------------------------------------------------------------------
# Modal entry points
# ---------------------------------------------------------------------------


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=REFRESH_DAG_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def refresh_after_ingest(
    ingest_run_id: str,
    mode: str = "strict",
    min_completed_pct: float = 100.0,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    refresh_run_id: str | None = None,
) -> dict[str, Any]:
    """Run the post-ingest refresh DAG for a completed ingest run.

    Args:
        ingest_run_id: the run_id from entities.fmcsa_ingest_runs.
        mode: 'strict' (require 100% completed) or 'lenient' (require
            min_completed_pct).
        min_completed_pct: floor for lenient mode.
        retention_days: drop feed_dates older than this from FMCSA raw
            tables. Default 14.
        refresh_run_id: optional explicit id (idempotent re-run support;
            omit for a fresh run).

    Returns the orchestrator summary dict.
    """
    rid = refresh_run_id or str(uuid.uuid4())
    print(
        f"[refresh_after_ingest] refresh_run_id={rid} "
        f"ingest_run_id={ingest_run_id} mode={mode} "
        f"retention_days={retention_days}"
    )
    return _refresh_dag(
        ingest_run_id=ingest_run_id,
        refresh_run_id=rid,
        mode=mode,
        min_completed_pct=min_completed_pct,
        retention_days=retention_days,
        handlers=StepHandlers(),
        audit=AuditWriter(),
    )


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    timeout=FULL_PIPELINE_TIMEOUT_SECONDS,
    memory=2048,
    cpu=1,
)
def run_full_pipeline(
    feed_names: list[str] | None = None,
    feed_date: str | None = None,
    max_concurrency: int = 6,
    refresh_mode: str = "strict",
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, Any]:
    """End-to-end: dispatch ingest, then refresh DAG.

    Calls into the sibling fmcsa_ingest_app.ingest_run_orchestrator
    Modal Function via .remote(), captures the run_id, then calls
    refresh_after_ingest. Daily-cron entry point.
    """
    # Resolve the sibling Modal Function lazily so this module imports
    # cleanly even if fmcsa_ingest_app isn't deployed (test/dev).
    from modal import Function

    ingest_orch = Function.from_name(
        "data-engine-x-fmcsa-ingest", "ingest_run_orchestrator"
    )
    ingest_summary = ingest_orch.remote(
        feed_names=feed_names,
        feed_date=feed_date,
        max_concurrency=max_concurrency,
    )
    ingest_run_id = ingest_summary.get("run_id")
    if not ingest_run_id:
        return {
            "ingest": ingest_summary,
            "refresh": None,
            "error": "ingest produced no run_id (no valid feeds?)",
        }
    refresh_summary = refresh_after_ingest.remote(
        ingest_run_id=ingest_run_id,
        mode=refresh_mode,
        retention_days=retention_days,
    )
    return {"ingest": ingest_summary, "refresh": refresh_summary}


# ---------------------------------------------------------------------------
# Refresh trigger heartbeat (Option A: daily 23:30 ET debounce)
# ---------------------------------------------------------------------------
#
# Replaces the orphaned ingest -> refresh wiring. Under the polling-based
# ingest model, feeds are dispatched throughout the day with potentially
# multiple distinct ingest_run_ids (one per heartbeat tick that found fresh
# feeds). Rather than fire a refresh DAG per ingest_run_id (which would
# REFRESH MATERIALIZED VIEW the same MVs N times), we debounce: at 23:30
# ET each day, fire ONE refresh DAG against the latest terminal ingest_run
# of the day. The MVs read from the source tables, so a single refresh at
# end-of-day picks up every feed that landed.
#
# Idempotency: skip the trigger if any fmcsa_refresh_runs row already
# exists for today (operator may have run it manually). The audit table
# is the source of truth — no separate "refresh_fired" flag.

REFRESH_TRIGGER_CRON_EXPRESSION = "30 23 * * *"  # 23:30 ET daily


def _select_refresh_trigger_target(*, today_et: date) -> dict[str, Any] | None:
    """Pick the ingest_run_id to feed into refresh_after_ingest, or return
    None if today's refresh is already done / nothing to refresh.

    Returns a dict with shape:
      {ingest_run_id: str, completed_at: datetime, total_runs_today: int}
    """
    with connect_db() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT 1
                FROM {REFRESH_RUNS_TABLE}
                WHERE created_at >= %s
                LIMIT 1
                """,
                (today_et,),
            )
            already_run = cursor.fetchone()
        if already_run:
            return None

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH today AS (
                    SELECT
                        run_id,
                        MAX(completed_at) AS run_completed_at,
                        BOOL_AND(status NOT IN ('pending', 'running')) AS is_terminal,
                        BOOL_OR(status = 'completed') AS has_completion
                    FROM {INGEST_RUNS_TABLE}
                    WHERE COALESCE(completed_at, started_at, created_at) >= %s
                    GROUP BY run_id
                )
                SELECT
                    run_id,
                    run_completed_at,
                    (SELECT COUNT(*) FROM today) AS total_runs_today
                FROM today
                WHERE is_terminal AND has_completion
                ORDER BY run_completed_at DESC NULLS LAST
                LIMIT 1
                """,
                (today_et,),
            )
            row = cursor.fetchone()
    if not row:
        return None
    return {
        "ingest_run_id": str(row["run_id"]),
        "run_completed_at": row["run_completed_at"],
        "total_runs_today": int(row["total_runs_today"] or 0),
    }


REFRESH_TRIGGER_TIMEOUT_SECONDS = 60 * 5  # 5 minutes — this fn just spawns


# retry-policy: no-retry
@app.function(
    image=image,
    secrets=FUNCTION_SECRETS,
    # NIGHTLY CRON DISABLED 2026-05-07 — entities.mv_fmcsa_* MVs dropped
    # ahead of RisingWave cutover; the 45-step DAG has no MVs to refresh.
    # Function remains deployed for on-demand operator use; re-enable by
    # restoring the schedule= line below and `modal deploy`.
    # schedule=modal.Cron(
    #     REFRESH_TRIGGER_CRON_EXPRESSION, timezone="America/New_York"
    # ),
    timeout=REFRESH_TRIGGER_TIMEOUT_SECONDS,
    memory=512,
    cpu=1,
)
def refresh_trigger_heartbeat(
    *,
    target_selector: Callable[..., dict[str, Any] | None] = _select_refresh_trigger_target,
    refresh_spawner: Callable[..., Any] | None = None,
    today_et: date | None = None,
) -> dict[str, Any]:
    """Daily 23:30 ET trigger: spawn refresh_after_ingest if today's
    ingest produced terminal completions and no refresh row exists yet.

    Args are test-injectable; production calls with no overrides.
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    resolved_today = today_et or _dt.now(_ZI("America/New_York")).date()
    target = target_selector(today_et=resolved_today)
    if not target:
        return {
            "spawned": False,
            "reason": "no terminal ingest run today, or refresh already fired",
            "today_et": resolved_today.isoformat(),
        }

    spawn_fn = refresh_spawner or refresh_after_ingest.spawn
    spawn_fn(
        ingest_run_id=target["ingest_run_id"],
        mode="lenient",
        min_completed_pct=50.0,
    )
    return {
        "spawned": True,
        "today_et": resolved_today.isoformat(),
        "target_ingest_run_id": target["ingest_run_id"],
        "total_terminal_runs_today": target["total_runs_today"],
    }


@app.local_entrypoint()
def run(
    ingest_run_id: str = "",
    mode: str = "strict",
    retention_days: int = DEFAULT_RETENTION_DAYS,
    full_pipeline: bool = False,
) -> None:
    if full_pipeline:
        result = run_full_pipeline.remote(
            refresh_mode=mode, retention_days=retention_days
        )
        print(json.dumps(result, indent=2, default=str))
        return
    if not ingest_run_id:
        raise ValueError("Provide --ingest-run-id or set --full-pipeline=true")
    result = refresh_after_ingest.remote(
        ingest_run_id=ingest_run_id,
        mode=mode,
        retention_days=retention_days,
    )
    print(json.dumps(result, indent=2, default=str))
