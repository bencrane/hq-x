"""Public (token-gated) proposal endpoints — prospect-facing.

No Supabase JWT here: the prospect doesn't have an account. Auth is
the unguessable ``public_token`` in the URL path. Each route fails
404 if the token doesn't resolve, never 401.

Surface:

  GET  /api/v1/proposals/by-token/{token}
       Read the proposal. First read after 'sent' flips status to
       'viewed' and stamps viewed_at.

  POST /api/v1/proposals/by-token/{token}/audience
       Prospect refines the audience via the composer; record the
       chosen DEX audience_specs id as final_data_engine_audience_id.
       Refused once checkout has been initiated.

  POST /api/v1/proposals/by-token/{token}/checkout-session
       Mint a Stripe Checkout session, persist the session id, return
       the redirect URL. Idempotent re-fires before payment surface
       a fresh session URL (Stripe sessions expire in 24h).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.services import proposals as proposals_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/proposals/by-token",
    tags=["proposals", "public"],
)


class SetAudienceRequest(BaseModel):
    data_engine_audience_id: UUID
    model_config = {"extra": "forbid"}


def _scrub_for_public(p: dict[str, Any]) -> dict[str, Any]:
    """Strip operator-internal fields before returning to the prospect.

    We keep the proposal economics, the audience id (so the partner-
    platform page can fetch the descriptor + count from DEX), and the
    lifecycle state. We drop the contract id, partner id, brand id,
    organization id, audit user ids, and Stripe payment intent id.
    """
    keep = {
        "id", "public_token", "status",
        "proposed_data_engine_audience_id", "final_data_engine_audience_id",
        "proposed_transfer_count", "proposed_price_per_transfer_cents",
        "proposed_window_days", "proposed_total_cents",
        "prospect_company_name", "prospect_contact_name", "prospect_contact_email",
        "stripe_checkout_session_id",
        "paid_at", "paid_amount_cents",
        "sent_at", "viewed_at", "expires_at",
        "created_at", "updated_at",
    }
    return {k: v for k, v in p.items() if k in keep}


@router.get("/{token}")
async def get_by_token(token: str) -> dict[str, Any]:
    try:
        proposal = await proposals_svc.get_proposal_by_token(token)
    except proposals_svc.ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        ) from exc
    return _scrub_for_public(proposal)


@router.post("/{token}/audience")
async def set_audience(token: str, body: SetAudienceRequest) -> dict[str, Any]:
    try:
        proposal = await proposals_svc.set_proposal_audience(
            token=token,
            new_data_engine_audience_id=body.data_engine_audience_id,
        )
    except proposals_svc.ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        ) from exc
    except proposals_svc.ProposalStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "state_error", "message": str(exc)},
        ) from exc
    return _scrub_for_public(proposal)


@router.post("/{token}/checkout-session")
async def initiate_checkout(token: str) -> dict[str, Any]:
    try:
        result = await proposals_svc.initiate_checkout(token=token)
    except proposals_svc.ProposalNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found"},
        ) from exc
    except proposals_svc.ProposalStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "state_error", "message": str(exc)},
        ) from exc
    return {
        "checkout_url": result["checkout_url"],
        "checkout_session_id": result["checkout_session_id"],
        "proposal": _scrub_for_public(result["proposal"]),
    }


__all__ = ["router"]
