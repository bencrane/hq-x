"""Cluster 3 reconciliation sweep — recover dropped EmailBison webhooks.

EmailBison's webhook delivery is not idempotent and is occasionally
dropped. If a reply lands in EB but the corresponding webhook event
never reaches hq-x, our email_messages.replied_at stays NULL and we
never run inbox_orchestrator.

This sweep:
  1. Lists active Leg-2 channel_campaigns (initiative.metadata.leg=2,
     channel_campaign.provider='emailbison',
     external_provider_id present).
  2. For each, polls EB GET /api/replies?folder=Inbox&campaign_id=<id>
     covering the last `lookback_hours` window.
  3. Cross-references each EB reply against business.email_messages
     by (eb_workspace_id, eb_scheduled_email_id) tuple.
  4. For each missed reply: build a synthetic email_message_event row
     (event_type='replied', payload={data:{reply:{...the EB reply...}}})
     so the orchestrator can pick it up on the next pass.
  5. Triggers inbox_orchestrator.handle_inbound_reply directly for each
     backfilled message.

Records summary to business.cluster3_reconciliation_log. Alerts on any
non-zero backfill (operator should know a webhook was dropped).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError
from app.services import alerts, inbox_orchestrator

logger = logging.getLogger(__name__)


def _api_key() -> str | None:
    secret = getattr(settings, "EMAILBISON_API_KEY", None)
    if not secret:
        return None
    return (
        secret.get_secret_value()
        if hasattr(secret, "get_secret_value")
        else str(secret)
    )


async def sweep_reconciliation(
    *, lookback_hours: int = 48, per_campaign_limit: int = 200
) -> dict[str, Any]:
    started_at = time.monotonic()
    log_id = await _create_log_row()

    api_key = _api_key()
    if not api_key:
        await _mark_log_fail(
            log_id=log_id,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            reason="EMAILBISON_API_KEY_not_configured",
        )
        return {"status": "skipped", "reason": "EMAILBISON_API_KEY not configured"}

    campaigns = await _list_active_leg2_campaigns()
    if not campaigns:
        await _mark_log_pass(
            log_id=log_id,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            scanned=0,
            already=0,
            backfilled=0,
        )
        return {
            "status": "pass",
            "scanned": 0,
            "backfilled": 0,
            "campaigns": 0,
        }

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    scanned = 0
    already = 0
    backfilled = 0
    backfilled_ids: list[str] = []
    eb_errors: list[dict[str, Any]] = []

    for camp in campaigns:
        try:
            replies = _list_replies_for_campaign(
                api_key=api_key,
                eb_campaign_id=camp["external_provider_id"],
                limit=per_campaign_limit,
            )
        except EmailBisonProviderError as exc:
            eb_errors.append(
                {"campaign_id": camp["external_provider_id"], "error": str(exc)[:300]}
            )
            continue

        for reply in replies:
            scanned += 1
            received_at_str = reply.get("date_received") or reply.get("created_at")
            if received_at_str:
                try:
                    received_at = datetime.fromisoformat(
                        received_at_str.replace("Z", "+00:00")
                    )
                    if received_at < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass

            eb_scheduled_id = _coerce_int(reply.get("scheduled_email_id"))
            if eb_scheduled_id is None:
                # Untracked reply, can't link to our email_messages.
                continue

            existing = await _find_email_message(
                eb_workspace_id=camp["eb_workspace_id"],
                eb_scheduled_email_id=eb_scheduled_id,
            )
            if not existing:
                continue

            already_processed = await _has_replied_event(existing["id"])
            if already_processed:
                already += 1
                continue

            await _backfill_event(
                email_message_id=existing["id"],
                reply=reply,
                eb_campaign_id=camp["external_provider_id"],
            )
            backfilled += 1
            backfilled_ids.append(str(existing["id"]))

            try:
                await inbox_orchestrator.handle_inbound_reply(
                    email_message_id=existing["id"],
                    eb_reply_id=_coerce_int(reply.get("id")),
                    eb_workspace_id=camp["eb_workspace_id"],
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "reconciliation orchestrator dispatch failed for em=%s",
                    existing["id"],
                )

    duration_ms = int((time.monotonic() - started_at) * 1000)
    await _mark_log_pass(
        log_id=log_id,
        duration_ms=duration_ms,
        scanned=scanned,
        already=already,
        backfilled=backfilled,
        metadata={
            "backfilled_ids": backfilled_ids,
            "eb_errors": eb_errors,
            "campaigns": len(campaigns),
        },
    )

    if backfilled > 0:
        await alerts.fire_alert(
            severity="warning",
            source="cluster3_reconciliation",
            summary=(
                f"Reconciliation backfilled {backfilled} dropped reply"
                f"{'s' if backfilled != 1 else ''}"
            ),
            payload={
                "backfilled": backfilled,
                "scanned": scanned,
                "already_processed": already,
                "duration_ms": duration_ms,
                "backfilled_ids": backfilled_ids[:20],
            },
        )

    if eb_errors:
        await alerts.fire_alert(
            severity="warning",
            source="cluster3_reconciliation",
            summary=f"EB reply listing failed for {len(eb_errors)} campaigns",
            payload={"errors": eb_errors[:5]},
        )

    return {
        "status": "pass",
        "scanned": scanned,
        "already": already,
        "backfilled": backfilled,
        "campaigns": len(campaigns),
        "duration_ms": duration_ms,
        "errors": eb_errors,
    }


# ── helpers ──────────────────────────────────────────────────────────────


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


async def _list_active_leg2_campaigns() -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT cc.id, cc.external_provider_id, cc.metadata
                FROM business.channel_campaigns cc
                JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                WHERE cc.provider = 'emailbison'
                  AND cc.external_provider_id IS NOT NULL
                  AND init.kind = 'partner_demand'
                  AND (init.metadata->>'leg')::int = 2
                  AND init.status NOT IN ('cancelled', 'completed')
                """
            )
            rows = await cur.fetchall()
    return [
        {
            "channel_campaign_id": r[0],
            "external_provider_id": r[1],
            "eb_workspace_id": (r[2] or {}).get("eb_workspace_id"),
        }
        for r in rows or []
    ]


