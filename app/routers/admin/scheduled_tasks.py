"""Admin REST surface for the Trigger.dev scheduled-task control plane.

Backs the hq-zone "Scheduled Tasks" page (platform-app, via the platform-api BFF).
The BFF presents the static service token as its own identity; gated here by
``verify_backend_x_token``. The operator gate is platform-api's ``requireUser``
(only the operator authenticates to platform-app). The PATCH actor (disabled_by)
arrives as ``user_id`` injected by the BFF (identity="body") from the JWT sub.

  GET   /api/v1/admin/scheduled-tasks         list every registered schedule
                                              with computed green/red/grey status
  PATCH /api/v1/admin/scheduled-tasks/{id}    toggle enable/disable + retag
                                              priority / SLA / notes
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.services import scheduled_tasks

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/scheduled-tasks", tags=["admin-scheduled-tasks"])


class ScheduledTaskPatch(BaseModel):
    """All fields optional — only the provided ones are updated."""

    is_enabled: bool | None = None
    priority: int | None = Field(None, ge=1, le=3)
    is_sla_critical: bool | None = None
    notes: str | None = Field(None, max_length=2000)
    reason: str | None = Field(None, max_length=500, description="Audit note when disabling.")
    # Injected by the platform-api BFF (identity="body") from the validated JWT
    # `sub` — used as the disabled_by actor. The browser never sends it.
    user_id: str | None = Field(None, max_length=200)

    model_config = ConfigDict(extra="forbid")


@router.get("")
async def list_scheduled_tasks(
    _: None = Depends(verify_backend_x_token),
) -> dict:
    """Every registered schedule + computed status + roll-up summary."""
    return await scheduled_tasks.list_with_status()


@router.patch("/{task_id}")
async def patch_scheduled_task(
    task_id: str,
    payload: ScheduledTaskPatch,
    _: None = Depends(verify_backend_x_token),
) -> dict:
    if all(
        v is None
        for v in (payload.is_enabled, payload.priority, payload.is_sla_critical, payload.notes)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "no_fields", "message": "Provide at least one field to update."},
        )
    row = await scheduled_tasks.update_task(
        task_id,
        is_enabled=payload.is_enabled,
        priority=payload.priority,
        is_sla_critical=payload.is_sla_critical,
        notes=payload.notes,
        actor=payload.user_id,
        reason=payload.reason,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "message": f"task {task_id!r} not registered"},
        )
    return {"data": row}
