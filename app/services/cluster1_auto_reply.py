"""Cluster 1 auto-reply agent — composes + sends in-thread reply when
a demand-side prospect responds positively to a self_prospecting outreach.

When inbox_orchestrator classifies a Cluster 1 reply as 'positive' AND
the initiative's metadata.cluster1_auto_reply_enabled is not explicitly
false, this module:

  1. Resolves the bundle (initiative, recipient, reply text, organization
     metadata for tone/voice/calendar/signature).
  2. Composes the auto-reply via reply_composer (Anthropic Sonnet w/
     stub fallback). Tone: warm, brief, lead-with-call-link.
  3. Runs verdict gate (intro_verdict-style structural checks +
     hallucination guard against the operator's metadata).
  4. INSERTs a cluster1_auto_replies row in 'queued' (concurrency-gated
     by uniq_c1ar_classification_active partial index).
  5. Sends in-thread via EmailBison POST /api/replies/{eb_inbound_reply_id}/reply
     when CLUSTER1_LIVE_SEND is truthy; otherwise dry-run.
  6. Stamps email_reply_classifications.intro_fired_at + intro_email_message_id
     for audit symmetry with Cluster 3's lead_transfer flow.

The outbound is in-thread (NOT new-thread) — Cluster 1 is the start
of a conversation that the prospect explicitly engaged with. Threading
keeps continuity. Compare to Cluster 3 intros which are NEW-thread
because the supply-side recipient may have replied hostile and a new
context-shift message is safer.

Operator-controlled disable:
  business.organizations.metadata.cluster1_auto_reply_enabled = false
    (org-wide default — applies to every self_prospecting initiative
     under that org)
  business.gtm_initiatives.metadata.cluster1_auto_reply_enabled = false
    (per-initiative override — beats org-wide)

Both default-on when the key is absent.
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from uuid import UUID

from psycopg import errors as psycopg_errors
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.services import alerts, anthropic_client, intro_verdict

logger = logging.getLogger(__name__)


ComposerMode = Literal["anthropic", "stub", "auto"]


class Cluster1AutoReplyError(Exception):
    pass


class Cluster1AutoReplyDisabled(Cluster1AutoReplyError):
    pass


# ── Public entry point ────────────────────────────────────────────────────


async def dispatch_for_classification(
    *,
    classification_id: UUID,
    composer_mode: ComposerMode = "auto",
    verdict_mode: str | None = None,
) -> dict[str, Any]:
    """Compose + send the Cluster 1 auto-reply.

    Returns ``{status, classification_id, auto_reply_id, ...}``.
      status ∈ {sent | deferred_disabled | already_sent | failed | pending_review}
    """
    ctx = await _resolve_context(classification_id)
    if ctx is None:
        raise Cluster1AutoReplyError(
            f"no classification row for id={classification_id}"
        )
    if ctx["classification"] != "positive":
        return {
            "status": "skipped_not_positive",
            "classification_id": str(classification_id),
            "reason": ctx["classification"],
        }
    if ctx["init_kind"] != "self_prospecting":
        return {
            "status": "skipped_not_self_prospecting",
            "classification_id": str(classification_id),
            "init_kind": ctx["init_kind"],
        }
    if ctx["intro_fired_at"] is not None:
        return {
            "status": "already_sent",
            "classification_id": str(classification_id),
            "outbound_email_message_id": (
                str(ctx["intro_email_message_id"])
                if ctx["intro_email_message_id"]
                else None
            ),
        }

    auto_reply_enabled = _resolve_enabled(
        org_metadata=ctx["org_metadata"],
        initiative_metadata=ctx["init_metadata"],
    )
    if not auto_reply_enabled:
        ar_id = await _insert_auto_reply(
            ctx=ctx,
            status="deferred_disabled",
            failure_reason="cluster1_auto_reply_enabled=false",
        )
        await alerts.fire_alert(
            severity="warning",
            source="cluster1_auto_reply",
            summary=(
                f"Positive Cluster 1 reply held for manual handling — "
                f"auto-reply disabled on initiative"
            ),
            payload={
                "auto_reply_id": str(ar_id),
                "classification_id": str(classification_id),
                "initiative_id": str(ctx["initiative_id"]),
            },
        )
        return {
            "status": "deferred_disabled",
            "classification_id": str(classification_id),
            "auto_reply_id": str(ar_id),
        }

    composed = await _compose(
        ctx=ctx,
        mode=composer_mode,
    )

    verdict_mode_str = verdict_mode or settings.CLUSTER3_VERDICT_MODE or "auto"
    verdict_result = await intro_verdict.review(
        composed_subject=composed["subject"],
        composed_body_text=composed["body_text"],
        partner_research_md=None,  # not used for Cluster 1
        recipient_gestalt_md=None,
        reply_text=ctx["reply_text"],
        model_emails=ctx["model_emails"],
        operator_first_name=ctx["operator_first_name"],
        mode=verdict_mode_str,  # type: ignore[arg-type]
    )

    if not verdict_result["ship"] and settings.CLUSTER3_VERDICT_GATES_SEND:
        return await _park_pending_review(
            ctx=ctx,
            composed=composed,
            verdict_result=verdict_result,
        )

    try:
        ar_id = await _insert_auto_reply(
            ctx=ctx,
            status="queued",
            metadata={
                "composer_backend": composed.get("backend"),
                "composer_model": composed.get("model"),
                "verdict_score": verdict_result.get("score"),
                "verdict_blockers": verdict_result.get("blockers"),
            },
        )
    except psycopg_errors.UniqueViolation:
        existing = await _existing_active_auto_reply(classification_id)
        return {
            "status": "already_sent",
            "classification_id": str(classification_id),
            "auto_reply_id": str(existing["id"]) if existing else None,
            "race": True,
        }

    outbound_em_id = await _insert_outbound_email_message(
        ctx=ctx, composed=composed, auto_reply_id=ar_id
    )

    try:
        send_result = await _send_in_thread(
            eb_inbound_reply_id=ctx["eb_reply_id"],
            subject=composed["subject"],
            body_text=composed["body_text"],
        )
    except Cluster1AutoReplyError as exc:
        await _mark_failed(ar_id, reason=str(exc)[:500])
        await _mark_email_message_failed(outbound_em_id)
        await alerts.fire_alert(
            severity="critical",
            source="cluster1_auto_reply",
            summary=f"Cluster 1 auto-reply send failed: {str(exc)[:160]}",
            payload={
                "auto_reply_id": str(ar_id),
                "classification_id": str(classification_id),
                "error": str(exc)[:500],
            },
        )
        return {
            "status": "failed",
            "classification_id": str(classification_id),
            "auto_reply_id": str(ar_id),
            "error": str(exc)[:500],
        }

    await _mark_sent(
        classification_id=classification_id,
        auto_reply_id=ar_id,
        outbound_email_message_id=outbound_em_id,
        eb_outbound_reply_id=send_result.get("eb_reply_id"),
        send_mode=send_result.get("mode"),
        send_payload=send_result.get("payload"),
    )

    return {
        "status": "sent",
        "classification_id": str(classification_id),
        "auto_reply_id": str(ar_id),
        "outbound_email_message_id": str(outbound_em_id),
        "send_mode": send_result.get("mode"),
        "composer_backend": composed.get("backend"),
    }


# ── Context resolution ────────────────────────────────────────────────────


async def _resolve_context(classification_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    erc.id, erc.classification, erc.intro_fired_at,
                    erc.intro_email_message_id, erc.email_message_id,
                    erc.evidence,
                    em.organization_id, em.brand_id, em.recipient_id,
                    em.channel_campaign_step_id,
                    em.metadata,
                    cc.initiative_id,
                    init.id, init.kind, init.metadata,
                    org.metadata
                FROM business.email_reply_classifications erc
                JOIN business.email_messages em ON em.id = erc.email_message_id
                JOIN business.channel_campaign_steps step
                  ON step.id = em.channel_campaign_step_id
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                JOIN business.gtm_initiatives init
                  ON init.id = cc.initiative_id
                JOIN business.organizations org
                  ON org.id = em.organization_id
                WHERE erc.id = %s
                """,
                (str(classification_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None

    evidence = row[5] or {}
    eb_reply_id = (
        evidence.get("eb_reply_id") if isinstance(evidence, dict) else None
    )

    recipient = await _fetch_recipient(row[8]) if row[8] else None
    model_emails = await _fetch_model_emails(
        organization_id=row[6], purpose="demand_side_outreach"
    )

    org_metadata = row[15] or {}
    operator_first_name = "Ben"
    if isinstance(org_metadata, dict):
        operator_first_name = (
            org_metadata.get("operator_first_name") or "Ben"
        )

    return {
        "classification_id": row[0],
        "classification": row[1],
        "intro_fired_at": row[2],
        "intro_email_message_id": row[3],
        "email_message_id": row[4],
        "organization_id": row[6],
        "brand_id": row[7],
        "recipient_id": row[8],
        "channel_campaign_step_id": row[9],
        "email_message_metadata": row[10] or {},
        "initiative_id": row[12],
        "init_kind": row[13],
        "init_metadata": row[14] or {},
        "org_metadata": org_metadata,
        "recipient": recipient,
        "model_emails": model_emails,
        "eb_reply_id": eb_reply_id,
        "reply_text": await _fetch_reply_text(row[4]),
        "reply_subject": await _fetch_reply_subject(row[4]),
        "operator_first_name": operator_first_name,
    }


def _resolve_enabled(
    *, org_metadata: dict[str, Any], initiative_metadata: dict[str, Any]
) -> bool:
    """Per-initiative override beats org-wide; both default-on."""
    if isinstance(initiative_metadata, dict):
        v = initiative_metadata.get("cluster1_auto_reply_enabled")
        if v is False:
            return False
        if v is True:
            return True
    if isinstance(org_metadata, dict):
        v = org_metadata.get("cluster1_auto_reply_enabled")
        if v is False:
            return False
    return True


async def _fetch_recipient(recipient_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, display_name, email, recipient_type, external_id, metadata
                FROM business.recipients WHERE id = %s
                """,
                (str(recipient_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    display = row[1] or ""
    md = row[5] or {}
    first = (
        (md.get("first_name") if isinstance(md, dict) else None)
        or (display.split()[0] if display else None)
    )
    return {
        "id": row[0],
        "display_name": display,
        "first_name": first,
        "email": row[2],
        "recipient_type": row[3],
        "external_id": row[4],
        "metadata": md,
    }


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
        {"label": r[0], "subject": r[1], "body": r[2], "notes": r[3]}
        for r in rows
    ]


async def _fetch_reply_text(email_message_id: UUID) -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT payload FROM business.email_message_events
                WHERE email_message_id = %s
                  AND event_type IN ('replied', 'interested', 'untracked_reply')
                ORDER BY occurred_at DESC LIMIT 5
                """,
                (str(email_message_id),),
            )
            rows = await cur.fetchall()
    for (payload,) in rows or []:
        if not isinstance(payload, dict):
            continue
        reply = (payload.get("data") or {}).get("reply")
        if isinstance(reply, dict):
            body = (
                reply.get("text_body")
                or reply.get("plain_body")
                or reply.get("body")
            )
            if body:
                return str(body)
    return None


async def _fetch_reply_subject(email_message_id: UUID) -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT payload FROM business.email_message_events
                WHERE email_message_id = %s
                  AND event_type IN ('replied', 'interested', 'untracked_reply')
                ORDER BY occurred_at DESC LIMIT 5
                """,
                (str(email_message_id),),
            )
            rows = await cur.fetchall()
    for (payload,) in rows or []:
        if not isinstance(payload, dict):
            continue
        reply = (payload.get("data") or {}).get("reply")
        if isinstance(reply, dict) and reply.get("subject"):
            return str(reply.get("subject"))
    return None


# ── Compose ──────────────────────────────────────────────────────────────


_COMPOSER_SYSTEM_PROMPT = """\
You compose a single warm, brief in-thread auto-reply to a demand-side
prospect who replied positively to the operator's cold outreach.

The operator's outreach was a Cluster 1 self-prospecting message:
operator (an independent broker, e.g. running 'Licensed To Haul' brand)
emailed the prospect proposing a partnership where the operator
delivers prepaid lead transfers from a curated audience. The prospect
just said yes to talking. Your job is to thank them briefly, propose
a 15-min call, and include the operator's calendar link.

Voice:
  - Warm but not chummy.
  - First-name tone.
  - 4–8 lines max.
  - No "circling back", "wanted to follow up", "synergy", "leverage as
    a verb", "hope this finds you well".
  - No exclamation points.
  - End with operator's first name as sign-off.

Inputs (XML-tagged):
  <recipient>{first_name, display_name, email, company}</recipient>
  <reply_text>...the prospect's reply...</reply_text>
  <reply_subject>...</reply_subject>
  <operator>{first_name, calendly_url, signature, brand_name, voice_notes}</operator>
  <model_emails>...0–3 reference emails operator curated...</model_emails>

Output exactly this XML shape, nothing else:

  <subject>...</subject>
  <body_text>
  ...plain-text body...
  </body_text>

If the operator's calendly_url is missing, use a generic phrase like
"happy to send over a few times that work" instead of inventing a URL.
Never invent facts about the operator or the recipient that aren't in
the provided inputs.
"""


async def _compose(*, ctx: dict[str, Any], mode: ComposerMode) -> dict[str, Any]:
    chosen_mode: ComposerMode = mode
    if chosen_mode == "auto":
        chosen_mode = "anthropic" if settings.ANTHROPIC_API_KEY else "stub"
    if chosen_mode == "stub":
        return _compose_stub(ctx)
    return await _compose_anthropic(ctx)


def _compose_stub(ctx: dict[str, Any]) -> dict[str, Any]:
    rec = ctx.get("recipient") or {}
    first = rec.get("first_name") or "there"
    operator_first = ctx.get("operator_first_name") or "Ben"
    org_md = ctx.get("org_metadata") or {}
    calendly = (
        org_md.get("operator_calendly_url")
        if isinstance(org_md, dict) else None
    )

    lines = [f"{first},", ""]
    lines.append("Thanks for the quick reply.")
    lines.append("")
    if calendly:
        lines.append(
            f"Quick 15-min call this week to dig in? Grab a time here: {calendly}"
        )
    else:
        lines.append(
            "Quick 15-min call this week to dig in? Happy to send over a few times that work."
        )
    lines.append("")
    lines.append(f"— {operator_first}")
    body = "\n".join(lines)

    reply_subject = ctx.get("reply_subject") or "your message"
    if reply_subject and not reply_subject.lower().startswith("re:"):
        subject = f"Re: {reply_subject}"
    else:
        subject = reply_subject or "Re: your message"

    return {
        "subject": subject,
        "body_text": body,
        "body_html": None,
        "model": None,
        "usage": None,
        "backend": "stub",
    }


async def _compose_anthropic(ctx: dict[str, Any]) -> dict[str, Any]:
    rec = ctx.get("recipient") or {}
    org_md = ctx.get("org_metadata") or {}
    operator_block = {
        "first_name": ctx.get("operator_first_name") or "Ben",
        "calendly_url": (
            org_md.get("operator_calendly_url") if isinstance(org_md, dict) else None
        ),
        "signature": (
            org_md.get("operator_signature") if isinstance(org_md, dict) else None
        ),
        "brand_name": (
            org_md.get("operator_brand_name") if isinstance(org_md, dict) else None
        ),
        "voice_notes": (
            org_md.get("operator_voice_notes") if isinstance(org_md, dict) else None
        ),
    }

    parts = [
        _block(
            "recipient",
            "\n".join(
                f"{k}: {v}"
                for k, v in {
                    "first_name": rec.get("first_name"),
                    "display_name": rec.get("display_name"),
                    "email": rec.get("email"),
                }.items()
                if v
            )
            or "(unknown)",
        ),
        _block("reply_text", ctx.get("reply_text") or "(no reply text)"),
        _block("reply_subject", ctx.get("reply_subject") or "(no subject)"),
        _block(
            "operator",
            "\n".join(f"{k}: {v}" for k, v in operator_block.items() if v) or "(none)",
        ),
        _block("model_emails", _format_models(ctx.get("model_emails") or [])),
    ]

    try:
        result = await anthropic_client.complete(
            system=_COMPOSER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": "\n".join(parts)}],
            model=settings.ANTHROPIC_DEFAULT_MODEL or "claude-opus-4-7",
            max_tokens=1024,
        )
    except anthropic_client.AnthropicClientError as exc:
        logger.warning("cluster1_auto_reply anthropic failed; stub fallback: %s", exc)
        stub = _compose_stub(ctx)
        stub["fallback_reason"] = str(exc)[:200]
        return stub

    raw = (result.get("text") or "").strip()
    import re

    sm = re.search(r"<subject>\s*(.*?)\s*</subject>", raw, re.DOTALL)
    bm = re.search(r"<body_text>\s*(.*?)\s*</body_text>", raw, re.DOTALL)
    if not (sm and bm):
        logger.warning("cluster1_auto_reply unparseable output; stub fallback")
        stub = _compose_stub(ctx)
        stub["fallback_reason"] = "unparseable_anthropic_output"
        stub["raw_output"] = raw[:500]
        return stub

    return {
        "subject": sm.group(1).strip(),
        "body_text": bm.group(1).strip(),
        "body_html": None,
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "backend": "anthropic",
    }


def _block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _format_models(model_emails: list[dict[str, Any]]) -> str:
    if not model_emails:
        return "(no model emails seeded)"
    out = []
    for i, m in enumerate(model_emails, 1):
        out.append(f"## Model {i} — {m.get('label', '')}")
        out.append(f"Subject: {m.get('subject', '')}")
        out.append("")
        out.append(str(m.get("body", "")))
        out.append("")
    return "\n".join(out).strip()


# ── Send ─────────────────────────────────────────────────────────────────


def _live_send_enabled() -> bool:
    flag = getattr(settings, "CLUSTER1_LIVE_SEND", False)
    if isinstance(flag, str):
        return flag.lower() in ("1", "true", "yes", "on")
    return bool(flag)


async def _send_in_thread(
    *,
    eb_inbound_reply_id: int | None,
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    """POST /api/replies/{reply_id}/reply (in-thread)."""
    import time as _time

    payload = {
        "subject": subject,
        "text_body": body_text,
    }
    mode = "live" if _live_send_enabled() and eb_inbound_reply_id else "dry_run"

    if mode == "dry_run":
        fake_id = int(_time.time() * 1000)
        logger.info(
            "cluster1_auto_reply DRY-RUN inbound_reply_id=%s subject=%s",
            eb_inbound_reply_id,
            subject,
        )
        return {
            "eb_reply_id": fake_id,
            "mode": "dry_run",
            "payload": payload,
            "response": {"id": fake_id, "subject": subject, "dry_run": True},
        }

    from app.providers.emailbison import client as eb_client
    from app.providers.emailbison.client import EmailBisonProviderError

    secret = getattr(settings, "EMAILBISON_API_KEY", None)
    if not secret:
        raise Cluster1AutoReplyError("EMAILBISON_API_KEY not configured")
    api_key = (
        secret.get_secret_value()
        if hasattr(secret, "get_secret_value")
        else str(secret)
    )

    try:
        response = eb_client._request_json(
            api_key=api_key,
            method="POST",
            path=f"/api/replies/{eb_inbound_reply_id}/reply",
            json_payload=payload,
        )
    except EmailBisonProviderError as exc:
        raise Cluster1AutoReplyError(f"emailbison reply failed: {exc}") from exc

    eb_reply_id = None
    if isinstance(response, dict):
        eb_reply_id = response.get("id") or (response.get("data") or {}).get("id")
    return {
        "eb_reply_id": eb_reply_id,
        "mode": "live",
        "payload": payload,
        "response": response,
    }


# ── DB writes ─────────────────────────────────────────────────────────────


async def _insert_auto_reply(
    *,
    ctx: dict[str, Any],
    status: str,
    failure_reason: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.cluster1_auto_replies (
                    organization_id, brand_id, initiative_id,
                    inbound_email_message_id, email_reply_classification_id,
                    eb_inbound_reply_id,
                    status, failure_reason, metadata,
                    failed_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                        CASE WHEN %s IN ('failed','deferred_disabled')
                             THEN NOW() ELSE NULL END)
                RETURNING id
                """,
                (
                    str(ctx["organization_id"]),
                    str(ctx["brand_id"]) if ctx.get("brand_id") else None,
                    str(ctx["initiative_id"]),
                    str(ctx["email_message_id"]),
                    str(ctx["classification_id"]),
                    ctx.get("eb_reply_id"),
                    status,
                    failure_reason,
                    Jsonb(metadata or {}),
                    status,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _existing_active_auto_reply(
    classification_id: UUID,
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, status FROM business.cluster1_auto_replies
                WHERE email_reply_classification_id = %s
                  AND status IN ('queued', 'sent', 'pending_review')
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(classification_id),),
            )
            row = await cur.fetchone()
    return None if row is None else {"id": row[0], "status": row[1]}


async def _insert_outbound_email_message(
    *, ctx: dict[str, Any], composed: dict[str, Any], auto_reply_id: UUID
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages (
                    organization_id, brand_id, channel_campaign_step_id,
                    recipient_id,
                    subject_snapshot, body_snapshot, status, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    str(ctx["organization_id"]),
                    str(ctx["brand_id"]) if ctx.get("brand_id") else None,
                    str(ctx["channel_campaign_step_id"]),
                    str(ctx["recipient_id"]) if ctx.get("recipient_id") else None,
                    composed["subject"],
                    composed["body_text"],
                    Jsonb(
                        {
                            "cluster": "cluster_1",
                            "auto_reply_id": str(auto_reply_id),
                            "in_thread_for_classification": str(
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


async def _park_pending_review(
    *, ctx: dict[str, Any], composed: dict[str, Any], verdict_result: dict[str, Any]
) -> dict[str, Any]:
    ar_id = await _insert_auto_reply(
        ctx=ctx,
        status="pending_review",
        metadata={
            "verdict_blockers": verdict_result.get("blockers"),
            "verdict_score": verdict_result.get("score"),
            "verdict_rationale": verdict_result.get("rationale"),
            "verdict_backend": verdict_result.get("backend"),
            "composer_backend": composed.get("backend"),
        },
    )
    em_id = await _insert_outbound_email_message(
        ctx=ctx, composed=composed, auto_reply_id=ar_id
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster1_auto_replies
                SET outbound_email_message_id = %s,
                    rendered_subject = %s,
                    rendered_body_text = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(em_id),
                    composed["subject"],
                    composed["body_text"],
                    str(ar_id),
                ),
            )
        await conn.commit()
    await alerts.fire_alert(
        severity="warning",
        source="cluster1_auto_reply",
        summary=(
            f"Auto-reply held by verdict gate (blockers: "
            f"{', '.join(verdict_result.get('blockers') or []) or 'none'})"
        ),
        payload={
            "auto_reply_id": str(ar_id),
            "outbound_email_message_id": str(em_id),
            "verdict": verdict_result,
        },
    )
    return {
        "status": "pending_review",
        "classification_id": str(ctx["classification_id"]),
        "auto_reply_id": str(ar_id),
        "outbound_email_message_id": str(em_id),
        "verdict": verdict_result,
    }


async def _mark_failed(auto_reply_id: UUID, *, reason: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster1_auto_replies
                SET status = 'failed', failed_at = NOW(),
                    failure_reason = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (reason, str(auto_reply_id)),
            )
        await conn.commit()


async def _mark_email_message_failed(email_message_id: UUID) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.email_messages
                SET status = 'failed', updated_at = NOW()
                WHERE id = %s
                """,
                (str(email_message_id),),
            )
        await conn.commit()


async def _mark_sent(
    *,
    classification_id: UUID,
    auto_reply_id: UUID,
    outbound_email_message_id: UUID,
    eb_outbound_reply_id: int | None,
    send_mode: str | None,
    send_payload: dict[str, Any] | None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.cluster1_auto_replies
                SET status = 'sent', sent_at = NOW(),
                    outbound_email_message_id = %s,
                    eb_outbound_reply_id = %s,
                    metadata = metadata || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    str(outbound_email_message_id),
                    eb_outbound_reply_id,
                    Jsonb({"send_mode": send_mode}),
                    str(auto_reply_id),
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
                            "eb_outbound_reply_id": eb_outbound_reply_id,
                        }
                    ),
                    str(outbound_email_message_id),
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
                (str(outbound_email_message_id), str(classification_id)),
            )
        await conn.commit()


__all__ = [
    "dispatch_for_classification",
    "Cluster1AutoReplyError",
    "Cluster1AutoReplyDisabled",
    "ComposerMode",
]
