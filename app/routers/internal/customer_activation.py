"""Internal customer-activation endpoints.

The fire-intro endpoint is the seam between "positive reply detected"
(however the classifier did it — manual, agent, EmailBison rule) and
"intro email goes out." It's invoked by:

  * The `intro.dispatch_pending_positives` Trigger.dev schedule (off in
    v1; you can also curl this endpoint directly to test).
  * The `intro.send_intro` Trigger.dev task, on a per-recipient basis.

Both call shapes are accepted: this endpoint mints the intro email_messages
row + marks the classification fired, and returns the rendered preview.

The actual EmailBison send happens inside the `intro.send_intro` Trigger
task after this endpoint returns — separation lets the task own retry
semantics without putting EB calls inline in the request handler.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.trigger_secret import verify_trigger_secret
from app.services import customer_activation as ca_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/customer-activation", tags=["internal"])


class FireIntroRequest(BaseModel):
    leg2_initiative_id: UUID
    email_message_id: UUID
    source: str = Field(default="manual", max_length=64)
    model_config = {"extra": "forbid"}


class InstantiateForPaymentRequest(BaseModel):
    organization_id: UUID
    brand_id: UUID
    partner_id: UUID
    partner_contract_id: UUID
    data_engine_audience_id: UUID
    name: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}


@router.post(
    "/instantiate-for-payment",
    dependencies=[Depends(verify_trigger_secret)],
)
async def instantiate_for_payment(
    body: InstantiateForPaymentRequest,
) -> dict[str, Any]:
    """Auto-instantiate the Customer Activation pair from the org's
    pre-authored Leg 2 sequence + Leg 3 intro templates.

    Called by the upstream payment / reservation flow when a demand-side
    partner pays for an audience reservation. Refuses with 400 if the
    org templates aren't authored yet.

    Manually testable today via curl with a TRIGGER_SHARED_SECRET bearer:
        curl -X POST -H "Authorization: Bearer <secret>" \\
             -H "Content-Type: application/json" \\
             -d '{"organization_id":"...", "brand_id":"...", ...}' \\
             https://<hq-x>/internal/customer-activation/instantiate-for-payment
    """
    try:
        return await ca_svc.instantiate_for_payment(
            organization_id=body.organization_id,
            brand_id=body.brand_id,
            partner_id=body.partner_id,
            partner_contract_id=body.partner_contract_id,
            data_engine_audience_id=body.data_engine_audience_id,
            name=body.name,
            metadata=body.metadata,
        )
    except ca_svc.CustomerActivationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc
    except ca_svc.CustomerActivationLaunchPreconditionFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "launch_precondition_failed", "message": str(exc)},
        ) from exc
    except ca_svc.CustomerActivationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc


@router.post(
    "/fire-intro",
    dependencies=[Depends(verify_trigger_secret)],
)
async def fire_intro(body: FireIntroRequest) -> dict[str, Any]:
    try:
        return await ca_svc.fire_intro(
            leg2_initiative_id=body.leg2_initiative_id,
            email_message_id=body.email_message_id,
            source=body.source,
        )
    except ca_svc.CustomerActivationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "validation_error", "message": str(exc)},
        ) from exc
    except ca_svc.CustomerActivationNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": str(exc)},
        ) from exc


class ListPendingRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)
    model_config = {"extra": "forbid"}


@router.post(
    "/pending-positive-replies",
    dependencies=[Depends(verify_trigger_secret)],
)
async def list_pending(
    body: ListPendingRequest = Body(default_factory=ListPendingRequest),
) -> dict[str, Any]:
    """POST (not GET) so it composes with callHqx, which is POST-only."""
    items = await ca_svc.list_pending_positive_replies(limit=body.limit)
    return {"items": items, "limit": body.limit}


__all__ = ["router"]
