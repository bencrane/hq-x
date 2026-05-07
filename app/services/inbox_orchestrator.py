"""Inbox orchestrator — entry point from EmailBison reply webhook.

Cluster 3's front door. The EB webhook processor calls in here whenever
an inbound email_messages row's status flips to ``replied`` (or when the
EB-native ``interested`` event fires). The orchestrator:

  1. Fetches the reply body (from EB if not already in the
     ``email_message_events`` audit row, falling back to the captured
     metadata snapshot).
  2. Calls ``reply_classifier`` to label the reply.
  3. UPSERTs ``business.email_reply_classifications``.
  4. Routes by initiative kind / leg:

         partner_demand + leg=2 → cluster3_dispatch.dispatch_for_classification
         self_prospecting       → record only (Cluster 1 reply behavior is
                                  operator-codified later, not here)
         partner_demand + leg=3 → record only (Leg-3 reply ≈ recipient
                                  responding to the intro itself; out of
                                  scope for the v1 build)
         other                  → record only

The orchestrator is fire-and-forget from the webhook processor's POV: it
returns a small status dict but never raises out to the projector. All
real failures stamp ``email_reply_classifications.evidence`` with a
diagnostic and let the operator inspect.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.providers.emailbison import client as eb_client
from app.providers.emailbison.client import EmailBisonProviderError
from app.services import alerts, cluster1_auto_reply, cluster3_dispatch, reply_classifier

logger = logging.getLogger(__name__)


async def handle_inbound_reply(
    *,
    email_message_id: UUID,
    eb_reply_id: int | None = None,
    eb_workspace_id: str | None = None,
    classifier_mode: reply_classifier.ClassifierMode = "auto",
    composer_mode: str | None = None,
    verdict_mode: str | None = None,
) -> dict[str, Any]:
    """Top-level entry. Idempotent on email_message_id.

    Returns:
        {
            status: "dispatched" | "classified_only" | "skipped" | "error",
            classification_id: str | None,
            classification: str | None,
            dispatch_result: dict | None,
            ...
        }
    """
    msg = await _fetch_email_message_with_init(email_message_id)
    if msg is None:
        return {"status": "skipped", "reason": "no email_message"}

    # Fetch reply text — first from email_message_events audit, else hit EB.
    reply_text, reply_subject, reply_from = await _resolve_reply_content(
        email_message_id=email_message_id,
        eb_reply_id=eb_reply_id,
    )

    # Classify.
    cls = await reply_classifier.classify(
        reply_text=reply_text or "",
        reply_subject=reply_subject,
        reply_from_email=reply_from,
        mode=classifier_mode,
    )

    classification_id = await _upsert_classification(
        email_message_id=email_message_id,
        classification=cls["classification"],
        classified_by=cls["classified_by"],
        evidence={
            **(cls.get("evidence") or {}),
            "reply_text_len": len(reply_text or ""),
            "reply_subject": reply_subject,
            "reply_from": reply_from,
            "eb_reply_id": eb_reply_id,
            "eb_workspace_id": eb_workspace_id,
        },
    )

    init_kind = msg.get("init_kind")
    init_leg = msg.get("init_leg")

    # Branch on initiative shape:
    #   * partner_demand + leg=2  → Cluster 3 (intro to demand-side partner)
    #   * self_prospecting        → Cluster 1 auto-reply (book the call)
    #   * everything else         → classify-only
    if cls["classification"] != "positive":
        return _classified_only(
            classification_id=classification_id,
            classification=cls["classification"],
            init_kind=init_kind,
            init_leg=init_leg,
        )

    if init_kind == "partner_demand" and init_leg == 2:
        return await _dispatch_cluster3(
            classification_id=classification_id,
            classification=cls["classification"],
            email_message_id=email_message_id,
            composer_mode=composer_mode,
            verdict_mode=verdict_mode,
        )

    if init_kind == "self_prospecting":
        return await _dispatch_cluster1(
            classification_id=classification_id,
            classification=cls["classification"],
            email_message_id=email_message_id,
            composer_mode=composer_mode,
            verdict_mode=verdict_mode,
        )

    return _classified_only(
        classification_id=classification_id,
        classification=cls["classification"],
        init_kind=init_kind,
        init_leg=init_leg,
    )


def _classified_only(
    *,
    classification_id: UUID,
    classification: str,
    init_kind: str | None,
    init_leg: int | None,
) -> dict[str, Any]:
    return {
        "status": "classified_only",
        "classification_id": str(classification_id),
        "classification": classification,
        "init_kind": init_kind,
        "init_leg": init_leg,
        "reason": _why_not_dispatched(
            classification=classification,
            init_kind=init_kind,
            init_leg=init_leg,
        ),
    }


async def _dispatch_cluster3(
    *,
    classification_id: UUID,
    classification: str,
    email_message_id: UUID,
    composer_mode: str | None,
    verdict_mode: str | None,
) -> dict[str, Any]:
    try:
        result = await cluster3_dispatch.dispatch_for_classification(
            classification_id=classification_id,
            composer_mode=composer_mode or "auto",  # type: ignore[arg-type]
            verdict_mode=verdict_mode,
        )
    except cluster3_dispatch.Cluster3DispatchError as exc:
        logger.exception(
            "cluster3_dispatch failed for classification=%s", classification_id
        )
        await _stamp_dispatch_error(classification_id, str(exc)[:500])
        await alerts.fire_alert(
            severity="critical",
            source="inbox_orchestrator",
            summary=f"Cluster 3 dispatch failed: {str(exc)[:160]}",
            payload={
                "classification_id": str(classification_id),
                "email_message_id": str(email_message_id),
                "error": str(exc)[:500],
            },
        )
        return {
            "status": "error",
            "classification_id": str(classification_id),
            "classification": classification,
            "error": str(exc)[:500],
            "cluster": "cluster_3",
        }
    except Exception as exc:
        logger.exception("cluster3_dispatch crashed")
        await _stamp_dispatch_error(classification_id, str(exc)[:500])
        await alerts.fire_alert(
            severity="critical",
            source="inbox_orchestrator",
            summary=f"Cluster 3 dispatch CRASHED: {str(exc)[:160]}",
            payload={
                "classification_id": str(classification_id),
                "email_message_id": str(email_message_id),
                "error": str(exc)[:500],
                "exception_type": type(exc).__name__,
            },
        )
        return {
            "status": "error",
            "classification_id": str(classification_id),
            "classification": classification,
            "error": str(exc)[:500],
            "cluster": "cluster_3",
        }

    return {
        "status": "dispatched",
        "classification_id": str(classification_id),
        "classification": classification,
        "dispatch_result": result,
        "cluster": "cluster_3",
    }


async def _dispatch_cluster1(
    *,
    classification_id: UUID,
    classification: str,
    email_message_id: UUID,
    composer_mode: str | None,
    verdict_mode: str | None,
) -> dict[str, Any]:
    try:
        result = await cluster1_auto_reply.dispatch_for_classification(
            classification_id=classification_id,
            composer_mode=composer_mode or "auto",  # type: ignore[arg-type]
            verdict_mode=verdict_mode,
        )
    except cluster1_auto_reply.Cluster1AutoReplyError as exc:
        logger.exception(
            "cluster1_auto_reply failed for classification=%s", classification_id
        )
        await _stamp_dispatch_error(classification_id, str(exc)[:500])
        await alerts.fire_alert(
            severity="critical",
            source="inbox_orchestrator",
            summary=f"Cluster 1 auto-reply failed: {str(exc)[:160]}",
            payload={
                "classification_id": str(classification_id),
                "email_message_id": str(email_message_id),
                "error": str(exc)[:500],
            },
        )
        return {
            "status": "error",
            "classification_id": str(classification_id),
            "classification": classification,
            "error": str(exc)[:500],
            "cluster": "cluster_1",
        }
    except Exception as exc:
        logger.exception("cluster1_auto_reply crashed")
        await _stamp_dispatch_error(classification_id, str(exc)[:500])
        await alerts.fire_alert(
            severity="critical",
            source="inbox_orchestrator",
            summary=f"Cluster 1 auto-reply CRASHED: {str(exc)[:160]}",
            payload={
                "classification_id": str(classification_id),
                "email_message_id": str(email_message_id),
                "error": str(exc)[:500],
                "exception_type": type(exc).__name__,
            },
        )
        return {
            "status": "error",
            "classification_id": str(classification_id),
            "classification": classification,
            "error": str(exc)[:500],
            "cluster": "cluster_1",
        }
    return {
        "status": "dispatched",
        "classification_id": str(classification_id),
        "classification": classification,
        "dispatch_result": result,
        "cluster": "cluster_1",
    }


def _why_not_dispatched(
    *, classification: str, init_kind: str | None, init_leg: int | None
) -> str:
    if classification != "positive":
        return f"classification={classification}"
    if init_kind != "partner_demand":
        return f"init_kind={init_kind}"
    if init_leg != 2:
        return f"init_leg={init_leg}"
    return "unknown"


async def _fetch_email_message_with_init(
    email_message_id: UUID,
) -> dict[str, Any] | None:
    """Pull the email_message + which initiative it belongs to (and which leg)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT em.id, em.organization_id, em.brand_id, em.recipient_id,
                       em.channel_campaign_step_id, em.eb_lead_id,
                       em.subject_snapshot, em.body_snapshot, em.metadata,
                       cc.initiative_id,
                       init.kind  AS init_kind,
                       init.metadata AS init_metadata
                FROM business.email_messages em
                JOIN business.channel_campaign_steps step
                  ON step.id = em.channel_campaign_step_id
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                LEFT JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                WHERE em.id = %s
                """,
                (str(email_message_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    init_metadata = row[11] or {}
    leg = init_metadata.get("leg") if isinstance(init_metadata, dict) else None
    return {
        "id": row[0],
        "organization_id": row[1],
        "brand_id": row[2],
        "recipient_id": row[3],
        "channel_campaign_step_id": row[4],
        "eb_lead_id": row[5],
        "subject_snapshot": row[6],
        "body_snapshot": row[7],
        "metadata": row[8] or {},
        "initiative_id": row[9],
        "init_kind": row[10],
        "init_leg": leg,
    }


async def _resolve_reply_content(
    *, email_message_id: UUID, eb_reply_id: int | None
) -> tuple[str | None, str | None, str | None]:
    """Best-effort resolution of (reply_text, reply_subject, reply_from).

    Order:
      1. Last `replied`/`interested` event row in email_message_events —
         payload may include data.reply with text_body / subject / from.
      2. If we have eb_reply_id and a live API key, fetch
         GET /api/replies/{id} for the canonical body.
      3. Whatever the email_messages.metadata snapshot has.
    """
    text, subject, from_email = await _scan_email_events(email_message_id)
    if text:
        return text, subject, from_email

    if eb_reply_id is not None:
        live = await _fetch_eb_reply_live(eb_reply_id)
        if live:
            return live

    # Final fallback — empty.
    return text, subject, from_email


async def _scan_email_events(
    email_message_id: UUID,
) -> tuple[str | None, str | None, str | None]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT payload
                FROM business.email_message_events
                WHERE email_message_id = %s
                  AND event_type IN ('replied', 'interested', 'untracked_reply')
                ORDER BY occurred_at DESC
                LIMIT 5
                """,
                (str(email_message_id),),
            )
            rows = await cur.fetchall()
    for (payload,) in rows or []:
        if not isinstance(payload, dict):
            continue
        data_block = payload.get("data") or {}
        reply = data_block.get("reply") if isinstance(data_block, dict) else None
        if not isinstance(reply, dict):
            continue
        text = (
            reply.get("text_body")
            or reply.get("plain_body")
            or reply.get("body")
            or reply.get("html_body")
        )
        if text:
            return (
                str(text),
                reply.get("subject"),
                reply.get("from_email_address") or reply.get("from"),
            )
    return None, None, None


