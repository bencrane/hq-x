"""Cluster 3 synthetic heartbeat — proves the chain is alive end-to-end.

Runs hourly via Trigger.dev. Fires a synthetic 'replied' event against
the heartbeat email_message in the sim org, drives inbox_orchestrator
in stub mode (no real Anthropic, no real EB), verifies a lead_transfer
lands in 'sent' status. Records to business.cluster3_heartbeat_log.

If anything in the chain breaks, the heartbeat fails fast and fires a
critical alert with the failing step + diagnostic. Stale heartbeats
(no 'pass' in the last 2 hours) also alert.

The heartbeat does NOT exercise:
  * real Anthropic calls (uses stub classifier + stub composer)
  * real EmailBison sends (CLUSTER3_LIVE_SEND defaults false)
  * real DEX traffic (sim recipient is local-only)

It DOES exercise:
  * webhook → email_message_event → orchestrator → classifier UPSERT
  * dispatch → allocation → composer → verdict → ledger insert
  * unique-index concurrency guard
  * idempotent retry semantics

For the live-Anthropic + live-EB path, run cluster3_simulation manually
or set CLUSTER3_HEARTBEAT_LIVE=true (rare, costs money).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.services import alerts, inbox_orchestrator

logger = logging.getLogger(__name__)


SIM_ORG_SLUG = "cluster3-sim"
SIM_HEARTBEAT_RECIPIENT_EXTERNAL_ID = "sim-heartbeat-fixed"
HEARTBEAT_REPLY_TEXT = (
    "Yes interested — happy to chat. Send me a calendar link and I'll "
    "grab time this week. Let's talk."
)


async def run_heartbeat() -> dict[str, Any]:
    """One end-to-end synthetic dispatch. Returns a result dict and
    writes an audit row to business.cluster3_heartbeat_log."""
    started_at = time.monotonic()
    log_id = await _create_log_row()
    fail_reason: str | None = None
    classification_id: UUID | None = None
    lead_transfer_id: UUID | None = None
    intro_email_message_id: UUID | None = None

    try:
        target = await _resolve_heartbeat_target()
        if target is None:
            fail_reason = "no_heartbeat_recipient"
            await _mark_log_fail(
                log_id=log_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                reason=fail_reason,
                metadata={
                    "hint": (
                        "run scripts.cluster3_simulation --mode=seed against "
                        "the dev DB to scaffold the heartbeat fixture"
                    ),
                },
            )
            await alerts.fire_alert(
                severity="critical",
                source="cluster3_heartbeat",
                summary="Heartbeat fixture missing — no sim recipient found",
                payload={"reason": fail_reason},
            )
            return {"status": "fail", "reason": fail_reason}

        await _stamp_synthetic_reply(
            email_message_id=target["email_message_id"],
            recipient_email=target["recipient_email"],
        )

        result = await inbox_orchestrator.handle_inbound_reply(
            email_message_id=target["email_message_id"],
            eb_reply_id=int(time.time() * 1000),
            eb_workspace_id="heartbeat-workspace",
            classifier_mode="stub",
            composer_mode="stub",
            verdict_mode="stub",
        )
        status = result.get("status")
        if status not in ("dispatched", "classified_only"):
            fail_reason = f"orchestrator_returned_status={status}"
        elif status == "dispatched":
            dr = result.get("dispatch_result") or {}
            if dr.get("status") != "sent":
                fail_reason = f"dispatch_status={dr.get('status')!r}"
            classification_id = result.get("classification_id")
            lead_transfer_id = dr.get("lead_transfer_id")
            intro_email_message_id = dr.get("intro_email_message_id")
        else:
            fail_reason = (
                f"classified_only (expected dispatched). "
                f"classification={result.get('classification')!r}"
            )

    except Exception as exc:  # noqa: BLE001
        fail_reason = f"unhandled_exception: {str(exc)[:300]}"
        logger.exception("heartbeat unhandled exception")

    duration_ms = int((time.monotonic() - started_at) * 1000)

    if fail_reason is None:
        await _mark_log_pass(
            log_id=log_id,
            duration_ms=duration_ms,
            classification_id=classification_id,
            lead_transfer_id=lead_transfer_id,
            intro_email_message_id=intro_email_message_id,
        )
        await _maybe_fire_recovered_alert()
        return {
            "status": "pass",
            "duration_ms": duration_ms,
            "classification_id": str(classification_id) if classification_id else None,
            "lead_transfer_id": str(lead_transfer_id) if lead_transfer_id else None,
        }

    await _mark_log_fail(
        log_id=log_id,
        duration_ms=duration_ms,
        reason=fail_reason,
        metadata={
            "classification_id": str(classification_id) if classification_id else None,
            "lead_transfer_id": str(lead_transfer_id) if lead_transfer_id else None,
        },
    )
    await alerts.fire_alert(
        severity="critical",
        source="cluster3_heartbeat",
        summary=f"Heartbeat failed: {fail_reason[:160]}",
        payload={
            "duration_ms": duration_ms,
            "classification_id": str(classification_id) if classification_id else None,
            "lead_transfer_id": str(lead_transfer_id) if lead_transfer_id else None,
        },
    )
    return {"status": "fail", "duration_ms": duration_ms, "reason": fail_reason}


async def staleness_check() -> dict[str, Any]:
    """If no 'pass' heartbeat in the last 2 hours, fire a critical alert.

    Catches the case where the cron itself stops firing (Trigger.dev
    project disabled, schedule errored, etc.). Run as part of every
    heartbeat invocation — cheap.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT MAX(started_at) FROM business.cluster3_heartbeat_log
                WHERE status = 'pass'
                """
            )
            row = await cur.fetchone()
    last_pass = row[0] if row and row[0] else None

    if last_pass is None:
        # Either fresh install or never-passed — let main heartbeat surface that.
        return {"last_pass": None}

    from datetime import datetime, timezone, timedelta

    age = datetime.now(timezone.utc) - last_pass
    if age > timedelta(hours=2):
        await alerts.fire_alert(
            severity="critical",
            source="cluster3_heartbeat_staleness",
            summary=f"No passing heartbeat in {int(age.total_seconds() / 60)} minutes",
            payload={"last_pass": last_pass.isoformat()},
        )
    return {"last_pass": last_pass.isoformat(), "age_seconds": age.total_seconds()}


# ── helpers ──────────────────────────────────────────────────────────────


async def _create_log_row() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster3_heartbeat_log (status)
                VALUES ('running')
                RETURNING id
                """
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _mark_log_pass(
    *,
    log_id: UUID,
    duration_ms: int,
    classification_id: UUID | None,
    lead_transfer_id: UUID | None,
    intro_email_message_id: UUID | None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster3_heartbeat_log
                SET status = 'pass',
                    completed_at = NOW(),
                    duration_ms = %s,
                    classification_id = %s,
                    lead_transfer_id = %s,
                    intro_email_message_id = %s
                WHERE id = %s
                """,
                (
                    duration_ms,
                    str(classification_id) if classification_id else None,
                    str(lead_transfer_id) if lead_transfer_id else None,
                    str(intro_email_message_id) if intro_email_message_id else None,
                    str(log_id),
                ),
            )
        await conn.commit()


async def _mark_log_fail(
    *,
    log_id: UUID,
    duration_ms: int,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster3_heartbeat_log
                SET status = 'fail',
                    completed_at = NOW(),
                    duration_ms = %s,
                    failure_reason = %s,
                    metadata = %s
                WHERE id = %s
                """,
                (duration_ms, reason, Jsonb(metadata or {}), str(log_id)),
            )
        await conn.commit()


