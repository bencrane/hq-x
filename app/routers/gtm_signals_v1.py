"""Read + edit passthrough to DEX /api/v1/gtm/signals for the platform-api BFF.

Mirrors the coverage_stats_v1 / gtm_views passthrough pattern: hq-zone-api
hits us with BACKEND_X_SERVICE_TOKEN; we forward to DEX with DEX_SERVICE_TOKEN
from this app's Doppler config. DEX owns the actual ops.gtm_signals data.

Endpoints:
  GET    /api/v1/signals                       → list
  PATCH  /api/v1/signals/{slug}                → patch webhook URLs / webhook_target / is_active
  DELETE /api/v1/signals/{slug}                → hard-delete a signal row
  POST   /api/v1/signals/{slug}/fire           → spawn manual fire (returns call_id)
  GET    /api/v1/signals/fire/status/{call_id} → poll spawned fire's status
  POST   /api/v1/signals/{slug}/run-agent      → mint a gtm-agent session, seed it with the
                                                  signal definition + preview cohort, persist
                                                  the row to business.agent_runs

The /fire route is intentionally untouched — that remains the deterministic
Modal-dispatch path. /run-agent is a parallel sibling: it computes the same
cohort (via the shared cohort-preview primitive) but ships it to an
Anthropic Managed Agents session instead of an n8n webhook.
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.config import settings
from app.routers.agent_runs_v1 import (
    AgentRunEnvelope,
    _insert_agent_run,
)
from app.services import dex_client, managed_agents

router = APIRouter(prefix="/api/v1/signals", tags=["gtm-signals"])


class SignalPatchBody(BaseModel):
    """Mirrors DEX's SignalPatchRequest. Pydantic enforces type + pattern;
    extra fields rejected so a typo doesn't silently no-op."""
    webhook_test_url: str | None = Field(default=None, max_length=2000)
    webhook_prod_url: str | None = Field(default=None, max_length=2000)
    webhook_target:   str | None = Field(default=None, pattern=r"^(test|prod)$")
    is_active:        bool | None = None
    model_config = ConfigDict(extra="forbid")


class SignalFireBody(BaseModel):
    """Mirrors DEX's SignalFireRequest. Both fields optional — with neither
    set, the manual fire matches the cron's behavior exactly for this slug."""
    target: str | None = Field(default=None, pattern=r"^(test|prod)$")
    limit:  int | None = Field(default=None, ge=1, le=10000)
    model_config = ConfigDict(extra="forbid")


