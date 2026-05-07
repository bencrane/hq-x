"""Admin REST surface for proposals (operator-only).

Public, token-gated endpoints for the prospect-facing flow live in
``app/routers/proposals.py``. This module is the operator's create/list/
read surface, gated on ``require_platform_operator``.
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
from app.services import proposals as proposals_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/proposals",
    tags=["admin", "proposals"],
)


class CreateProposalRequest(BaseModel):
    organization_id: UUID
    brand_id: UUID
    prospect_company_name: str = Field(min_length=1, max_length=200)
    prospect_contact_email: EmailStr | None = None
    prospect_contact_name: str | None = Field(default=None, max_length=200)
    proposed_data_engine_audience_id: UUID
    proposed_transfer_count: int = Field(gt=0)
    proposed_price_per_transfer_cents: int = Field(gt=0)
    proposed_window_days: int = Field(gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


class ProposalSendRequest(BaseModel):
    """Operator marks the proposal as sent. The actual delivery (email,
    Slack, manual share) is out of scope for this endpoint — this
    flips the status and stamps sent_at so the funnel state is honest.
    """
    model_config = {"extra": "forbid"}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_proposal(
    body: CreateProposalRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await proposals_svc.create_proposal(
            organization_id=body.organization_id,
            brand_id=body.brand_id,
            prospect_company_name=body.prospect_company_name,
            prospect_contact_email=body.prospect_contact_email,
            prospect_contact_name=body.prospect_contact_name,
            proposed_data_engine_audience_id=body.proposed_data_engine_audience_id,
            proposed_transfer_count=body.proposed_transfer_count,
            proposed_price_per_transfer_cents=body.proposed_price_per_transfer_cents,
            proposed_window_days=body.proposed_window_days,
            created_by_user_id=user.business_user_id,
            metadata=body.metadata,
        )
    except proposals_svc.ProposalValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc


@router.get("/{proposal_id}")
async def read_proposal(
    proposal_id: UUID,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await proposals_svc.get_proposal(proposal_id)
    except proposals_svc.ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        ) from exc


@router.get("")
async def list_proposals(
    organization_id: UUID | None = None,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    where: list[str] = []
    args: list[Any] = []
    if organization_id is not None:
        where.append("organization_id = %s")
        args.append(str(organization_id))
    if status_filter is not None:
        where.append("status = %s")
        args.append(status_filter)
    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    args.extend([min(max(limit, 1), 200), max(offset, 0)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, organization_id, brand_id, partner_id,
                       partner_contract_id,
                       proposed_data_engine_audience_id,
                       final_data_engine_audience_id,
                       proposed_transfer_count,
                       proposed_price_per_transfer_cents,
                       proposed_window_days, proposed_total_cents,
                       status, public_token,
                       prospect_company_name, prospect_contact_email,
                       paid_at, paid_amount_cents,
                       instantiated_leg2_initiative_id,
                       sent_at, viewed_at, expires_at,
                       created_at, updated_at
                FROM business.proposals
                {where_clause}
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    keys = [
        "id", "organization_id", "brand_id", "partner_id",
        "partner_contract_id",
        "proposed_data_engine_audience_id", "final_data_engine_audience_id",
        "proposed_transfer_count", "proposed_price_per_transfer_cents",
        "proposed_window_days", "proposed_total_cents",
        "status", "public_token",
        "prospect_company_name", "prospect_contact_email",
        "paid_at", "paid_amount_cents",
        "instantiated_leg2_initiative_id",
        "sent_at", "viewed_at", "expires_at",
        "created_at", "updated_at",
    ]
    items = [dict(zip(keys, r, strict=True)) for r in rows]
    return {"items": items, "limit": limit, "offset": offset}


@router.post("/{proposal_id}/mark-sent")
async def mark_sent(
    proposal_id: UUID,
    _body: ProposalSendRequest = ProposalSendRequest(),
    _: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Stamp sent_at + flip status to 'sent'. Idempotent; re-fires don't
    update sent_at.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.proposals
                SET status = CASE WHEN status = 'draft' THEN 'sent' ELSE status END,
                    sent_at = COALESCE(sent_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id
                """,
                (str(proposal_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail={"error": "not_found"},
                )
        await conn.commit()
    return await proposals_svc.get_proposal(proposal_id)


__all__ = ["router"]
