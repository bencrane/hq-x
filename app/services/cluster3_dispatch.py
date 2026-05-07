"""Cluster 3 dispatch — allocation + intro composition + send + ledger.

Entry point: ``dispatch_for_classification(classification_id)``. Called
when a Leg-2 reply has been classified positive. Walks:

    1. Resolve the classification → email_message → channel_campaign_step
       → Leg-2 channel_campaign → Leg-2 initiative → partner +
       partner_contract + audience.
    2. Confirm we're operating on a Leg-2 (metadata.leg=2). If not,
       no-op (Cluster 3 only fires from Leg-2 replies; other initiative
       types are surfaced to the operator inbox view, future work).
    3. Look up the proposal that funded this contract — gives us the
       paid transfer count + per-transfer price. Allocation cap.
    4. Count delivered transfers in lead_transfers for
       (partner_id, audience_id, status='sent'). If at-or-over cap,
       insert a deferred_capped lead_transfer row and stop.
    5. Resolve the Leg-3 initiative (parent_initiative_id=Leg-2.id),
       its single channel_campaign_step (the intro template), and
       compose the intro via ``intro_composer``.
    6. INSERT a lead_transfers row in 'queued' status. Use the unique
       index on email_reply_classification_id WHERE status IN
       ('queued','sent') to gate against double-spend on concurrent
       webhook replays.
    7. Build an outbound email_messages row in 'pending' status (Leg-3
       step, render path) and call ``eb_send.send_intro``.
    8. On send success: stamp email_messages.status='sent',
       lead_transfers.status='sent' / sent_at, classification.intro_fired_at,
       classification.intro_email_message_id. On failure: stamp
       lead_transfers.status='failed' / failure_reason, surface a
       partial-result envelope, do NOT mark intro_fired_at (operator can
       retry).

The function is idempotent on the classification_id key thanks to the
unique-index. A retry that arrives while a previous attempt is in 'sent'
status will UniqueViolation and short-circuit with status='already_sent'.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg import errors as psycopg_errors
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.services import alerts, eb_send, intro_composer, intro_verdict

logger = logging.getLogger(__name__)


class Cluster3DispatchError(Exception):
    pass


class Cluster3PreconditionError(Cluster3DispatchError):
    pass


# ── Public entry point ────────────────────────────────────────────────────


async def dispatch_for_classification(
    *,
    classification_id: UUID,
    composer_mode: intro_composer.ComposerMode = "auto",
    verdict_mode: str | None = None,
) -> dict[str, Any]:
    """Drive the Cluster 3 flow for one positive classification.

    Returns a result envelope suitable for logging / API response:

        {status, classification_id, lead_transfer_id, intro_email_message_id,
         allocation_snapshot, send_mode, ...}

    ``status`` ∈ {'sent', 'deferred_capped', 'already_sent', 'failed',
                  'skipped_not_leg2', 'skipped_not_positive'}
    """
    ctx = await _resolve_dispatch_context(classification_id)
    if ctx is None:
        raise Cluster3PreconditionError(
            f"no classification row for id={classification_id}"
        )

    if ctx["classification"] != "positive":
        return {
            "status": "skipped_not_positive",
            "classification_id": str(classification_id),
            "reason": ctx["classification"],
        }

    if ctx["leg"] != 2:
        return {
            "status": "skipped_not_leg2",
            "classification_id": str(classification_id),
            "reason": f"reply on initiative leg={ctx['leg']}",
        }

    if ctx["intro_fired_at"] is not None:
        return {
            "status": "already_sent",
            "classification_id": str(classification_id),
            "intro_email_message_id": str(ctx["intro_email_message_id"])
            if ctx["intro_email_message_id"]
            else None,
        }

    # Resolve Leg-3 + allocation context.
    leg3 = await _resolve_leg3(leg2_initiative_id=ctx["leg2_initiative_id"])
    if leg3 is None:
        raise Cluster3PreconditionError(
            f"leg-2 initiative {ctx['leg2_initiative_id']} has no Leg-3 child"
        )

    cap = await _resolve_allocation_cap(
        partner_id=ctx["partner_id"],
        partner_contract_id=ctx["partner_contract_id"],
        data_engine_audience_id=ctx["data_engine_audience_id"],
    )
    delivered = await _count_delivered_transfers(
        partner_id=ctx["partner_id"],
        partner_contract_id=ctx["partner_contract_id"],
        data_engine_audience_id=ctx["data_engine_audience_id"],
    )
    allocation_snapshot = {
        "paid_transfer_count": cap["paid_transfer_count"],
        "delivered_count": delivered,
        "remaining": (
            cap["paid_transfer_count"] - delivered
            if cap["paid_transfer_count"] is not None
            else None
        ),
        "per_transfer_price_cents": cap.get("per_transfer_price_cents"),
        "amount_cents": cap.get("amount_cents"),
        "computed_via": cap.get("computed_via"),
    }

    if cap["paid_transfer_count"] is not None and delivered >= cap["paid_transfer_count"]:
        # Park as deferred_capped for operator visibility.
        lt_id = await _insert_lead_transfer(
            ctx=ctx,
            leg3_initiative_id=leg3["leg3_initiative_id"],
            allocation_snapshot=allocation_snapshot,
            status="deferred_capped",
            failure_reason=(
                f"at cap: delivered={delivered} >= paid={cap['paid_transfer_count']}"
            ),
        )
        return {
            "status": "deferred_capped",
            "classification_id": str(classification_id),
            "lead_transfer_id": str(lt_id) if lt_id else None,
            "allocation_snapshot": allocation_snapshot,
        }

    # Compose intro.
    bundle = await _gather_compose_bundle(ctx=ctx, leg3=leg3)
    composed = await intro_composer.compose(
        recipient=bundle["recipient"],
        partner=bundle["partner"],
        partner_research_md=bundle["partner_research_md"],
        audience_context_md=bundle["audience_context_md"],
        recipient_gestalt_md=bundle["recipient_gestalt_md"],
        reply_text=bundle["reply_text"],
        reply_subject=bundle["reply_subject"],
        model_emails=bundle["model_emails"],
        operator_first_name=bundle["operator_first_name"],
        mode=composer_mode,
    )

    # Verdict gate. Reads the composed output + source artifacts, decides
    # ship-or-block. On block, park in 'pending_review' (operator decides
    # via dashboard). Mirrors the actor/verdict pattern PR #184 set up
    # for gtm-pipeline subagents.
    verdict_mode_str = verdict_mode or settings.CLUSTER3_VERDICT_MODE or "auto"
    verdict_result = await intro_verdict.review(
        composed_subject=composed["subject"],
        composed_body_text=composed["body_text"],
        partner_research_md=bundle["partner_research_md"],
        recipient_gestalt_md=bundle["recipient_gestalt_md"],
        reply_text=bundle["reply_text"],
        model_emails=bundle["model_emails"],
        operator_first_name=bundle["operator_first_name"],
        mode=verdict_mode_str,  # type: ignore[arg-type]
    )

    if not verdict_result["ship"] and settings.CLUSTER3_VERDICT_GATES_SEND:
        return await _park_pending_review(
            ctx=ctx,
            leg3_initiative_id=leg3["leg3_initiative_id"],
            leg3=leg3,
            composed=composed,
            verdict_result=verdict_result,
            allocation_snapshot=allocation_snapshot,
        )

    # Insert lead_transfer in 'queued' (unique index gates double-spend).
    try:
        lt_id = await _insert_lead_transfer(
            ctx=ctx,
            leg3_initiative_id=leg3["leg3_initiative_id"],
            allocation_snapshot=allocation_snapshot,
            status="queued",
            metadata={
                "composer_backend": composed.get("backend"),
                "composer_model": composed.get("model"),
                "verdict_score": verdict_result.get("score"),
                "verdict_blockers": verdict_result.get("blockers"),
                "verdict_backend": verdict_result.get("backend"),
            },
        )
    except psycopg_errors.UniqueViolation:
        # Concurrent dispatch beat us. Read back and short-circuit.
        existing = await _existing_lead_transfer_for_classification(classification_id)
        return {
            "status": "already_sent",
            "classification_id": str(classification_id),
            "lead_transfer_id": str(existing["id"]) if existing else None,
            "race": True,
        }

    # Build the intro email_messages row in pending state.
    intro_email_message_id = await _insert_intro_email_message(
        ctx=ctx,
        leg3=leg3,
        composed=composed,
        lead_transfer_id=lt_id,
    )

    # Send via EmailBison (live or dry-run, gated by CLUSTER3_LIVE_SEND).
    try:
        send_result = await eb_send.send_intro(
            sender_email_id=None,
            to_email=bundle["recipient_email"],
            to_name=bundle["recipient_name"],
            cc_emails=None,
            subject=composed["subject"],
            body_text=composed["body_text"],
            body_html=composed.get("body_html"),
            metadata={
                "lead_transfer_id": str(lt_id),
                "intro_email_message_id": str(intro_email_message_id),
                "leg2_initiative_id": str(ctx["leg2_initiative_id"]),
            },
        )
    except eb_send.ClusterIntroSendError as exc:
        await _mark_lead_transfer_failed(lt_id, reason=str(exc)[:500])
        await _mark_email_message_status(intro_email_message_id, status="failed")
        await alerts.fire_alert(
            severity="critical",
            source="cluster3_dispatch",
            summary=f"EB send failed for lead_transfer {lt_id}",
            payload={
                "lead_transfer_id": str(lt_id),
                "classification_id": str(classification_id),
                "intro_email_message_id": str(intro_email_message_id),
                "error": str(exc)[:500],
            },
        )
        return {
            "status": "failed",
            "classification_id": str(classification_id),
            "lead_transfer_id": str(lt_id),
            "intro_email_message_id": str(intro_email_message_id),
            "error": str(exc)[:500],
            "allocation_snapshot": allocation_snapshot,
        }

    # Stamp success.
    await _mark_dispatch_sent(
        classification_id=classification_id,
        lead_transfer_id=lt_id,
        intro_email_message_id=intro_email_message_id,
        eb_reply_id=send_result.get("eb_reply_id"),
        send_mode=send_result.get("mode"),
        send_payload=send_result.get("payload"),
    )

    return {
        "status": "sent",
        "classification_id": str(classification_id),
        "lead_transfer_id": str(lt_id),
        "intro_email_message_id": str(intro_email_message_id),
        "send_mode": send_result.get("mode"),
        "eb_reply_id": send_result.get("eb_reply_id"),
        "allocation_snapshot": allocation_snapshot,
        "composer_backend": composed.get("backend"),
    }


# ── Context resolution ────────────────────────────────────────────────────


async def _resolve_dispatch_context(
    classification_id: UUID,
) -> dict[str, Any] | None:
    """Single read that joins everything we need for the dispatch."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    erc.id, erc.classification, erc.intro_fired_at,
                    erc.intro_email_message_id, erc.email_message_id,
                    em.organization_id, em.brand_id, em.recipient_id,
                    em.subject_snapshot, em.body_snapshot,
                    em.metadata,
                    cc.initiative_id  AS leg2_initiative_id_via_cc,
                    init.id           AS leg2_initiative_id,
                    init.partner_id, init.partner_contract_id,
                    init.data_engine_audience_id,
                    init.metadata     AS init_metadata
                FROM business.email_reply_classifications erc
                JOIN business.email_messages em ON em.id = erc.email_message_id
                JOIN business.channel_campaign_steps step
                  ON step.id = em.channel_campaign_step_id
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                WHERE erc.id = %s
                """,
                (str(classification_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    init_metadata = row[16] or {}
    leg = init_metadata.get("leg") if isinstance(init_metadata, dict) else None
    return {
        "classification_id": row[0],
        "classification": row[1],
        "intro_fired_at": row[2],
        "intro_email_message_id": row[3],
        "email_message_id": row[4],
        "organization_id": row[5],
        "brand_id": row[6],
        "recipient_id": row[7],
        "reply_subject": row[8],   # outbound subject_snapshot, not reply
        "reply_text": row[9],      # outbound body_snapshot, not reply
        "email_message_metadata": row[10] or {},
        "leg2_initiative_id": row[12],
        "partner_id": row[13],
        "partner_contract_id": row[14],
        "data_engine_audience_id": row[15],
        "init_metadata": init_metadata,
        "leg": leg,
    }


async def _resolve_leg3(
    *, leg2_initiative_id: UUID
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT init.id, c.id, cc.id, step.id, step.channel_specific_config
                FROM business.gtm_initiatives init
                JOIN business.campaigns c ON c.initiative_id = init.id
                JOIN business.channel_campaigns cc ON cc.campaign_id = c.id
                JOIN business.channel_campaign_steps step
                  ON step.channel_campaign_id = cc.id
                WHERE init.parent_initiative_id = %s
                  AND (init.metadata->>'leg')::int = 3
                ORDER BY step.step_order ASC
                LIMIT 1
                """,
                (str(leg2_initiative_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "leg3_initiative_id": row[0],
        "leg3_campaign_id": row[1],
        "leg3_channel_campaign_id": row[2],
        "leg3_step_id": row[3],
        "leg3_step_template": row[4] or {},
    }


async def _resolve_allocation_cap(
    *,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
) -> dict[str, Any]:
    """Allocation cap = paid transfer count.

    Source of truth precedence:
      1. proposals.proposed_transfer_count (PR #184) when a proposal links
         to this partner_contract_id and matches the audience.
      2. Fallback: derive from partner_contracts.amount_cents /
         per-transfer cents in proposal if either present.
      3. None — uncapped (operator can dispatch freely).
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT proposed_transfer_count, proposed_price_per_transfer_cents,
                       proposed_total_cents
                FROM business.proposals
                WHERE partner_contract_id = %s
                  AND (final_data_engine_audience_id = %s
                       OR proposed_data_engine_audience_id = %s)
                  AND status = 'paid'
                ORDER BY paid_at DESC NULLS LAST
                LIMIT 1
                """,
                (
                    str(partner_contract_id),
                    str(data_engine_audience_id),
                    str(data_engine_audience_id),
                ),
            )
            prop_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT amount_cents, duration_days, status
                FROM business.partner_contracts
                WHERE id = %s
                """,
                (str(partner_contract_id),),
            )
            contract_row = await cur.fetchone()

    if prop_row is not None:
        return {
            "paid_transfer_count": prop_row[0],
            "per_transfer_price_cents": prop_row[1],
            "amount_cents": prop_row[2],
            "computed_via": "proposal",
        }
    if contract_row is not None:
        return {
            "paid_transfer_count": None,
            "per_transfer_price_cents": None,
            "amount_cents": contract_row[0],
            "computed_via": "contract_only_no_count",
        }
    return {
        "paid_transfer_count": None,
        "per_transfer_price_cents": None,
        "amount_cents": None,
        "computed_via": "missing",
    }


async def _count_delivered_transfers(
    *,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
) -> int:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM business.lead_transfers
                WHERE partner_id = %s
                  AND partner_contract_id = %s
                  AND data_engine_audience_id = %s
                  AND status = 'sent'
                """,
                (
                    str(partner_id),
                    str(partner_contract_id),
                    str(data_engine_audience_id),
                ),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


