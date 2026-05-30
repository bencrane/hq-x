"""ops.scheduled_tasks — registry CRUD, the operator gate, and the status engine.

The status engine answers the operator's core question — *did the schedule
actually fire when it should have?* — by grading the actual Trigger.dev run
history against the cadence stored in the registry:

  green   the most-recent MATURED fire has a completed run in its window
  red     that fire's window closed with no run (missed) or the run failed
  amber   a run for that fire exists but is in-flight / late / non-terminal
  grey    the fire window is still open (pending), or the task is brand-new
          and awaiting its first tracked fire, or run data is unavailable
  disabled  operator has gated the task off

"Matured fire" = the most recent scheduled fire whose grace window has already
closed. Grading the matured fire (not the raw last fire) is what makes the model
work across frequencies: an every-minute task whose last fire is always ~now is
graded on the fire one step back, so a stall is still caught.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from croniter import croniter

from app.db import get_db_connection
from app.services import trigger_dev_client

log = logging.getLogger(__name__)

# Tolerance for a scheduled run being created a hair before its tick.
_SKEW = timedelta(minutes=5)
# How long to cache the (expensive) Trigger.dev run fetch. The UI polls every
# 30s; a 20s TTL collapses concurrent operators onto one upstream sweep without
# making the view feel stale.
_RUNS_TTL_SEC = 20.0
# Bounded concurrency for the per-task run lookups.
_FETCH_CONCURRENCY = 12

# Trigger.dev run-status buckets (compared case-insensitively).
_SUCCESS = {"completed"}
_FAILURE = {"failed", "crashed", "system_failure", "timed_out", "expired"}
_INFLIGHT = {
    "executing", "queued", "dequeued", "waiting", "reattempting",
    "pending_version", "delayed",
}

_COLUMNS = (
    "task_id", "label", "description", "category", "priority", "is_sla_critical",
    "cron", "cron_human", "timezone", "execution_kind", "modal_app",
    "modal_function", "hqx_endpoint", "produces", "grace_minutes", "is_enabled",
    "disabled_at", "disabled_by", "disable_reason", "last_gate_check_at",
    "last_gate_decision", "notes", "created_at", "updated_at",
)
_SELECT = f"SELECT {', '.join(_COLUMNS)} FROM ops.scheduled_tasks"

# Module-level cache for the upstream run sweep.
_runs_cache: dict[str, Any] = {"at": 0.0, "data": None, "ok": False}


def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    out = dict(zip(_COLUMNS, row, strict=True))
    for k, v in out.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
    return out


# ── registry reads / writes ─────────────────────────────────────────────────

async def list_registry() -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _SELECT + " ORDER BY priority ASC, category ASC, task_id ASC"
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_one(task_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT + " WHERE task_id = %s", (task_id,))
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def update_task(
    task_id: str,
    *,
    is_enabled: bool | None = None,
    priority: int | None = None,
    is_sla_critical: bool | None = None,
    notes: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> dict[str, Any] | None:
    """Patch operator-owned columns. When is_enabled flips, stamp the audit
    trio (disabled_at / disabled_by / disable_reason)."""
    sets: list[str] = []
    args: list[Any] = []
    if is_enabled is not None:
        sets.append("is_enabled = %s")
        args.append(is_enabled)
        if is_enabled:
            sets += ["disabled_at = NULL", "disabled_by = NULL", "disable_reason = NULL"]
        else:
            sets += ["disabled_at = now()", "disabled_by = %s", "disable_reason = %s"]
            args += [actor, reason]
    if priority is not None:
        sets.append("priority = %s")
        args.append(priority)
    if is_sla_critical is not None:
        sets.append("is_sla_critical = %s")
        args.append(is_sla_critical)
    if notes is not None:
        sets.append("notes = %s")
        args.append(notes)
    if not sets:
        return await get_one(task_id)
    sets.append("updated_at = now()")
    args.append(task_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE ops.scheduled_tasks SET {', '.join(sets)} "
                f"WHERE task_id = %s RETURNING {', '.join(_COLUMNS)}",
                tuple(args),
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_dict(row) if row else None


async def gate(task_id: str) -> dict[str, Any]:
    """The operator gate. Stamp the fire-ledger (last_gate_check_at) and return
    whether the task should run. Unknown task_id fails OPEN (run=True) — the
    gate must never be the reason a not-yet-registered schedule stops working."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE ops.scheduled_tasks "
                "SET last_gate_check_at = now(), last_gate_decision = is_enabled "
                "WHERE task_id = %s RETURNING is_enabled",
                (task_id,),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        return {"run": True, "enabled": True, "reason": "not_registered"}
    enabled = bool(row[0])
    return {
        "run": enabled,
        "enabled": enabled,
        "reason": None if enabled else "disabled_by_operator",
    }


# ── Trigger.dev run sweep (cached) ──────────────────────────────────────────

async def _fetch_runs_for(task_id: str, sem: asyncio.Semaphore) -> list[dict[str, Any]]:
    async with sem:
        try:
            resp = await trigger_dev_client.list_runs(task_identifier=task_id, limit=10)
        except Exception as exc:  # noqa: BLE001
            log.warning("list_runs failed for %s: %s", task_id, exc)
            raise
    return resp.get("data") or []


