"""Admin REST surface for Customer Activation initiatives (Leg 2 + Leg 3).

Backs the Customer Activation admin page (/admin/customer-activation in
the hq-command frontend). Operator-only.

Endpoints:

  GET   /api/v1/admin/customer-activation/orgs
        Orgs with at least one brand (picker dropdown source).

  GET   /api/v1/admin/customer-activation/orgs/{org_id}/leg2-template
  PUT   /api/v1/admin/customer-activation/orgs/{org_id}/leg2-template
        Read / upsert per-org Leg 2 sequence template (multi-step).

  GET   /api/v1/admin/customer-activation/orgs/{org_id}/leg3-template
  PUT   /api/v1/admin/customer-activation/orgs/{org_id}/leg3-template
        Read / upsert per-org Leg 3 intro template (subject + body).

  GET   /api/v1/admin/customer-activation/initiatives
        List Leg 2 initiatives (parents) with brand/org/partner decoration.
        These are auto-instantiated on payment — there is no manual
        create endpoint.

  GET   /api/v1/admin/customer-activation/initiatives/{leg2_id}
        Full nested view: leg2 + leg3 + each leg's campaign tree + steps.

  PATCH /api/v1/admin/customer-activation/initiatives/{leg2_id}
        Override Leg 2 steps and/or Leg 3 step content (escape hatch
        only — default is the org-template snapshot). Draft only.

  POST  /api/v1/admin/customer-activation/initiatives/{leg2_id}/launch
        Re-launch button. Auto-instantiations launch immediately on
        payment, but if an activation was rolled back to draft (e.g.
        for content override), this re-flips it to active.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection
from app.services import customer_activation as ca_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/customer-activation",
    tags=["admin", "customer-activation"],
)


# ── Request models ────────────────────────────────────────────────────────


class Leg2StepInput(BaseModel):
    step_order: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    delay_days_from_previous: int = Field(default=0, ge=0)
    content_mode: str = Field(default="manual")
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = {"extra": "forbid"}

    def to_step_dict(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {}
        if self.subject is not None:
            cfg["subject"] = self.subject
        if self.body_text is not None:
            cfg["body_text"] = self.body_text
        if self.body_html is not None:
            cfg["body_html"] = self.body_html
        return {
            "step_order": self.step_order,
            "name": self.name,
            "delay_days_from_previous": self.delay_days_from_previous,
            "content_mode": self.content_mode,
            "channel_specific_config": cfg,
            "metadata": self.metadata,
        }


class Leg3StepInput(BaseModel):
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    model_config = {"extra": "forbid"}


class UpdateActivationRequest(BaseModel):
    leg2_steps: list[Leg2StepInput] | None = None
    leg3_step: Leg3StepInput | None = None
    model_config = {"extra": "forbid"}


class Leg3TemplateInput(BaseModel):
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    model_config = {"extra": "forbid"}


class Leg2TemplateStepInput(BaseModel):
    step_order: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=200)
    delay_days_from_previous: int = Field(default=0, ge=0)
    subject: str | None = None
    body_text: str | None = None
    body_html: str | None = None
    model_config = {"extra": "forbid"}


class Leg2TemplateInput(BaseModel):
    steps: list[Leg2TemplateStepInput]
    model_config = {"extra": "forbid"}


# ── Helpers ───────────────────────────────────────────────────────────────


def _bad_request(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": error, "message": message},
    )


def _not_found(error: str, message: str | None = None) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": error, **({"message": message} if message else {})},
    )


def _conflict(error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"error": error, "message": message},
    )


# ── Orgs (decorated picker source) ────────────────────────────────────────


@router.get("/orgs")
async def list_orgs(
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT o.id, o.name, o.slug,
                       (SELECT COUNT(*) FROM business.brands b WHERE b.organization_id = o.id) AS brand_count
                FROM business.organizations o
                ORDER BY o.name ASC
                """
            )
            rows = await cur.fetchall()
    return {
        "items": [
            {
                "id": r[0],
                "name": r[1],
                "slug": r[2],
                "brand_count": r[3],
            }
            for r in rows
        ]
    }


