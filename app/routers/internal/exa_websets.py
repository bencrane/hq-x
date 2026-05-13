"""Internal endpoint that drives an Exa Webset job to terminal state.

The Trigger.dev task ``exa.process_webset_job`` POSTs here with
``{job_id, trigger_run_id}``. We:

1. Mark the job running.
2. Create the webset on Exa, passing dex_run_id as externalId.
3. Poll Exa until the webset reaches a terminal status.
4. Fetch all items and persist raw + normalized rows to DEX via the
   /api/internal/exa/websets endpoint.
5. Mark the job succeeded or failed.

The endpoint is idempotent on terminal jobs (no-op if already terminal).
Transient infra errors re-raise so Trigger.dev's retry policy handles them.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.config import settings
from app.services import exa_client
from app.services import exa_webset_jobs as webset_jobs_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exa", tags=["internal"])

# Polling config — websets can take minutes to compute.
_POLL_INTERVAL = 15.0
_POLL_MAX_ATTEMPTS = 60  # 15 min total


def _dex_base_url() -> str:
    base = settings.DEX_BASE_URL
    if not base:
        raise RuntimeError("DEX_BASE_URL is not configured — cannot persist webset results to DEX")
    return base.rstrip("/")


def _dex_headers() -> dict[str, str]:
    key = settings.DEX_SERVICE_TOKEN
    if key is None:
        raise RuntimeError("DEX_SERVICE_TOKEN is not configured")
    secret = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
    return {
        "Authorization": f"Bearer {secret}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _persist_to_dex(
    *,
    dex_run_id: str,
    exa_webset_id: str,
    description: str,
    request_config: dict[str, Any],
    items: list[dict[str, Any]],
    exa_status: str,
) -> None:
    """POST the raw items to DEX for persistence into exa.* tables."""
    payload = {
        "dex_run_id": dex_run_id,
        "exa_webset_id": exa_webset_id,
        "description": description,
        "request_config": request_config,
        "items": items,
        "exa_status": exa_status,
    }
    url = f"{_dex_base_url()}/api/internal/exa/websets"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(url, json=payload, headers=_dex_headers())
    if resp.status_code >= 400:
        raise RuntimeError(
            f"DEX /api/internal/exa/websets returned {resp.status_code}: {resp.text[:500]}"
        )


@router.post(
    "/websets/{job_id}/process",
    dependencies=[Depends(verify_trigger_secret)],
)
async def process_exa_webset(
    job_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    """Trigger.dev callback — drive one webset job to terminal state."""
    job = await webset_jobs_svc.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "webset_job_not_found"},
        )

    if job["status"] in ("succeeded", "failed"):
        return {
            "job_id": str(job_id),
            "status": job["status"],
            "skipped": True,
            "reason": "job_already_terminal",
        }

    trigger_run_id = body.get("trigger_run_id")
    await webset_jobs_svc.mark_running(
        job_id, trigger_run_id if isinstance(trigger_run_id, str) else None
    )

    dex_run_id = str(job["dex_run_id"])
    count = job["count"]
    criteria = job["criteria"]
    enrichments = job["enrichments"]
    entity = job["entity"]
    description = job["description"]
    request_config = {
        "count": count,
        "criteria": criteria,
        "enrichments": enrichments,
        "entity": entity,
    }

    # Step 1 — create the webset on Exa.
    try:
        create_resp = await exa_client.create_webset(
            count=count,
            criteria=criteria,
            enrichments=enrichments,
            entity=entity,
            external_id=dex_run_id,
        )
    except exa_client.ExaNotConfiguredError as exc:
        await webset_jobs_svc.mark_failed(
            job_id, error={"reason": "exa_not_configured", "message": str(exc)}
        )
        return {"job_id": str(job_id), "status": "failed", "error": "exa_not_configured"}
    except exa_client.ExaCallError as exc:
        await webset_jobs_svc.mark_failed(
            job_id,
            error={
                "reason": "exa_create_failed",
                "exa_status_code": getattr(exc, "status_code", None),
                "message": str(exc)[:1000],
            },
        )
        return {"job_id": str(job_id), "status": "failed", "error": "exa_create_failed"}

    exa_webset_id = (
        create_resp.get("id") or create_resp.get("websetId") or create_resp.get("webset_id")
    )
    if not exa_webset_id:
        await webset_jobs_svc.mark_failed(
            job_id,
            error={
                "reason": "exa_create_no_id",
                "message": f"Exa did not return a webset id: {create_resp!r}"[:1000],
            },
        )
        return {"job_id": str(job_id), "status": "failed", "error": "exa_create_no_id"}

    await webset_jobs_svc.append_history(
        job_id,
        {"kind": "created_on_exa", "exa_webset_id": exa_webset_id},
    )

    # Step 2 — poll until the webset is terminal.
    exa_status = ""
    for attempt in range(_POLL_MAX_ATTEMPTS):
        try:
            status_resp = await exa_client.get_webset(webset_id=exa_webset_id)
        except exa_client.ExaCallError as exc:
            logger.warning(
                "get_webset poll attempt %d failed: %s", attempt + 1, exc
            )
            await asyncio.sleep(_POLL_INTERVAL)
            continue

        exa_status = (status_resp.get("status") or "").lower()
        if exa_status in ("completed", "succeeded", "success"):
            break
        if exa_status in ("failed", "error", "cancelled", "canceled"):
            await webset_jobs_svc.mark_failed(
                job_id,
                error={
                    "reason": "exa_webset_failed",
                    "exa_status": exa_status,
                    "exa_webset_id": exa_webset_id,
                },
            )
            return {
                "job_id": str(job_id),
                "status": "failed",
                "exa_webset_id": exa_webset_id,
                "error": f"exa_webset_{exa_status}",
            }
        await asyncio.sleep(_POLL_INTERVAL)
    else:
        await webset_jobs_svc.mark_failed(
            job_id,
            error={
                "reason": "exa_poll_timeout",
                "exa_webset_id": exa_webset_id,
                "attempts": _POLL_MAX_ATTEMPTS,
            },
        )
        return {"job_id": str(job_id), "status": "failed", "error": "exa_poll_timeout"}

    # Step 3 — fetch all items.
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        try:
            page = await exa_client.list_webset_items(
                webset_id=exa_webset_id,
                cursor=cursor,
                count=count,
            )
        except exa_client.ExaCallError as exc:
            logger.error("list_webset_items failed: %s", exc)
            break
        page_items = page.get("results") or page.get("items") or []
        items.extend(page_items)
        cursor = page.get("nextCursor") or page.get("next_cursor")
        if not cursor:
            break

    # Step 4 — persist to DEX.
    try:
        await _persist_to_dex(
            dex_run_id=dex_run_id,
            exa_webset_id=exa_webset_id,
            description=description,
            request_config=request_config,
            items=items,
            exa_status=exa_status,
        )
    except Exception as exc:
        # Transient persistence failure — let Trigger retry.
        await webset_jobs_svc.append_history(
            job_id, {"kind": "persist_error", "error": str(exc)[:500]}
        )
        raise

    # Step 5 — mark succeeded.
    await webset_jobs_svc.mark_succeeded(
        job_id,
        exa_webset_id=exa_webset_id,
        result_summary={
            "exa_webset_id": exa_webset_id,
            "dex_run_id": dex_run_id,
            "item_count": len(items),
        },
    )

    return {
        "job_id": str(job_id),
        "status": "succeeded",
        "exa_webset_id": exa_webset_id,
        "dex_run_id": dex_run_id,
        "item_count": len(items),
    }


__all__ = ["router"]