# ── Compose bundle gathering ──────────────────────────────────────────────


async def _gather_compose_bundle(
    *, ctx: dict[str, Any], leg3: dict[str, Any]
) -> dict[str, Any]:
    recipient = await _fetch_recipient(ctx["recipient_id"])
    partner = await _fetch_partner(ctx["partner_id"])
    partner_research_md = await _fetch_partner_research(ctx["partner_id"])
    audience_context_md = await _fetch_audience_context(
        ctx["data_engine_audience_id"]
    )
    recipient_gestalt_md = None  # filled by gestalt fetcher when wired
    model_emails = await _fetch_model_emails(
        organization_id=ctx["organization_id"],
        purpose="lead_intro",
    )

    # The reply text lives in email_message_events, NOT in
    # email_messages.body_snapshot (which is the original outbound). The
    # ctx.reply_text/subject keys are the outbound snapshot — keep as
    # fallback if no inbound event row exists yet.
    reply_text, reply_subject, _reply_from = await _fetch_reply_from_events(
        ctx["email_message_id"]
    )
    if not reply_text:
        reply_text = ctx.get("reply_text")
    if not reply_subject:
        reply_subject = ctx.get("reply_subject")

    operator_first_name = "Ben"

    return {
        "recipient": recipient,
        "recipient_email": (recipient or {}).get("email"),
        "recipient_name": _fmt_name(recipient),
        "partner": partner,
        "partner_research_md": partner_research_md,
        "audience_context_md": audience_context_md,
        "recipient_gestalt_md": recipient_gestalt_md,
        "reply_text": reply_text,
        "reply_subject": reply_subject,
        "model_emails": model_emails,
        "operator_first_name": operator_first_name,
    }


