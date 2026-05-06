"""Admin REST surface for JSearch scheduled ingests.

Backs the JSearch admin tile (/admin/jsearch in hq-command). Operator-only —
every route is gated by ``require_platform_operator``. Schedule data is
owned by hq-x; data lands in DEX via /internal/jsearch/run-scheduled-ingest
on each Trigger fire.

Endpoints:
  POST   /api/v1/admin/jsearch/schedules          create
  GET    /api/v1/admin/jsearch/schedules          list (most recent first)
  GET    /api/v1/admin/jsearch/schedules/{id}     get one
  DELETE /api/v1/admin/jsearch/schedules/{id}     delete
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.services import jsearch_schedules
from app.services.trigger_dev_client import TriggerDevError

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/v1/admin/jsearch",
    tags=["admin-jsearch"],
)


DatePosted = Literal["all", "today", "3days", "week", "month"]


class JSearchScheduleCreateRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500)
    cron_expr: str = Field(..., min_length=1, max_length=200,
                           description="Standard 5-field cron, e.g. '0 9 * * *'.")
    timezone: str = Field("UTC", min_length=1, max_length=64)
    label: str | None = Field(None, max_length=200)
    num_pages: int = Field(1, ge=1, le=20)
    country: str = Field("us", min_length=2, max_length=2)
    language: str | None = "en"
    date_posted: DatePosted | None = None
    work_from_home: bool = False
    employment_types: str | None = None
    job_requirements: str | None = None
    radius: float | None = Field(None, gt=0)
    exclude_job_publishers: str | None = None


@router.post("/schedules")
async def create_schedule(
    payload: JSearchScheduleCreateRequest,
    _: UserContext = Depends(require_platform_operator),
) -> dict:
    try:
        return await jsearch_schedules.create_schedule(
            query=payload.query,
            cron_expr=payload.cron_expr,
            timezone=payload.timezone,
            label=payload.label,
            num_pages=payload.num_pages,
            country=payload.country,
            language=payload.language,
            date_posted=payload.date_posted,
            work_from_home=payload.work_from_home,
            employment_types=payload.employment_types,
            job_requirements=payload.job_requirements,
            radius=payload.radius,
            exclude_job_publishers=payload.exclude_job_publishers,
        )
    except TriggerDevError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": "Trigger.dev rejected schedule",
                "trigger_error": exc.body,
            },
        )
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        )


@router.get("/schedules")
async def list_schedules(
    active_only: bool = False,
    _: UserContext = Depends(require_platform_operator),
) -> dict:
    rows = await jsearch_schedules.list_schedules(active_only=active_only)
    return {"data": rows}


@router.get("/schedules/{schedule_id}")
async def get_schedule(
    schedule_id: str,
    _: UserContext = Depends(require_platform_operator),
) -> dict:
    schedule = await jsearch_schedules.get_schedule(schedule_id)
    if schedule is None:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"data": schedule}


@router.delete("/schedules/{schedule_id}")
async def delete_schedule(
    schedule_id: str,
    _: UserContext = Depends(require_platform_operator),
) -> dict:
    deleted = await jsearch_schedules.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule not found")
    return {"data": {"schedule_id": schedule_id, "deleted": True}}