@router.get("/orgs/{org_id}/leg2-template")
async def get_org_leg2_template(
    org_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        steps = await ca_svc.get_org_leg2_sequence_template(org_id)
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("organization_not_found", str(exc)) from exc
    return {"steps": steps}


@router.put("/orgs/{org_id}/leg2-template")
async def put_org_leg2_template(
    org_id: UUID,
    body: Leg2TemplateInput,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        steps = await ca_svc.set_org_leg2_sequence_template(
            organization_id=org_id,
            steps=[s.model_dump() for s in body.steps],
        )
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("organization_not_found", str(exc)) from exc
    return {"steps": steps}


@router.get("/orgs/{org_id}/leg3-template")
async def get_org_leg3_template(
    org_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await ca_svc.get_org_leg3_intro_template(org_id)
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("organization_not_found", str(exc)) from exc


@router.put("/orgs/{org_id}/leg3-template")
async def put_org_leg3_template(
    org_id: UUID,
    body: Leg3TemplateInput,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await ca_svc.set_org_leg3_intro_template(
            organization_id=org_id,
            subject=body.subject,
            body_text=body.body_text,
            body_html=body.body_html,
        )
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("organization_not_found", str(exc)) from exc


# ── Initiatives (Leg 2 + Leg 3 paired) ────────────────────────────────────


@router.get("/initiatives")
async def list_initiatives(
    organization_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    items = await ca_svc.list_customer_activations(
        organization_id=organization_id,
        limit=limit,
        offset=offset,
    )
    return {"items": items}


@router.get("/initiatives/{leg2_id}")
async def get_initiative(
    leg2_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await ca_svc.get_customer_activation_full(leg2_id)
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc


@router.patch("/initiatives/{leg2_id}")
async def update_initiative(
    leg2_id: UUID,
    body: UpdateActivationRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    full = await ca_svc.get_customer_activation_full(leg2_id)
    organization_id = UUID(str(full["leg2"]["initiative"]["organization_id"]))

    if body.leg2_steps is not None:
        try:
            await ca_svc.replace_leg2_steps(
                organization_id=organization_id,
                leg2_initiative_id=leg2_id,
                steps=[s.to_step_dict() for s in body.leg2_steps],
            )
        except ca_svc.CustomerActivationValidationError as exc:
            raise _bad_request("validation_error", str(exc)) from exc
        except ca_svc.CustomerActivationNotFound as exc:
            raise _not_found("initiative_not_found", str(exc)) from exc

    if body.leg3_step is not None:
        try:
            await ca_svc.update_leg3_step(
                organization_id=organization_id,
                leg2_initiative_id=leg2_id,
                subject=body.leg3_step.subject,
                body_text=body.leg3_step.body_text,
                body_html=body.leg3_step.body_html,
            )
        except ca_svc.CustomerActivationValidationError as exc:
            raise _bad_request("validation_error", str(exc)) from exc
        except ca_svc.CustomerActivationNotFound as exc:
            raise _not_found("initiative_not_found", str(exc)) from exc

    return await ca_svc.get_customer_activation_full(leg2_id)


@router.post("/initiatives/{leg2_id}/launch")
async def launch_initiative(
    leg2_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    full = await ca_svc.get_customer_activation_full(leg2_id)
    organization_id = UUID(str(full["leg2"]["initiative"]["organization_id"]))
    try:
        return await ca_svc.launch_leg2(
            organization_id=organization_id,
            leg2_initiative_id=leg2_id,
            actor_user_id=getattr(user, "business_user_id", None),
        )
    except ca_svc.CustomerActivationLaunchPreconditionFailed as exc:
        raise _bad_request("launch_precondition_failed", str(exc)) from exc
    except ca_svc.CustomerActivationNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc


__all__ = ["router"]