async def _runs_map(task_ids: list[str]) -> tuple[dict[str, list[dict[str, Any]]], bool]:
    """Map task_id -> recent runs (newest first). Cached for _RUNS_TTL_SEC.
    Second element is False when the upstream sweep failed (e.g. the Trigger.dev
    key is unset in this env) so the caller can mark statuses 'unknown'."""
    now = time.monotonic()
    if _runs_cache["data"] is not None and now - _runs_cache["at"] < _RUNS_TTL_SEC:
        return _runs_cache["data"], _runs_cache["ok"]

    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)
    results = await asyncio.gather(
        *(_fetch_runs_for(t, sem) for t in task_ids), return_exceptions=True
    )
    out: dict[str, list[dict[str, Any]]] = {}
    failures = 0
    for tid, res in zip(task_ids, results, strict=True):
        if isinstance(res, BaseException):
            failures += 1
            out[tid] = []
        else:
            out[tid] = res
    ok = failures < len(task_ids)  # at least some succeeded
    _runs_cache.update({"at": now, "data": out, "ok": ok})
    return out, ok


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _run_ts(run: dict[str, Any]) -> datetime | None:
    for key in ("createdAt", "created_at", "startedAt", "started_at"):
        dt = _parse_dt(run.get(key))
        if dt is not None:
            return dt
    return None


def _matured_fire(cron_expr: str, now: datetime, grace: timedelta) -> datetime:
    """Most recent scheduled fire whose grace window has already closed."""
    prev = croniter(cron_expr, now).get_prev(datetime)
    if now - prev < grace:
        # The last fire is still within grace — grade the one before it.
        prev = croniter(cron_expr, prev).get_prev(datetime)
    return prev


def _compute_status(
    row: dict[str, Any], runs: list[dict[str, Any]], runs_ok: bool, now: datetime
) -> dict[str, Any]:
    cron_expr = row["cron"]
    grace = timedelta(minutes=int(row["grace_minutes"]))
    created_at = _parse_dt(row["created_at"]) or now

    try:
        prev_fire = croniter(cron_expr, now).get_prev(datetime)
        next_fire = croniter(cron_expr, now).get_next(datetime)
        due_fire = _matured_fire(cron_expr, now, grace)
    except (ValueError, KeyError):
        return {
            **row, "status": "grey", "status_reason": f"unparseable cron {cron_expr!r}",
            "last_run_at": None, "last_run_status": None, "last_run_id": None,
            "prev_fire": None, "next_fire": None,
        }

    runs_sorted = sorted(
        (r for r in runs if _run_ts(r) is not None),
        key=lambda r: _run_ts(r),  # type: ignore[arg-type, return-value]
        reverse=True,
    )
    last_run = runs_sorted[0] if runs_sorted else None
    last_run_at = _run_ts(last_run) if last_run else None
    last_run_status = (str(last_run.get("status", "")).lower() or None) if last_run else None
    last_run_id = last_run.get("id") if last_run else None

    base = {
        **row,
        "prev_fire": prev_fire.isoformat(),
        "next_fire": next_fire.isoformat(),
        "due_fire": due_fire.isoformat(),
        "last_run_at": last_run_at.isoformat() if last_run_at else None,
        "last_run_status": last_run_status,
        "last_run_id": last_run_id,
    }

    if not row["is_enabled"]:
        return {**base, "status": "disabled", "status_reason": "disabled by operator"}
    if not runs_ok:
        return {**base, "status": "grey", "status_reason": "run data unavailable"}

    # Find a run that lands in the matured fire's window [due - skew, due + grace].
    lo, hi = due_fire - _SKEW, due_fire + grace
    fire_run = next(
        (r for r in runs_sorted if lo <= _run_ts(r) <= hi),  # type: ignore[operator]
        None,
    )
    if fire_run is not None:
        st = str(fire_run.get("status", "")).lower()
        if st in _SUCCESS:
            return {**base, "status": "green", "status_reason": "fired on schedule"}
        if st in _FAILURE:
            return {**base, "status": "red", "status_reason": f"run {st}"}
        if st in _INFLIGHT:
            return {**base, "status": "amber", "status_reason": f"run {st}"}
        return {**base, "status": "amber", "status_reason": f"run {st or 'unknown'}"}

    # No run in the matured window.
    if last_run_at is None and due_fire < created_at:
        return {**base, "status": "grey", "status_reason": "awaiting first scheduled fire"}
    if last_run_at is None:
        return {**base, "status": "red", "status_reason": "never fired"}
    return {**base, "status": "red", "status_reason": "missed — no run in fire window"}


async def list_with_status() -> dict[str, Any]:
    """Registry rows enriched with computed status + a roll-up summary."""
    rows = await list_registry()
    runs, runs_ok = await _runs_map([r["task_id"] for r in rows])
    now = datetime.now(UTC)
    enriched = [_compute_status(r, runs.get(r["task_id"], []), runs_ok, now) for r in rows]

    summary = {
        "total": len(enriched),
        "green": sum(1 for e in enriched if e["status"] == "green"),
        "amber": sum(1 for e in enriched if e["status"] == "amber"),
        "red": sum(1 for e in enriched if e["status"] == "red"),
        "grey": sum(1 for e in enriched if e["status"] == "grey"),
        "disabled": sum(1 for e in enriched if e["status"] == "disabled"),
        "p1_red": sum(
            1 for e in enriched if e["status"] == "red" and e["is_sla_critical"]
        ),
        "runs_source_ok": runs_ok,
        "as_of": now.isoformat(),
    }
    return {"data": enriched, "summary": summary}