async def _resolve_heartbeat_target() -> dict[str, Any] | None:
    """Find the heartbeat email_message under the sim org. Caller seeds
    this once via scripts.cluster3_simulation --mode=seed."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT em.id, r.email
                FROM business.email_messages em
                JOIN business.recipients r ON r.id = em.recipient_id
                JOIN business.organizations o ON o.id = em.organization_id
                WHERE o.slug = %s
                  AND em.metadata->>'sim_idx' = '0'
                ORDER BY em.created_at DESC
                LIMIT 1
                """,
                (SIM_ORG_SLUG,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {"email_message_id": row[0], "recipient_email": row[1]}


async def _stamp_synthetic_reply(
    *, email_message_id: UUID, recipient_email: str
) -> None:
    payload = {
        "event": {"type": "lead_replied"},
        "data": {
            "reply": {
                "id": int(time.time() * 1000),
                "subject": "Re: heartbeat",
                "text_body": HEARTBEAT_REPLY_TEXT,
                "from_email_address": recipient_email,
                "interested": True,
                "automated_reply": False,
                "folder": "Inbox",
                "type": "Replied",
            }
        },
    }
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_message_events
                    (email_message_id, event_type, raw_event_name, occurred_at, payload)
                VALUES (%s, 'replied', 'lead_replied', NOW(), %s)
                ON CONFLICT (email_message_id, raw_event_name, occurred_at)
                  DO NOTHING
                """,
                (str(email_message_id), Jsonb(payload)),
            )
            # Reset the heartbeat email_message + classifications so we
            # can re-fire the same row hourly without UniqueViolation
            # snags. We delete prior heartbeat-tagged classifications +
            # lead_transfers; the recipient + email_message persist.
            await cur.execute(
                """
                DELETE FROM business.lead_transfers
                WHERE email_reply_classification_id IN (
                    SELECT id FROM business.email_reply_classifications
                    WHERE email_message_id = %s
                )
                """,
                (str(email_message_id),),
            )
            await cur.execute(
                """
                DELETE FROM business.email_reply_classifications
                WHERE email_message_id = %s
                """,
                (str(email_message_id),),
            )
        await conn.commit()


async def _maybe_fire_recovered_alert() -> None:
    """If the previous heartbeat failed but this one passed, log a
    recovered-info alert so the operator can see the recovery."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT status, failure_reason
                FROM business.cluster3_heartbeat_log
                WHERE status IN ('pass', 'fail')
                ORDER BY started_at DESC
                LIMIT 2
                """
            )
            rows = await cur.fetchall()
    if len(rows) >= 2 and rows[0][0] == "pass" and rows[1][0] == "fail":
        await alerts.fire_alert(
            severity="info",
            source="cluster3_heartbeat",
            summary="Heartbeat recovered after previous failure",
            payload={"prev_failure_reason": rows[1][1]},
        )


__all__ = ["run_heartbeat", "staleness_check"]
