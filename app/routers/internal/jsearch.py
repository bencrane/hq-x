"""Internal endpoint that drives one JSearch scheduled-ingest fire.

The Trigger.dev task ``jsearch-scheduled-ingest`` POSTs here with
``{schedule_id, trigger_run_id}``. We:

1. Look up the schedule's params from hq-x's business.jsearch_search_schedules.
2. POST to DEX (/api/v1/jsearch/ingest) to actually run the search +
   write rows to entities.source_jsearch_search + ops.jsearch_search_ingest_runs.
3. Advance hq-x's last_fired_at + last_run_id on the schedule row.
4. Return the ingest result so Trigger's run history shows counts.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.config import settings
from app.services import jsearch_schedules

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jsearch", tags=["internal"])


def _dex_base_url() -> str:
    base = settings.DEX_BASE_URL
    if not base:
        raise RuntimeError(
            "DEX_BASE_URL not configured — cannot run scheduled JSearch ingests."
        )
    return base.rstrip("/")


def _dex_headers() -> dict[str, str]:
    key = settings.DEX_SERVICE_TOKEN
    if key is None:
        raise RuntimeError("DEX_SERVICE_TOKEN not configured.")
    secret = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


@router.post(
    "/run-scheduled-ingest",
    dependencies=[Depends(verify_trigger_secret)],
)
async def run_scheduled_ingest(
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Trigger.dev callback — fire one ingest off a stored schedule."""
    schedule_id = body.get("schedule_id")
    trigger_run_id = body.get("trigger_run_id")
    if not isinstance(schedule_id, str) or not schedule_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "schedule_id required"},
        )

    schedule = await jsearch_schedules.get_schedule(schedule_id)
    if schedule is None:
        logger.warning("jsearch schedule %s not found in hq-x DB", schedule_id)
        return {
            "schedule_id": schedule_id,
            "status": "skipped",
            "skipped": True,
            "reason": "schedule_not_found",
        }

    ingest_body: dict[str, Any] = {
        "query": schedule["query"],
        "num_pages": schedule["num_pages"],
        "page": 1,
        "country": schedule["country"],
        "schedule_id": schedule_id,
        "task_id": trigger_run_id,
    }
    for opt_key in (
        "language",
        "date_posted",
        "employment_types",
        "job_requirements",
        "exclude_job_publishers",
    ):
        v = schedule.get(opt_key)
        if v is not None and v != "":
            ingest_body[opt_key] = v
    if schedule.get("work_from_home"):
        ingest_body["work_from_home"] = True
    if schedule.get("radius") is not None:
        ingest_body["radius"] = schedule["radius"]

    async with httpx.AsyncClient(timeout=300.0) as client:
        ingest = await client.post(
            f"{_dex_base_url()}/api/v1/jsearch/ingest",
            json=ingest_body,
            headers=_dex_headers(),
        )
    if ingest.status_code >= 400:
        raise RuntimeError(
            f"DEX ingest returned {ingest.status_code}: {ingest.text[:500]}"
        )
    result = ingest.json().get("data") or {}

    # Advance the schedule's last-fired/last-run pointers in hq-x DB.
    try:
        await jsearch_schedules.update_last_fired(schedule_id, result.get("run_id"))
    except Exception:  # noqa: BLE001
        logger.exception("failed to advance last_fired_at for %s", schedule_id)
        # Don't fail the run — the data already landed in DEX.

    return {
        "schedule_id": schedule_id,
        "status": result.get("status", "succeeded"),
        "rows_seen": result.get("rows_seen"),
        "rows_upserted": result.get("rows_upserted"),
        "credits_used": result.get("credits_used"),
        "run_id": result.get("run_id"),
        "error": result.get("error"),
    }
