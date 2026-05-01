"""Admin surface for per-org doctrine. Mounted at /api/v1/admin/doctrine
and gated to platform operators.

The frontend doctrine editor is a single page (acq-eng only for v0)
with two text areas: markdown body + parameters JSON. Save POSTs here.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.services import org_doctrine

router = APIRouter(prefix="/api/v1/admin/doctrine", tags=["admin"])


@router.get("/{org_id}")
async def get_doctrine(
    org_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    row = await org_doctrine.get_for_org(org_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "doctrine_not_found", "organization_id": str(org_id)},
        )
    return row


@router.post("/{org_id}")
async def upsert_doctrine(
    org_id: UUID,
    body: dict[str, Any] = Body(...),
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    markdown = body.get("doctrine_markdown")
    parameters = body.get("parameters")
    if not isinstance(markdown, str) or not markdown.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "doctrine_markdown_required"},
        )
    if isinstance(parameters, str):
        # Frontend may send parameters as a JSON-encoded string when
        # the user is editing in a textarea. Best-effort parse.
        try:
            parameters = json.loads(parameters)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "parameters_not_json",
                    "message": str(exc),
                },
            ) from exc
    if not isinstance(parameters, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "parameters_must_be_object"},
        )
    try:
        row = await org_doctrine.upsert(
            organization_id=org_id,
            doctrine_markdown=markdown,
            parameters=parameters,
            updated_by_user_id=user.business_user_id,
        )
    except org_doctrine.DoctrineValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "doctrine_validation_error", "message": str(exc)},
        ) from exc
    return row


__all__ = ["router"]
