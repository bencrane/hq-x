"""Admin REST surface for self-prospecting GTM initiatives.

Backs the Initiative Composer admin page (/admin/self-prospecting in the
hq-command frontend). Operator-only — every route is gated by
``require_platform_operator``.

Endpoints:

  GET   /api/v1/admin/self-prospecting/audiences
        DEX audience-template list for the picker dropdown.

  GET   /api/v1/admin/self-prospecting/orgs
        Orgs eligible for self-prospecting (those with at least one brand).

  POST  /api/v1/admin/self-prospecting/initiatives
        Create initiative + campaign + channel_campaign in one shot.
        Mints a DEX audience spec from the chosen template.

  GET   /api/v1/admin/self-prospecting/initiatives
        List all self-prospecting initiatives (cross-org).

  GET   /api/v1/admin/self-prospecting/initiatives/{id}
        Full nested shape: initiative + campaign + channel_campaign + steps.

  PATCH /api/v1/admin/self-prospecting/initiatives/{id}
        Update name and/or replace the step list (delete-then-insert).

  POST  /api/v1/admin/self-prospecting/initiatives/{id}/launch
        Run preconditions and transition draft → active.
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
from app.services import dex_client
from app.services import self_prospecting as sp_svc

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/self-prospecting",
    tags=["admin", "self-prospecting"],
)


# ── Request models ─────────────────────────────────────────────────────────


class StepInput(BaseModel):
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


class CreateInitiativeRequest(BaseModel):
    organization_id: UUID
    brand_id: UUID
    name: str = Field(min_length=1, max_length=200)
    channel: str = "email"
    provider: str | None = None
    audience_spec_id: UUID | None = None
    audience_template_slug: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


class UpdateInitiativeRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    steps: list[StepInput] | None = None

    model_config = {"extra": "forbid"}


# ── Helpers ────────────────────────────────────────────────────────────────


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


# ── Audience picker ────────────────────────────────────────────────────────


@router.get("/audiences")
async def list_audiences(
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Proxy to DEX audience-templates so the composer dropdown can render.

    Returns a flat ``items`` array; the frontend doesn't need the DEX
    response envelope.
    """
    payload = await dex_client.list_audience_templates()
    items = (
        payload.get("items")
        if isinstance(payload, dict)
        else None
    ) or []
    return {"items": items}


# ── Org picker ─────────────────────────────────────────────────────────────


@router.get("/orgs")
async def list_orgs(
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Orgs that have at least one brand (so they can serve as a sending
    identity). Operator picks one of these as the initiative's owner.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT o.id, o.name, o.slug,
                       (SELECT COUNT(*) FROM business.brands b
                        WHERE b.organization_id = o.id AND b.deleted_at IS NULL)
                            AS brand_count
                FROM business.organizations o
                WHERE o.deleted_at IS NULL
                ORDER BY o.created_at DESC
                """
            )
            rows = await cur.fetchall()
    return {
        "items": [
            {
                "id": r[0],
                "name": r[1],
                "slug": r[2],
                "brand_count": int(r[3] or 0),
            }
            for r in rows
        ]
    }


@router.get("/orgs/{org_id}/brands")
async def list_org_brands(
    org_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, display_name, domain
                FROM business.brands
                WHERE organization_id = %s AND deleted_at IS NULL
                ORDER BY created_at ASC
                """,
                (str(org_id),),
            )
            rows = await cur.fetchall()
    return {
        "items": [
            {
                "id": r[0],
                "name": r[1],
                "display_name": r[2],
                "domain": r[3],
            }
            for r in rows
        ]
    }


# ── Initiative CRUD ────────────────────────────────────────────────────────


@router.post("/initiatives", status_code=status.HTTP_201_CREATED)
async def create_initiative(
    body: CreateInitiativeRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    if body.audience_spec_id is None and not body.audience_template_slug:
        raise _bad_request(
            "audience_required",
            "audience_spec_id or audience_template_slug is required",
        )
    try:
        result = await sp_svc.create_self_prospecting_initiative(
            organization_id=body.organization_id,
            brand_id=body.brand_id,
            name=body.name,
            channel=body.channel,
            provider=body.provider,
            audience_spec_id=body.audience_spec_id,
            audience_template_slug=body.audience_template_slug,
            metadata=body.metadata,
        )
    except sp_svc.SelfProspectingValidationError as exc:
        raise _bad_request("validation_failed", str(exc)) from exc
    return await sp_svc.get_self_prospecting_initiative_full(
        UUID(str(result["initiative_id"]))
    )


@router.get("/channels")
async def list_channels(
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Channels surfaced in the Initiative Composer picker. The default
    provider for each is whatever the canonical pair in the campaigns
    model is — operator can override at create time if needed.
    """
    return {
        "items": [
            {"channel": channel, "provider": provider}
            for channel, provider in sp_svc.supported_channels().items()
        ]
    }


@router.get("/initiatives")
async def list_initiatives(
    organization_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    items = await sp_svc.list_self_prospecting_initiatives(
        organization_id=organization_id, limit=limit, offset=offset
    )
    return {"items": items}


@router.get("/initiatives/{initiative_id}")
async def get_initiative(
    initiative_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        return await sp_svc.get_self_prospecting_initiative_full(initiative_id)
    except sp_svc.SelfProspectingNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc


@router.patch("/initiatives/{initiative_id}")
async def update_initiative(
    initiative_id: UUID,
    body: UpdateInitiativeRequest,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        existing = await sp_svc.get_self_prospecting_initiative_full(
            initiative_id
        )
    except sp_svc.SelfProspectingNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc

    org_id = UUID(str(existing["initiative"]["organization_id"]))

    if body.name is not None:
        try:
            await sp_svc.update_initiative_metadata(
                organization_id=org_id,
                initiative_id=initiative_id,
                name=body.name,
            )
        except sp_svc.SelfProspectingValidationError as exc:
            raise _conflict("invalid_state", str(exc)) from exc

    if body.steps is not None:
        try:
            await sp_svc.replace_steps(
                organization_id=org_id,
                initiative_id=initiative_id,
                steps=[s.to_step_dict() for s in body.steps],
            )
        except sp_svc.SelfProspectingValidationError as exc:
            raise _conflict("invalid_state", str(exc)) from exc

    return await sp_svc.get_self_prospecting_initiative_full(initiative_id)


@router.delete(
    "/initiatives/{initiative_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_initiative(
    initiative_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> None:
    try:
        existing = await sp_svc.get_self_prospecting_initiative_full(
            initiative_id
        )
    except sp_svc.SelfProspectingNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc

    org_id = UUID(str(existing["initiative"]["organization_id"]))
    try:
        await sp_svc.delete_self_prospecting_initiative(
            organization_id=org_id,
            initiative_id=initiative_id,
        )
    except sp_svc.SelfProspectingValidationError as exc:
        raise _conflict("invalid_state_for_delete", str(exc)) from exc


@router.post("/initiatives/{initiative_id}/launch")
async def launch_initiative(
    initiative_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    try:
        existing = await sp_svc.get_self_prospecting_initiative_full(
            initiative_id
        )
    except sp_svc.SelfProspectingNotFound as exc:
        raise _not_found("initiative_not_found", str(exc)) from exc

    org_id = UUID(str(existing["initiative"]["organization_id"]))
    try:
        return await sp_svc.launch_initiative(
            organization_id=org_id,
            initiative_id=initiative_id,
            actor_user_id=user.business_user_id,
        )
    except sp_svc.SelfProspectingLaunchPreconditionFailed as exc:
        raise _conflict("launch_preconditions_failed", str(exc)) from exc


__all__ = ["router"]