def _fmt_name(recipient: dict[str, Any] | None) -> str | None:
    if not recipient:
        return None
    parts = [recipient.get("first_name"), recipient.get("last_name")]
    parts = [p for p in parts if p]
    return " ".join(parts) if parts else None


async def _fetch_reply_from_events(
    email_message_id: UUID,
) -> tuple[str | None, str | None, str | None]:
    """Pull the latest replied/interested event payload's reply body."""
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
        body = (
            reply.get("text_body")
            or reply.get("plain_body")
            or reply.get("body")
            or reply.get("html_body")
        )
        if body:
            return (
                str(body),
                reply.get("subject"),
                reply.get("from_email_address") or reply.get("from"),
            )
    return None, None, None


async def _fetch_recipient(recipient_id: UUID | None) -> dict[str, Any] | None:
    if recipient_id is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, display_name, email, recipient_type, external_id, metadata
                FROM business.recipients
                WHERE id = %s
                """,
                (str(recipient_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    display_name = row[1] or ""
    md = row[5] or {}
    first = (md.get("first_name") if isinstance(md, dict) else None) or (
        display_name.split()[0] if display_name else None
    )
    last = (md.get("last_name") if isinstance(md, dict) else None) or (
        " ".join(display_name.split()[1:]) if len(display_name.split()) > 1 else None
    )
    return {
        "id": row[0],
        "display_name": display_name,
        "first_name": first,
        "last_name": last,
        "email": row[2],
        "recipient_type": row[3],
        "external_id": row[4],
        "metadata": md,
    }


async def _fetch_partner(partner_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, primary_contact_name, primary_contact_email,
                       domain, metadata
                FROM business.demand_side_partners
                WHERE id = %s
                """,
                (str(partner_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "primary_contact_name": row[2],
        "primary_contact_email": row[3],
        "domain": row[4],
        "metadata": row[5] or {},
    }


async def _fetch_partner_research(partner_id: UUID) -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT research_md
                FROM business.partner_research_artifacts
                WHERE partner_id = %s
                """,
                (str(partner_id),),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def _fetch_audience_context(audience_id: UUID) -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT context_md
                FROM business.audience_context_artifacts
                WHERE data_engine_audience_id = %s
                """,
                (str(audience_id),),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def _fetch_model_emails(
    *, organization_id: UUID, purpose: str, limit: int = 3
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT label, subject, body, notes
                FROM business.outreach_model_emails
                WHERE organization_id = %s
                  AND purpose = %s
                  AND is_active = TRUE
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(organization_id), purpose, limit),
            )
            rows = await cur.fetchall()
    return [
        {"label": r[0], "subject": r[1], "body": r[2], "notes": r[3]} for r in rows
    ]


