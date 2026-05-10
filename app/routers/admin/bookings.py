"""Admin REST surface for Cal.com bookings (operator-only).

Reads ``cal_raw_events`` and collapses to one row per ``cal_event_uid``,
latest event wins. The "current state" of a booking is the
trigger_event of its most recent row — BOOKING_CANCELLED beats
BOOKING_RESCHEDULED beats BOOKING_CREATED, in event-order, because Cal
fires them in lifecycle order.

Also exposes the post-call form action: ``POST /{uid}/proposal`` mints
a proposal from the booking + form fields and emails the prospect.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection
from app.services import proposal_emails
from app.services import proposals as proposals_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/bookings",
    tags=["admin", "bookings"],
)


@router.get("")
async def list_bookings(
    limit: int = 50,
    offset: int = 0,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    limit = min(max(limit, 1), 200)
    offset = max(offset, 0)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (cal_event_uid)
                        cal_event_uid,
                        trigger_event,
                        payload,
                        organizer_email,
                        created_at AS last_event_at
                    FROM cal_raw_events
                    WHERE cal_event_uid IS NOT NULL
                    ORDER BY cal_event_uid, created_at DESC
                )
                SELECT
                    cal_event_uid,
                    trigger_event,
                    payload->'payload'->>'title' AS title,
                    payload->'payload'->>'startTime' AS start_time,
                    payload->'payload'->>'endTime' AS end_time,
                    payload->'payload'->>'cancellationReason' AS cancellation_reason,
                    COALESCE(
                        payload->'payload'->'attendees'->0->>'name', ''
                    ) AS attendee_name,
                    COALESCE(
                        payload->'payload'->'attendees'->0->>'email', ''
                    ) AS attendee_email,
                    organizer_email,
                    last_event_at
                FROM latest
                ORDER BY (payload->'payload'->>'startTime')::timestamptz
                    DESC NULLS LAST
                LIMIT %s OFFSET %s
                """,
                (limit, offset),
            )
            rows = await cur.fetchall()

    keys = [
        "cal_event_uid",
        "trigger_event",
        "title",
        "start_time",
        "end_time",
        "cancellation_reason",
        "attendee_name",
        "attendee_email",
        "organizer_email",
        "last_event_at",
    ]
    items = [dict(zip(keys, r, strict=True)) for r in rows]
    return {"items": items, "limit": limit, "offset": offset}


@router.get("/{cal_event_uid}")
async def read_booking(
    cal_event_uid: str,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any] | None:
    """Latest event for a single booking, with the full raw payload so
    the post-call form can prefill from any field Cal sent.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    cal_event_uid,
                    trigger_event,
                    payload,
                    organizer_email,
                    attendee_emails,
                    created_at AS last_event_at
                FROM cal_raw_events
                WHERE cal_event_uid = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (cal_event_uid,),
            )
            row = await cur.fetchone()

    if row is None:
        return None
    keys = [
        "cal_event_uid",
        "trigger_event",
        "payload",
        "organizer_email",
        "attendee_emails",
        "last_event_at",
    ]
    return dict(zip(keys, row, strict=True))


# ---------------------------------------------------------------------------
# POST /{cal_event_uid}/proposal — the post-call form action.
#
# Resolves the operator's first brand under their active org, creates a
# proposal, sends the branded email to the prospect (+ ping to operator),
# flips status to 'sent'. Single round-trip from the form.
# ---------------------------------------------------------------------------


class ProposalFromBookingRequest(BaseModel):
    proposed_data_engine_audience_id: UUID
    proposed_transfer_count: int = Field(gt=0)
    proposed_price_per_transfer_cents: int = Field(gt=0)
    proposed_window_days: int = Field(gt=0)
    prospect_company_name: str = Field(min_length=1, max_length=200)
    prospect_contact_name: str | None = Field(default=None, max_length=200)
    prospect_contact_email: EmailStr
    model_config = {"extra": "forbid"}


async def _first_brand_for_org(organization_id: UUID) -> UUID | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.brands
                WHERE organization_id = %s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (str(organization_id),),
            )
            row = await cur.fetchone()
    return row[0] if row else None


@router.post("/{cal_event_uid}/proposal", status_code=status.HTTP_201_CREATED)
async def create_proposal_from_booking(
    cal_event_uid: str,
    body: ProposalFromBookingRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    if user.active_organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "no_active_organization"},
        )
    brand_id = await _first_brand_for_org(user.active_organization_id)
    if brand_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_brand",
                "message": (
                    "No brand exists under the active organization — "
                    "create one before sending proposals."
                ),
            },
        )

    try:
        proposal = await proposals_svc.create_proposal(
            organization_id=user.active_organization_id,
            brand_id=brand_id,
            prospect_company_name=body.prospect_company_name,
            prospect_contact_email=str(body.prospect_contact_email),
            prospect_contact_name=body.prospect_contact_name,
            proposed_data_engine_audience_id=body.proposed_data_engine_audience_id,
            proposed_transfer_count=body.proposed_transfer_count,
            proposed_price_per_transfer_cents=body.proposed_price_per_transfer_cents,
            proposed_window_days=body.proposed_window_days,
            created_by_user_id=user.business_user_id,
            metadata={"cal_event_uid": cal_event_uid, "source": "post_call_form"},
        )
    except proposals_svc.ProposalValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc

    # Send email + flip status to 'sent'. Send failure surfaces to operator
    # but the proposal row is preserved (idempotent — they can re-send via
    # the existing /mark-sent route).
    sent = False
    try:
        sent = await proposal_emails.send_proposal_email(proposal)
    except Exception as exc:
        logger.exception(
            "proposal email send failed proposal_id=%s", proposal["id"]
        )
        return {
            "proposal": proposal,
            "email_sent": False,
            "email_error": str(exc),
        }

    if sent:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE business.proposals
                    SET status = CASE WHEN status = 'draft' THEN 'sent' ELSE status END,
                        sent_at = COALESCE(sent_at, NOW()),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (str(proposal["id"]),),
                )
            await conn.commit()
        proposal = await proposals_svc.get_proposal(proposal["id"])

    return {"proposal": proposal, "email_sent": sent}


__all__ = ["router"]
