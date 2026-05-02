"""Admin surface for the GTM-pipeline agent registry + system-prompt
versioning. Mounted at /api/v1/admin/agents and gated to platform
operators.

The frontend reads a registry overview, opens an agent's editor
(current prompt + version history), and Activate / Rollback through
this surface. All Anthropic-side mutations flow through
app.services.agent_prompts which preserves the snapshot-then-overwrite
invariant.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.services import agent_prompts

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/agents", tags=["admin"])


@router.get("")
async def list_agents(
    include_deactivated: bool = False,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    rows = await agent_prompts.list_registry_rows(
        include_deactivated=include_deactivated,
    )
    return {"items": rows}


@router.get("/{slug}")
async def get_agent(
    slug: str,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    composite = await agent_prompts.get_current_for_admin(slug)
    if composite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "agent_not_registered", "agent_slug": slug},
        )
    return composite


@router.post("/{slug}/activate")
async def activate(
    slug: str,
    body: dict[str, Any] = Body(...),
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    new_prompt = body.get("system_prompt")
    if not isinstance(new_prompt, str) or not new_prompt.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "system_prompt_required"},
        )
    notes = body.get("notes")
    try:
        result = await agent_prompts.activate_prompt(
            agent_slug=slug,
            new_system_prompt=new_prompt,
            activated_by_user_id=user.business_user_id,
            notes=notes,
        )
    except agent_prompts.AgentNotRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "agent_not_registered", "message": str(exc)},
        ) from exc
    return result


@router.post("/{slug}/rollback")
async def rollback(
    slug: str,
    body: dict[str, Any] = Body(...),
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    version_index = body.get("version_index")
    if not isinstance(version_index, int):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "version_index_required"},
        )
    try:
        result = await agent_prompts.rollback_prompt(
            agent_slug=slug,
            version_index=version_index,
            activated_by_user_id=user.business_user_id,
            notes=body.get("notes"),
        )
    except agent_prompts.AgentNotRegistered as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "agent_not_registered", "message": str(exc)},
        ) from exc
    except agent_prompts.VersionNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "version_not_found", "message": str(exc)},
        ) from exc
    return result


@router.get("/{slug}/versions")
async def list_versions(
    slug: str,
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    rows = await agent_prompts.list_versions(
        slug, limit=min(max(limit, 1), 200), offset=max(offset, 0),
    )
    return {"items": rows}


@router.get("/{slug}/versions/{version_id}")
async def get_version(
    slug: str,
    version_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    row = await agent_prompts.get_version(version_id)
    if row is None or row["agent_slug"] != slug:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "version_not_found"},
        )
    return row


__all__ = ["router"]