@router.get("")
async def list_gtm_signals(
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.list_gtm_signals()
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc


@router.patch("/{signal_slug}")
async def patch_gtm_signal(
    signal_slug: str,
    payload: SignalPatchBody,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    patch = payload.model_dump(exclude_none=True)
    try:
        return await dex_client.patch_gtm_signal(signal_slug, patch)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"type": "not_found", "message": f"signal {signal_slug!r} not found"},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc


@router.delete("/{signal_slug}")
async def delete_gtm_signal(
    signal_slug: str,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    try:
        return await dex_client.delete_gtm_signal(signal_slug)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"type": "not_found", "message": f"signal {signal_slug!r} not found"},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc


@router.post("/{signal_slug}/fire")
async def fire_gtm_signal(
    signal_slug: str,
    payload: SignalFireBody | None = None,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    """Spawns the Modal manual-fire function. Returns immediately with
    {call_id, status: 'pending', slug}; caller polls /fire/status/{call_id}
    for the result. The spawn round-trip is ~100ms so this path no longer
    hits the dex_client default 30s timeout — which was the original cause
    of split-brain 599/502 responses while the Modal compute completed and
    n8n received the payload."""
    body = (payload or SignalFireBody()).model_dump(exclude_none=True)
    try:
        return await dex_client.fire_gtm_signal(signal_slug, body)
    except dex_client.DexCallError as exc:
        # DEX returns 404 for unknown slug (validated up-front) or other
        # Modal-side errors as 502. spawn() validates almost nothing on the
        # Python side — the slug lookup happens inside the container — so
        # 422 from this endpoint is no longer expected.
        if exc.status_code == 404:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"type": "dex_call_failed", "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc


# ── /run-agent: signal → Anthropic Managed Agents session coupling ─────


class SignalRunAgentBody(BaseModel):
    """Body for POST /api/v1/signals/{slug}/run-agent.

    `user_id` is REQUIRED — the trusted caller (platform-api) supplies the
    operator's UUID after validating their Supabase JWT upstream. It's the
    `user_id` written to business.agent_runs and is the only piece of
    customer context the route needs (the signal_slug is in the path; the
    cohort is fetched server-side via DEX). Stage 2 added user_id to the
    body though the operator-facing spec only listed limit + target —
    business.agent_runs.user_id is NOT NULL and a body field is the
    least-bad place to inject it (vs JWT-parsing a token from a different
    Supabase project, vs a placeholder sentinel UUID).
    """
    limit:   int  = Field(default=50, ge=1, le=10000)
    target:  str  = Field(default="test", pattern=r"^(test|prod)$")
    user_id: UUID
    model_config = ConfigDict(extra="forbid")


def _format_initial_user_message(
    *,
    signal_slug: str,
    signal_name: str,
    preview: dict[str, Any],
) -> str:
    """Render the initial user.message: signal context + directive + rows JSON.

    Keeps the rows under a fenced ```json block so the agent treats them
    as data, not free-form prose. Includes matched_count so the agent
    knows whether the rows are the full cohort or a top-N slice.
    """
    rows = preview.get("rows") or []
    matched_count = preview.get("matched_count")
    limited = preview.get("limited")
    target = preview.get("target")
    criteria = preview.get("criteria") or {}
    spine_target = preview.get("spine_target")

    rows_json = json.dumps(rows, default=str, indent=2)
    criteria_json = json.dumps(criteria, default=str)

    return (
        f"# Signal: {signal_name}\n"
        f"slug: `{signal_slug}`\n"
        f"target: `{target}`\n"
        f"spine_target: `{spine_target}`\n"
        f"criteria: `{criteria_json}`\n"
        f"matched_count: {matched_count}\n"
        f"rows_included: {len(rows)}"
        + (f" (top-{len(rows)} by federal_action_obligation; cohort truncated)" if limited else "")
        + "\n\n"
        "## Directive\n\n"
        "Enrich this cohort. Identify the highest conviction targets based on "
        "the signal intent. Output your analysis using the `present_result` "
        "tool (e.g., using `data_table`, `ranked_list`, or `narrative_summary`).\n\n"
        "## Cohort rows\n\n"
        "```json\n"
        f"{rows_json}\n"
        "```\n"
    )


@router.post("/{signal_slug}/run-agent", response_model=AgentRunEnvelope)
async def run_agent_for_signal(
    signal_slug: str,
    payload: SignalRunAgentBody,
    _auth: None = Depends(verify_backend_x_token),
) -> AgentRunEnvelope:
    # 1. Fetch signal definition from DEX (validates the slug exists too).
    try:
        signal_def = await dex_client.get_gtm_signal(signal_slug)
    except dex_client.DexCallError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "type": "signal_not_found",
                    "message": f"signal {signal_slug!r} not found",
                },
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc

    # 2. Compile the cohort via the non-dispatching preview primitive.
    try:
        preview = await dex_client.preview_signal_cohort(
            signal_slug,
            limit=payload.limit,
            target=payload.target,
        )
    except dex_client.DexCallError as exc:
        if exc.status_code in (404, 422):
            raise HTTPException(
                status_code=exc.status_code,
                detail={"type": "dex_call_failed", "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "preview_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "preview_failed", "message": str(exc)},
        ) from exc

    # 3. Build the initial user.message and mint the Anthropic session.
    initial_message = _format_initial_user_message(
        signal_slug=signal_slug,
        signal_name=signal_def.get("signal_slug", signal_slug),
        preview=preview,
    )
    try:
        session = await managed_agents.mint_session(
            initial_message=initial_message,
            title=f"signal:{signal_slug}",
            metadata={
                "source": "gtm_signal_run_agent",
                "signal_slug": signal_slug,
                "target": payload.target,
                "limit": str(payload.limit),
                "matched_count": str(preview.get("matched_count", 0)),
            },
        )
    except managed_agents.ManagedAgentsNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"type": "managed_agents_not_configured", "message": exc.message},
        ) from exc
    except managed_agents.ManagedAgentsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "anthropic_call_failed",
                "message": exc.message,
                "upstream_status": exc.status_code,
            },
        ) from exc

    # 4. Persist the run with signal_slug filled.
    row = await _insert_agent_run(
        session_id=session["id"],
        agent_id=settings.MANAGED_AGENT_ID_GTM or "",
        environment_id=settings.MANAGED_ENVIRONMENT_ID_GTM or "",
        signal_slug=signal_slug,
        user_id=payload.user_id,
        initial_message=initial_message,
    )
    return AgentRunEnvelope(**row, anthropic=None)


@router.get("/fire/status/{call_id}")
async def fire_gtm_signal_status(
    call_id: str,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    """Non-blocking poll of a previously-spawned fire. Returns
    {status: 'pending', call_id} while running; {status: 'done', call_id,
    result} when complete; 422 if the container surfaced a per-signal error
    (empty webhook URL, etc.); 410 if the Modal call_id has expired."""
    try:
        return await dex_client.fire_gtm_signal_status(call_id)
    except dex_client.DexCallError as exc:
        # 410 / 422 / 404 — propagate verbatim so the UI sees the right code.
        if exc.status_code in (404, 410, 422):
            raise HTTPException(
                status_code=exc.status_code,
                detail={"type": "dex_call_failed", "message": str(exc)},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
    except dex_client.DexClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"type": "dex_call_failed", "message": str(exc)},
        ) from exc