# ── Lead-transfer + email-message lifecycle writes ───────────────────────


async def _park_pending_review(
    *,
    ctx: dict[str, Any],
    leg3_initiative_id: UUID,
    leg3: dict[str, Any],
    composed: dict[str, Any],
    verdict_result: dict[str, Any],
    allocation_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Verdict gate blocked the intro. Park the lead_transfer in
    pending_review with the verdict diagnostic for operator review."""
    lt_id = await _insert_lead_transfer(
        ctx=ctx,
        leg3_initiative_id=leg3_initiative_id,
        allocation_snapshot=allocation_snapshot,
        status="pending_review",
        metadata={
            "verdict_blockers": verdict_result.get("blockers"),
            "verdict_score": verdict_result.get("score"),
            "verdict_rationale": verdict_result.get("rationale"),
            "verdict_backend": verdict_result.get("backend"),
            "verdict_raw_output": verdict_result.get("raw_output"),
            "composer_backend": composed.get("backend"),
            "composer_model": composed.get("model"),
        },
    )
    intro_email_message_id = await _insert_intro_email_message(
        ctx=ctx,
        leg3=leg3,
        composed=composed,
        lead_transfer_id=lt_id,
    )
    # Don't touch email_messages.status — its enum is the EB lifecycle
    # ('pending', 'scheduled', 'sent', 'opened', 'replied', 'bounced',
    # 'unsubscribed', 'failed'). Verdict-hold is a Cluster-3 pipeline
    # state and lives on lead_transfers.status='pending_review'. Stamp
    # metadata only here.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.email_messages
                SET metadata = metadata || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    Jsonb({"held_by_verdict_gate": True}),
                    str(intro_email_message_id),
                ),
            )
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET intro_email_message_id = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (str(intro_email_message_id), str(lt_id)),
            )
        await conn.commit()
    await alerts.fire_alert(
        severity="warning",
        source="cluster3_dispatch",
        summary=(
            f"Intro held by verdict gate (blockers: "
            f"{', '.join(verdict_result.get('blockers') or []) or 'none'})"
        ),
        payload={
            "lead_transfer_id": str(lt_id),
            "intro_email_message_id": str(intro_email_message_id),
            "verdict": verdict_result,
            "review_url": f"/admin/cluster3-health#pending-review",
        },
    )
    return {
        "status": "pending_review",
        "classification_id": str(ctx["classification_id"]),
        "lead_transfer_id": str(lt_id),
        "intro_email_message_id": str(intro_email_message_id),
        "verdict": verdict_result,
        "allocation_snapshot": allocation_snapshot,
    }


async def _insert_lead_transfer(
    *,
    ctx: dict[str, Any],
    leg3_initiative_id: UUID,
    allocation_snapshot: dict[str, Any],
    status: str,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    md = dict(metadata or {})
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.lead_transfers (
                    organization_id, brand_id, partner_id, partner_contract_id,
                    data_engine_audience_id,
                    leg2_initiative_id, leg3_initiative_id,
                    recipient_id,
                    positive_reply_email_message_id,
                    email_reply_classification_id,
                    status, allocation_snapshot, failure_reason, metadata,
                    failed_at
                )
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s,
                        %s,
                        %s,
                        %s,
                        %s, %s, %s, %s,
                        CASE WHEN %s = 'failed' OR %s = 'deferred_capped'
                             THEN NOW() ELSE NULL END)
                RETURNING id
                """,
                (
                    str(ctx["organization_id"]),
                    str(ctx["brand_id"]),
                    str(ctx["partner_id"]),
                    str(ctx["partner_contract_id"]),
                    str(ctx["data_engine_audience_id"]),
                    str(ctx["leg2_initiative_id"]),
                    str(leg3_initiative_id),
                    str(ctx["recipient_id"]) if ctx.get("recipient_id") else None,
                    str(ctx["email_message_id"]),
                    str(ctx["classification_id"]),
                    status,
                    Jsonb(allocation_snapshot),
                    failure_reason,
                    Jsonb(md),
                    status,
                    status,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _existing_lead_transfer_for_classification(
    classification_id: UUID,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status
                FROM business.lead_transfers
                WHERE email_reply_classification_id = %s
                  AND status IN ('queued', 'sent')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(classification_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "status": row[1]}


async def _insert_intro_email_message(
    *,
    ctx: dict[str, Any],
    leg3: dict[str, Any],
    composed: dict[str, Any],
    lead_transfer_id: UUID,
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages (
                    organization_id, brand_id, campaign_id, channel_campaign_id,
                    channel_campaign_step_id, recipient_id,
                    subject_snapshot, body_snapshot, status, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    str(ctx["organization_id"]),
                    str(ctx["brand_id"]),
                    str(leg3["leg3_campaign_id"]),
                    str(leg3["leg3_channel_campaign_id"]),
                    str(leg3["leg3_step_id"]),
                    str(ctx["recipient_id"]) if ctx.get("recipient_id") else None,
                    composed["subject"],
                    composed["body_text"],
                    Jsonb(
                        {
                            "leg": 3,
                            "lead_transfer_id": str(lead_transfer_id),
                            "intro_for_classification_id": str(
                                ctx["classification_id"]
                            ),
                            "composer_backend": composed.get("backend"),
                            "composer_model": composed.get("model"),
                        }
                    ),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _mark_lead_transfer_failed(
    lead_transfer_id: UUID, *, reason: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET status = 'failed', failed_at = NOW(),
                    failure_reason = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (reason, str(lead_transfer_id)),
            )
        await conn.commit()


async def _mark_email_message_status(email_message_id: UUID, *, status: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.email_messages
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, str(email_message_id)),
            )
        await conn.commit()


async def _mark_dispatch_sent(
    *,
    classification_id: UUID,
    lead_transfer_id: UUID,
    intro_email_message_id: UUID,
    eb_reply_id: int | None,
    send_mode: str | None,
    send_payload: dict[str, Any] | None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET status = 'sent', sent_at = NOW(),
                    intro_email_message_id = %s,
                    metadata = metadata || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(intro_email_message_id),
                    Jsonb(
                        {
                            "send_mode": send_mode,
                            "eb_reply_id": eb_reply_id,
                        }
                    ),
                    str(lead_transfer_id),
                ),
            )
            await cur.execute(
                """
                UPDATE business.email_messages
                SET status = 'sent', sent_at = NOW(),
                    metadata = metadata || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    Jsonb(
                        {
                            "send_mode": send_mode,
                            "send_payload": send_payload or {},
                            "eb_reply_id": eb_reply_id,
                        }
                    ),
                    str(intro_email_message_id),
                ),
            )
            await cur.execute(
                """
                UPDATE business.email_reply_classifications
                SET intro_fired_at = NOW(),
                    intro_email_message_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(intro_email_message_id), str(classification_id)),
            )
        await conn.commit()


__all__ = [
    "dispatch_for_classification",
    "Cluster3DispatchError",
    "Cluster3PreconditionError",
]
