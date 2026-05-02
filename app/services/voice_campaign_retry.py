"""Voice campaign retry — outcome processing + retry-decision helpers.

Ports the *callable* unit-of-work helpers out of the OEX
``voice_campaign_retry.py``. Per directive §10 #2 + handoff doc, the
scheduling side (``next_execute_at`` writes, ``campaign_lead_progress``
mutations) is **not** ported — the OEX orchestrator's lead-progress
table is deferred. This module ships:

  * ``should_retry(outcome, attempts, retry_policy)``           — pure decision
  * ``compute_retry_delay_hours(outcome, attempts, retry_policy)`` — pure
  * ``record_call_completed(...)``                              — DB write
  * ``increment_metric_counter(...)``                           — DB write
  * ``process_call_outcome(...)``                               — composition

Trigger.dev or end-of-call-report handlers call ``process_call_outcome``
once per terminal call event.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.db import get_db_connection

logger = logging.getLogger(__name__)


_DEFAULT_RETRY_POLICY: dict[str, Any] = {
    "max_attempts": 3,
    "delay_hours": 4,
    "backoff_multiplier": 1.5,
}

# Outcomes that are terminal-success (no retry, advance the funnel).
_SUCCESS_OUTCOMES = {
    "transferred",
    "qualified",
    "qualified_transfer",
    "callback_requested",
    "voicemail_left",
}

# Outcomes that should be retried up to ``max_attempts`` per the policy.
_RETRY_OUTCOMES = {"no_answer", "busy", "error"}

# Outcomes that immediately mark the lead failed (no retry).
_TERMINAL_FAILURE_OUTCOMES = {"not_qualified"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Pure retry-decision helpers
# ---------------------------------------------------------------------------


def get_retry_policy(retry_policy: dict[str, Any] | None) -> dict[str, Any]:
    """Merge a partial retry_policy onto the default. Robust to None / empty."""
    out = dict(_DEFAULT_RETRY_POLICY)
    if retry_policy:
        for k, v in retry_policy.items():
            if v is not None:
                out[k] = v
    return out


def should_retry(
    outcome: str, attempts: int, retry_policy: dict[str, Any] | None = None
) -> bool:
    """True if the outcome is retriable AND we have attempts left."""
    if outcome not in _RETRY_OUTCOMES:
        return False
    policy = get_retry_policy(retry_policy)
    return attempts < int(policy["max_attempts"])


def compute_retry_delay_hours(
    outcome: str, attempts: int, retry_policy: dict[str, Any] | None = None
) -> float:
    """Return the hour-delay for the next retry. Mirrors OEX semantics:

      * ``no_answer``: flat ``delay_hours``
      * ``busy``: ``max(1, delay_hours / 2)``
      * ``error``: exponential backoff ``delay_hours * multiplier ** attempts``
    """
    policy = get_retry_policy(retry_policy)
    delay = float(policy["delay_hours"])
    if outcome == "busy":
        return max(1.0, delay / 2)
    if outcome == "error":
        multiplier = float(policy["backoff_multiplier"])
        return delay * (multiplier**attempts)
    return delay


def classify_outcome(outcome: str) -> str:
    """Bucket an outcome into one of: ``success``, ``retry``,
    ``terminal_failure``, ``unknown``. Pure helper — caller decides what to do.
    """
    if outcome in _SUCCESS_OUTCOMES:
        return "success"
    if outcome in _RETRY_OUTCOMES:
        return "retry"
    if outcome in _TERMINAL_FAILURE_OUTCOMES:
        return "terminal_failure"
    return "unknown"


# ---------------------------------------------------------------------------
# DB writes
# ---------------------------------------------------------------------------


async def record_call_completed(
    *,
    brand_id: UUID,
    call_id: str,
    outcome: str,
    duration_seconds: int,
    cost_cents: int,
) -> bool:
    """Mark the matching ``voice_campaign_active_calls`` row completed.

    Idempotent: returns False if the row was already completed (or didn't
    exist), True if this call actually transitioned it. Mirrors OEX's
    ``neq("status", "completed")`` guard.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_campaign_active_calls
                SET status = 'completed',
                    outcome = %s,
                    duration_seconds = %s,
                    cost_cents = %s,
                    ended_at = NOW(),
                    updated_at = NOW()
                WHERE brand_id = %s
                  AND call_id = %s
                  AND status <> 'completed'
                  AND deleted_at IS NULL
                RETURNING id
                """,
                (outcome, duration_seconds, cost_cents, str(brand_id), call_id),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row is not None


async def increment_metric_counter(
    *,
    brand_id: UUID,
    campaign_id: UUID,
    outcome: str,
    duration_seconds: int,
    cost_cents: int,
) -> None:
    """Upsert ``voice_campaign_metrics`` counters for an outcome."""
    outcome_field_map = {
        "transferred": "calls_transferred",
        "qualified": "calls_qualified",
        "qualified_transfer": "calls_transferred",
        "voicemail_left": "calls_voicemail",
        "no_answer": "calls_no_answer",
        "busy": "calls_busy",
        "error": "calls_error",
        "connected": "calls_connected",
    }
    counter_field = outcome_field_map.get(outcome)

    set_counter = (
        f", {counter_field} = voice_campaign_metrics.{counter_field} + 1"
        if counter_field
        else ""
    )
    insert_counter_col = f", {counter_field}" if counter_field else ""
    insert_counter_val = ", 1" if counter_field else ""

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO voice_campaign_metrics (
                    brand_id, campaign_id,
                    total_calls, total_duration_seconds, total_cost_cents
                    {insert_counter_col}
                )
                VALUES (%s, %s, 1, %s, %s {insert_counter_val})
                ON CONFLICT (campaign_id) DO UPDATE SET
                    total_calls = voice_campaign_metrics.total_calls + 1,
                    total_duration_seconds =
                        voice_campaign_metrics.total_duration_seconds
                        + EXCLUDED.total_duration_seconds,
                    total_cost_cents =
                        voice_campaign_metrics.total_cost_cents
                        + EXCLUDED.total_cost_cents,
                    updated_at = NOW()
                    {set_counter}
                """,
                (
                    str(brand_id), str(campaign_id),
                    duration_seconds, cost_cents,
                ),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