def _list_replies_for_campaign(
    *, api_key: str, eb_campaign_id: str, limit: int
) -> list[dict[str, Any]]:
    """Direct EB call. Sync httpx through the existing _request_json helper."""
    response = eb_client._request_json(
        api_key=api_key,
        method="GET",
        path="/api/replies",
        params={"campaign_id": eb_campaign_id, "limit": limit, "folder": "Inbox"},
    )
    if isinstance(response, dict):
        data = response.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            inner = data.get("data") or data.get("items")
            if isinstance(inner, list):
                return inner
        if isinstance(response.get("items"), list):
            return response["items"]
    if isinstance(response, list):
        return response
    return []


async def _find_email_message(
    *, eb_workspace_id: str | None, eb_scheduled_email_id: int
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status, replied_at
                FROM business.email_messages
                WHERE eb_scheduled_email_id = %s
                  AND (eb_workspace_id IS NOT DISTINCT FROM %s)
                LIMIT 1
                """,
                (eb_scheduled_email_id, eb_workspace_id),
            )
            row = await cur.fetchone()
    return None if row is None else {
        "id": row[0], "status": row[1], "replied_at": row[2]
    }


async def _has_replied_event(email_message_id: UUID) -> bool:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1 FROM business.email_message_events
                WHERE email_message_id = %s
                  AND event_type IN ('replied', 'interested')
                LIMIT 1
                """,
                (str(email_message_id),),
            )
            row = await cur.fetchone()
    return row is not None


async def _backfill_event(
    *, email_message_id: UUID, reply: dict[str, Any], eb_campaign_id: str
) -> None:
    payload = {
        "event": {"type": "lead_replied", "_source": "reconciliation_backfill"},
        "data": {"reply": reply, "campaign": {"id": eb_campaign_id}},
    }
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_message_events
                    (email_message_id, event_type, raw_event_name, occurred_at, payload)
                VALUES (%s, 'replied', 'lead_replied_backfill', NOW(), %s)
                ON CONFLICT (email_message_id, raw_event_name, occurred_at)
                  DO NOTHING
                """,
                (str(email_message_id), Jsonb(payload)),
            )
            await cur.execute(
                """
                UPDATE business.email_messages
                SET status = 'replied',
                    replied_at = COALESCE(replied_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(email_message_id),),
            )
        await conn.commit()


async def _create_log_row() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster3_reconciliation_log (status)
                VALUES ('running') RETURNING id
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
    scanned: int,
    already: int,
    backfilled: int,
    metadata: dict[str, Any] | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster3_reconciliation_log
                SET status = 'pass', completed_at = NOW(),
                    duration_ms = %s,
                    eb_replies_scanned = %s,
                    eb_replies_already_processed = %s,
                    eb_replies_backfilled = %s,
                    metadata = %s
                WHERE id = %s
                """,
                (duration_ms, scanned, already, backfilled, Jsonb(metadata or {}), str(log_id)),
            )
        await conn.commit()


async def _mark_log_fail(
    *, log_id: UUID, duration_ms: int, reason: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster3_reconciliation_log
                SET status = 'fail', completed_at = NOW(),
                    duration_ms = %s, failure_reason = %s
                WHERE id = %s
                """,
                (duration_ms, reason, str(log_id)),
            )
        await conn.commit()


__all__ = ["sweep_reconciliation"]
