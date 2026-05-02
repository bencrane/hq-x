"""Internal endpoint that drives an Exa research job to terminal state.

The Trigger.dev task ``exa.process_research_job`` POSTs here with
``{job_id, trigger_run_id}``. We:

1. Mark the job running.
2. Dispatch to the right Exa client method based on ``endpoint``.
3. Persist the raw payload to whichever DB ``destination`` says.
4. Mark the job succeeded (with a result_ref) or failed.

The endpoint is idempotent on terminal jobs (no-ops if the job is
already terminal). Transient infra errors re-raise so Trigger.dev's
task retry policy can pick them up; deterministic Exa failures persist
status='failed' without re-raising.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, status

from app.auth.trigger_secret import verify_trigger_secret
from app.services import exa_call_persistence, exa_client
from app.services import exa_research_jobs as exa_jobs_svc
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/exa", tags=["internal"])


_ENDPOINT_DISPATCH = {
    "search": exa_client.search,
    "contents": exa_client.contents,
    "find_similar": exa_client.find_similar,
    "research": exa_client.research,
    "answer": exa_client.answer,
}


def _strip_meta(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pop ``_meta`` out of an Exa-client response so we can persist the
    raw body unchanged plus stamp the audit columns from meta."""
    if not isinstance(payload, dict):
        return payload, {}
    meta = payload.pop("_meta", {}) if "_meta" in payload else {}
    payload.pop("_research_id", None)
    return payload, meta


async def _persist(
    *,
    destination: str,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status_value: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> UUID:
    common = dict(
        job_id=job_id,
        endpoint=endpoint,
        objective=objective,
        objective_ref=objective_ref,
        request_payload=request_payload,
        response_payload=response_payload,
        status=status_value,
        error=error,
        exa_request_id=exa_request_id,
        cost_dollars=cost_dollars,
        duration_ms=duration_ms,
    )
    if destination == "hqx":
        return await exa_call_persistence.persist_exa_call_local(**common)
    if destination == "dex":
        return await exa_call_persistence.persist_exa_call_to_dex(**common)
    raise ValueError(f"unknown destination: {destination!r}")


@router.post(
    "/jobs/{job_id}/process",
    dependencies=[Depends(verify_trigger_secret)],
)
async def process_exa_job(
    job_id: UUID,
    body: dict[str, Any] = Body(default_factory=dict),
) -> dict[str, Any]:
    job = await exa_jobs_svc.get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "job_not_found"},
        )

    if job["status"] in ("succeeded", "failed", "cancelled", "dead_lettered"):
        return {
            "job_id": str(job_id),
            "status": job["status"],
            "skipped": True,
            "reason": "job_already_terminal",
            "result_ref": job.get("result_ref"),
        }

    trigger_run_id = body.get("trigger_run_id")
    await exa_jobs_svc.mark_running(
        job_id, trigger_run_id if isinstance(trigger_run_id, str) else None
    )

    endpoint = job["endpoint"]
    destination = job["destination"]
    objective = job["objective"]
    objective_ref = job.get("objective_ref")
    request_payload = job["request_payload"] or {}

    dispatch = _ENDPOINT_DISPATCH.get(endpoint)
    if dispatch is None:
        await exa_jobs_svc.mark_failed(
            job_id, error={"reason": "unknown_endpoint", "endpoint": endpoint}
        )
        return {"job_id": str(job_id), "status": "failed", "error": "unknown_endpoint"}

    try:
        raw = await dispatch(**request_payload)
    except exa_client.ExaNotConfiguredError as exc:
        await exa_jobs_svc.mark_failed(
            job_id, error={"reason": "exa_not_configured", "message": str(exc)}
        )
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": "exa_not_configured",
        }
    except exa_client.ExaCallError as exc:
        # Persist a 'failed' row in the destination DB so failures are
        # visible there too — not just inside the orchestration table.
        try:
            exa_call_id = await _persist(
                destination=destination,
                job_id=job_id,
                endpoint=endpoint,
                objective=objective,
                objective_ref=objective_ref,
                request_payload=request_payload,
                response_payload=None,
                status_value="failed",
                error=str(exc)[:2000],
                exa_request_id=None,
                cost_dollars=None,
                duration_ms=None,
            )
        except exa_call_persistence.ExaCallPersistenceError as persist_exc:
            await exa_jobs_svc.mark_failed(
                job_id,
                error={
                    "reason": "exa_call_failed_and_persist_failed",
                    "exa_status_code": getattr(exc, "status_code", None),
                    "exa_error": str(exc)[:1000],
                    "persist_error": str(persist_exc)[:500],
                },
            )
            return {
                "job_id": str(job_id),
                "status": "failed",
                "error": "exa_call_failed_and_persist_failed",
            }
        await exa_jobs_svc.mark_failed(
            job_id,
            error={
                "reason": "exa_call_failed",
                "exa_status_code": getattr(exc, "status_code", None),
                "message": str(exc)[:1000],
                "exa_call_id": str(exa_call_id),
            },
        )
        return {
            "job_id": str(job_id),
            "status": "failed",
            "result_ref": f"{destination}://exa.exa_calls/{exa_call_id}",
            "error": "exa_call_failed",
        }

    response_payload, meta = _strip_meta(raw if isinstance(raw, dict) else {})
    try:
        exa_call_id = await _persist(
            destination=destination,
            job_id=job_id,
            endpoint=endpoint,
            objective=objective,
            objective_ref=objective_ref,
            request_payload=request_payload,
            response_payload=response_payload,
            status_value="succeeded",
            error=None,
            exa_request_id=meta.get("exa_request_id"),
            cost_dollars=meta.get("cost_dollars"),
            duration_ms=meta.get("duration_ms"),
        )
    except exa_call_persistence.ExaCallPersistenceError as exc:
        await exa_jobs_svc.mark_failed(
            job_id,
            error={"reason": "persist_failed", "message": str(exc)[:1000]},
        )
        return {
            "job_id": str(job_id),
            "status": "failed",
            "error": "persist_failed",
        }
    except Exception as exc:  # transient — let Trigger retry
        await exa_jobs_svc.append_history(
            job_id, {"kind": "retry", "error": str(exc)[:500]}
        )
        raise

    result_ref = f"{destination}://exa.exa_calls/{exa_call_id}"
    await exa_jobs_svc.mark_succeeded(job_id, result_ref)

    # Dispatch post-processing for objectives that have a downstream
    # owner (e.g. strategic_context_research → flips its parent gtm
    # initiative to `strategic_research_ready`). Failures here are
    # observability-only — the exa job itself stays succeeded.
    try:
        await _post_process_by_objective(
            objective=objective,
            objective_ref=objective_ref,
            result_ref=result_ref,
        )
    except Exception:  # pragma: no cover — observability only
        logger.exception(
            "post_process_by_objective failed",
            extra={
                "job_id": str(job_id),
                "objective": objective,
                "objective_ref": objective_ref,
            },
        )

    return {
        "job_id": str(job_id),
        "status": "succeeded",
        "result_ref": result_ref,
    }


