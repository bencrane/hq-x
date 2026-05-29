"""Anthropic Managed Agents runs — public surface called by platform-api.

Mounts at ``/api/v1/agent-runs``. Auth is the same service-token pattern
as ``gtm_signals_v1`` (``verify_backend_x_token``); the caller
(``platform-api``) has already validated the customer's Supabase JWT
and forwards the resolved ``user_id`` in the request body for the
POST. The other routes are session-scoped (the session_id is the
secret-ish handle) and proxy verbatim to Anthropic.

Endpoints:
  POST   /                       → mint session against gtm-agent + write ledger row
  GET    /                       → list the caller's runs (sidebar history)
  GET    /{session_id}/stream    → SSE pipe-through of Anthropic's event stream
  GET    /{session_id}/events    → list events history (for reconnect backfill)
  POST   /{session_id}/events    → append a user-domain event (interrupt, steer, confirm)
  GET    /{session_id}           → status + cumulative usage (Anthropic) merged with ledger row
  PATCH  /{session_id}           → rename (set the operator-facing title)
  DELETE /{session_id}           → remove the run from the ledger

The streaming response is a raw byte pipe — platform-api re-emits it
unchanged to platform-app, which parses ``data: {...}\\n\\n`` frames in
the browser via fetch + ReadableStream.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.service_token import verify_backend_x_token
from app.config import settings
from app.db import get_db_connection
from app.services import managed_agents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agent-runs", tags=["agent-runs"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateAgentRunBody(BaseModel):
    """Body for POST /api/v1/agent-runs.

    `user_id` is supplied by the trusted caller (platform-api) which has
    already validated the customer's Supabase JWT upstream. `signal_slug`
    is optional — if set, the run is attributed to that signal in the
    ledger for the "runs per signal" admin view.
    """
    initial_message: str = Field(min_length=1, max_length=64_000)
    user_id: UUID
    signal_slug: str | None = Field(default=None, max_length=200)
    vault_ids: list[str] | None = None
    title: str | None = Field(default=None, max_length=200)
    metadata: dict[str, str] | None = None

    model_config = ConfigDict(extra="forbid")


class AgentRunEnvelope(BaseModel):
    """Response shape for POST / and GET /{session_id}."""
    session_id: str
    agent_id: str
    environment_id: str
    signal_slug: str | None
    user_id: UUID
    status: str
    stop_reason: dict[str, Any] | None
    usage: dict[str, Any] | None
    created_at: str
    updated_at: str
    title: str | None = None
    # Live Anthropic-side fields when retrieve_session was called.
    anthropic: dict[str, Any] | None = None


class SendEventsBody(BaseModel):
    """Body for POST /{session_id}/events — forwards the events array
    verbatim to Anthropic. Validates only that the array is non-empty;
    Anthropic enforces per-event schema."""
    events: list[dict[str, Any]] = Field(min_length=1)
    model_config = ConfigDict(extra="forbid")


class RenameAgentRunBody(BaseModel):
    """Body for PATCH /{session_id} — set the operator-facing title."""
    title: str = Field(min_length=1, max_length=200)
    model_config = ConfigDict(extra="forbid")


class AgentRunListItem(BaseModel):
    """One row in the session-history list (sidebar)."""
    session_id: str
    title: str | None
    signal_slug: str | None
    status: str
    created_at: str
    updated_at: str


class AgentRunListResponse(BaseModel):
    data: list[AgentRunListItem]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


_INSERT_AGENT_RUN_SQL = """
    INSERT INTO business.agent_runs (
        session_id, agent_id, environment_id, signal_slug, user_id,
        initial_message, status, title
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    RETURNING session_id, agent_id, environment_id, signal_slug, user_id,
              status, stop_reason, usage, created_at, updated_at, title
"""

_SELECT_AGENT_RUN_SQL = """
    SELECT session_id, agent_id, environment_id, signal_slug, user_id,
           status, stop_reason, usage, created_at, updated_at, title
    FROM business.agent_runs
    WHERE session_id = %s
"""

_LIST_AGENT_RUNS_SQL = """
    SELECT session_id,
           COALESCE(NULLIF(title, ''), LEFT(initial_message, 80)) AS title,
           signal_slug, status, created_at, updated_at
    FROM business.agent_runs
    WHERE user_id = %s
    ORDER BY updated_at DESC
    LIMIT %s
"""

_RENAME_AGENT_RUN_SQL = """
    UPDATE business.agent_runs
    SET title = %s
    WHERE session_id = %s
    RETURNING session_id, agent_id, environment_id, signal_slug, user_id,
              status, stop_reason, usage, created_at, updated_at, title
"""

_DELETE_AGENT_RUN_SQL = "DELETE FROM business.agent_runs WHERE session_id = %s"


async def _insert_agent_run(
    *,
    session_id: str,
    agent_id: str,
    environment_id: str,
    signal_slug: str | None,
    user_id: UUID,
    initial_message: str,
    title: str | None,
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                _INSERT_AGENT_RUN_SQL,
                (
                    session_id,
                    agent_id,
                    environment_id,
                    signal_slug,
                    str(user_id),
                    initial_message,
                    "starting",
                    title,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_dict(row, cur_description=("session_id", "agent_id", "environment_id",
                                              "signal_slug", "user_id", "status",
                                              "stop_reason", "usage", "created_at",
                                              "updated_at", "title"))


async def _fetch_agent_run(session_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_AGENT_RUN_SQL, (session_id,))
            row = await cur.fetchone()
    if row is None:
        return None
    return _row_to_dict(row, cur_description=("session_id", "agent_id", "environment_id",
                                              "signal_slug", "user_id", "status",
                                              "stop_reason", "usage", "created_at",
                                              "updated_at", "title"))


def _row_to_dict(row: tuple, *, cur_description: tuple[str, ...]) -> dict[str, Any]:
    out = dict(zip(cur_description, row, strict=True))
    # Normalize datetimes / UUIDs to ISO / str for JSON serialization.
    if out.get("user_id") is not None and not isinstance(out["user_id"], str):
        out["user_id"] = str(out["user_id"])
    for ts_key in ("created_at", "updated_at"):
        v = out.get(ts_key)
        if v is not None and not isinstance(v, str):
            out[ts_key] = v.isoformat()
    return out


async def _list_agent_runs(user_id: UUID, limit: int) -> list[dict[str, Any]]:
    cols = ("session_id", "title", "signal_slug", "status", "created_at", "updated_at")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_LIST_AGENT_RUNS_SQL, (str(user_id), limit))
            rows = await cur.fetchall()
    return [_row_to_dict(r, cur_description=cols) for r in rows]


async def _rename_agent_run(session_id: str, title: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_RENAME_AGENT_RUN_SQL, (title, session_id))
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        return None
    return _row_to_dict(row, cur_description=("session_id", "agent_id", "environment_id",
                                              "signal_slug", "user_id", "status",
                                              "stop_reason", "usage", "created_at",
                                              "updated_at", "title"))


async def _delete_agent_run(session_id: str) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_DELETE_AGENT_RUN_SQL, (session_id,))
        await conn.commit()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("", response_model=AgentRunEnvelope)
async def create_agent_run(
    body: CreateAgentRunBody,
    _auth: None = Depends(verify_backend_x_token),
) -> AgentRunEnvelope:
    """Mint a session against gtm-agent + gtm-env, send the first
    user.message, persist the ledger row, return the envelope."""
    try:
        session = await managed_agents.mint_session(
            initial_message=body.initial_message,
            vault_ids=body.vault_ids,
            title=body.title,
            metadata=body.metadata,
        )
    except managed_agents.ManagedAgentsNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"type": "managed_agents_not_configured", "message": exc.message},
        ) from exc
    except managed_agents.ManagedAgentsError as exc:
        logger.warning(
            "mint_session failed: status=%s message=%s body=%s",
            exc.status_code, exc.message, exc.response_body,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "anthropic_call_failed",
                "message": exc.message,
                "upstream_status": exc.status_code,
            },
        ) from exc

    session_id = session["id"]
    row = await _insert_agent_run(
        session_id=session_id,
        agent_id=settings.MANAGED_AGENT_ID_GTM or "",
        environment_id=settings.MANAGED_ENVIRONMENT_ID_GTM or "",
        signal_slug=body.signal_slug,
        user_id=body.user_id,
        initial_message=body.initial_message,
        title=body.title,
    )
    return AgentRunEnvelope(**row, anthropic=None)


@router.get("", response_model=AgentRunListResponse)
async def list_agent_runs(
    user_id: UUID,
    limit: int = 100,
    _auth: None = Depends(verify_backend_x_token),
) -> AgentRunListResponse:
    """List the caller's runs, newest first. ``user_id`` is supplied by the
    trusted caller (platform-api) from the validated JWT — the ledger is
    scoped per operator."""
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"type": "invalid_limit", "message": "limit must be in [1, 500]"},
        )
    rows = await _list_agent_runs(user_id, limit)
    return AgentRunListResponse(data=[AgentRunListItem(**r) for r in rows])


@router.get("/{session_id}/stream")
async def stream_agent_run(
    session_id: str,
    _auth: None = Depends(verify_backend_x_token),
) -> StreamingResponse:
    """Pipe Anthropic's text/event-stream through verbatim.

    No JSON parsing here — bytes pass through unmodified so platform-api
    and platform-app see the same SSE frames Anthropic emits. The
    httpx streaming context inside ``managed_agents.stream_events`` is
    bound to the response lifetime; client disconnect propagates an
    abort to the upstream fetch.
    """
    try:
        # stream_events_with_autoack silently acks `present_result` custom
        # tool calls so the stream doesn't stall waiting on a confirmation
        # the operator never needs to give. Other tool/permission gates
        # are NOT auto-acked — those surface in the UI as confirm prompts.
        agen = managed_agents.stream_events_with_autoack(session_id)
    except managed_agents.ManagedAgentsNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"type": "managed_agents_not_configured", "message": exc.message},
        ) from exc

    return StreamingResponse(
        agen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx + similar)
        },
    )


@router.get("/{session_id}/events")
async def list_agent_run_events(
    session_id: str,
    after: str | None = None,
    limit: int = 100,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    if limit < 1 or limit > 1000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"type": "invalid_limit", "message": "limit must be in [1, 1000]"},
        )
    try:
        return await managed_agents.list_events(session_id, after=after, limit=limit)
    except managed_agents.ManagedAgentsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "anthropic_call_failed",
                "message": exc.message,
                "upstream_status": exc.status_code,
            },
        ) from exc


@router.post("/{session_id}/events")
async def send_agent_run_events(
    session_id: str,
    body: SendEventsBody,
    _auth: None = Depends(verify_backend_x_token),
) -> dict[str, Any]:
    """Append one or more user-domain events. The events list is
    forwarded verbatim — Anthropic owns shape validation."""
    try:
        return await managed_agents.send_events(session_id, body.events)
    except managed_agents.ManagedAgentsError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "type": "anthropic_call_failed",
                "message": exc.message,
                "upstream_status": exc.status_code,
            },
        ) from exc


@router.get("/{session_id}", response_model=AgentRunEnvelope)
async def get_agent_run(
    session_id: str,
    _auth: None = Depends(verify_backend_x_token),
) -> AgentRunEnvelope:
    """Return the ledger row plus a live Anthropic session snapshot
    (status, usage, stop_reason)."""
    row = await _fetch_agent_run(session_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "agent_run_not_found", "session_id": session_id},
        )
    anthropic_snapshot: dict[str, Any] | None = None
    try:
        anthropic_snapshot = await managed_agents.retrieve_session(session_id)
    except managed_agents.ManagedAgentsError as exc:
        # The ledger row is still valid; surface a soft warning in the
        # response rather than 502'ing — the row alone is useful.
        logger.warning(
            "retrieve_session(%s) failed during GET: %s", session_id, exc.message,
        )
        anthropic_snapshot = {
            "_error": {
                "type": "anthropic_call_failed",
                "message": exc.message,
                "upstream_status": exc.status_code,
            }
        }
    return AgentRunEnvelope(**row, anthropic=anthropic_snapshot)


@router.patch("/{session_id}", response_model=AgentRunEnvelope)
async def rename_agent_run(
    session_id: str,
    body: RenameAgentRunBody,
    _auth: None = Depends(verify_backend_x_token),
) -> AgentRunEnvelope:
    """Set the operator-facing title for a run. Session-scoped; the event
    history is untouched."""
    row = await _rename_agent_run(session_id, body.title)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "agent_run_not_found", "session_id": session_id},
        )
    return AgentRunEnvelope(**row, anthropic=None)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent_run(
    session_id: str,
    _auth: None = Depends(verify_backend_x_token),
) -> None:
    """Remove a run from the ledger (drops it from the history list). The
    Anthropic session itself is left to idle out."""
    await _delete_agent_run(session_id)