async def process_call_outcome(
    *,
    brand_id: UUID,
    campaign_id: UUID,
    call_id: str,
    outcome: str,
    duration_seconds: int = 0,
    cost_cents: int = 0,
) -> dict[str, Any]:
    """Process a completed campaign call.

    Per directive: this is the unit-of-work entrypoint that the end-of-call-
    report handler (Vapi webhook) calls when it sees campaign context. It
    does NOT mutate ``campaign_lead_progress`` or schedule retries — the
    Trigger.dev orchestrator owns scheduling. We return a dict describing
    what should happen next so the caller can decide.
    """
    transitioned = await record_call_completed(
        brand_id=brand_id,
        call_id=call_id,
        outcome=outcome,
        duration_seconds=duration_seconds,
        cost_cents=cost_cents,
    )
    if not transitioned:
        logger.info(
            "voice_campaign_outcome_duplicate_or_missing",
            extra={
                "brand_id": str(brand_id), "campaign_id": str(campaign_id),
                "call_id": call_id, "outcome": outcome,
            },
        )
        return {"transitioned": False, "classification": classify_outcome(outcome)}

    await increment_metric_counter(
        brand_id=brand_id,
        campaign_id=campaign_id,
        outcome=outcome,
        duration_seconds=duration_seconds,
        cost_cents=cost_cents,
    )

    classification = classify_outcome(outcome)
    logger.info(
        "voice_campaign_outcome_processed",
        extra={
            "brand_id": str(brand_id), "campaign_id": str(campaign_id),
            "call_id": call_id, "outcome": outcome,
            "classification": classification,
        },
    )
    return {"transitioned": True, "classification": classification}