# ---------------------------------------------------------------------------
# Post-process dispatch
# ---------------------------------------------------------------------------


async def _post_process_by_objective(
    *,
    objective: str,
    objective_ref: str | None,
    result_ref: str,
) -> None:
    """Hook for objective-specific work that runs after an exa job
    succeeds. Today: strategic-context-research writes back to its
    parent gtm initiative."""
    if objective != "strategic_context_research":
        return
    if not objective_ref or not objective_ref.startswith("initiative:"):
        logger.warning(
            "strategic_context_research succeeded but objective_ref %r "
            "is not initiative:<uuid>; skipping post-process",
            objective_ref,
        )
        return
    initiative_id_str = objective_ref.split(":", 1)[1]
    try:
        initiative_id = UUID(initiative_id_str)
    except ValueError:
        logger.warning(
            "strategic_context_research objective_ref %r could not be parsed",
            objective_ref,
        )
        return
    await gtm_svc.set_strategic_context_research_ref(initiative_id, result_ref)
    try:
        await gtm_svc.transition_status(
            initiative_id,
            new_status="strategic_research_ready",
            history_event={
                "kind": "transition",
                "trigger": "post_process_by_objective",
                "objective": "strategic_context_research",
                "result_ref": result_ref,
            },
        )
    except gtm_svc.InvalidStatusTransition:
        # Already past this state (replay). Log and move on.
        logger.info(
            "initiative %s already past strategic_research_ready; "
            "result_ref refreshed",
            initiative_id,
        )
    except gtm_svc.GtmInitiativeNotFound:
        logger.warning(
            "gtm initiative %s not found while post-processing exa job",
            initiative_id,
        )


__all__ = ["router"]