async def _fetch_eb_reply_live(eb_reply_id: int) -> tuple[str, str | None, str | None] | None:
    """Hit GET /api/replies/{id} with the API key — returns None on any failure."""
    from app.config import settings

    secret = getattr(settings, "EMAILBISON_API_KEY", None)
    if not secret:
        return None
    api_key = (
        secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
    )

    try:
        response = eb_client._request_json(
            api_key=api_key,
            method="GET",
            path=f"/api/replies/{eb_reply_id}",
        )
    except EmailBisonProviderError as exc:
        logger.warning("eb fetch_reply %s failed: %s", eb_reply_id, exc)
        return None

    if not isinstance(response, dict):
        return None
    body = response.get("text_body") or response.get("html_body") or response.get("body")
    subject = response.get("subject")
    from_email = response.get("from_email_address") or response.get("from")
    if isinstance(from_email, dict):
        from_email = from_email.get("email") or from_email.get("name")
    if not body:
        # Some EB shapes nest under data
        data = response.get("data")
        if isinstance(data, dict):
            body = data.get("text_body") or data.get("html_body") or data.get("body")
            subject = subject or data.get("subject")
            from_email = from_email or data.get("from_email_address")
    if not body:
        return None
    return str(body), subject, from_email if isinstance(from_email, str) else None


async def _upsert_classification(
    *,
    email_message_id: UUID,
    classification: str,
    classified_by: str,
    evidence: dict[str, Any],
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_reply_classifications
                    (email_message_id, classification, classified_by,
                     classified_at, evidence)
                VALUES (%s, %s, %s, NOW(), %s)
                ON CONFLICT (email_message_id) DO UPDATE
                  SET classification = EXCLUDED.classification,
                      classified_by  = EXCLUDED.classified_by,
                      classified_at  = NOW(),
                      evidence       = EXCLUDED.evidence,
                      updated_at     = NOW()
                RETURNING id
                """,
                (
                    str(email_message_id),
                    classification,
                    classified_by,
                    Jsonb(evidence),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _stamp_dispatch_error(
    classification_id: UUID, error_message: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.email_reply_classifications
                SET evidence = evidence || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    Jsonb({"dispatch_error": error_message}),
                    str(classification_id),
                ),
            )
        await conn.commit()


__all__ = ["handle_inbound_reply"]
