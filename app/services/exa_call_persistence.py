"""Persist exa.exa_calls rows in either hq-x's own DB or in DEX.

Two functions, one per destination. The Trigger.dev worker picks the
right one based on the job's ``destination`` flag. The shape on the
wire to DEX matches DEX's POST /api/internal/exa/calls request body
exactly, so the two paths share a single payload-builder helper.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection

logger = logging.getLogger(__name__)


class ExaCallPersistenceError(Exception):
    pass


def _payload_for_dex(
    *,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> dict[str, Any]:
    return {
        "job_id": str(job_id),
        "endpoint": endpoint,
        "request_payload": request_payload,
        "response_payload": response_payload,
        "status": status,
        "error": error,
        "exa_request_id": exa_request_id,
        "cost_dollars": cost_dollars,
        "duration_ms": duration_ms,
        "objective": objective,
        "objective_ref": objective_ref,
    }


async def persist_exa_call_local(
    *,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> UUID:
    """INSERT into hq-x's own exa.exa_calls. Returns the new row id."""
    new_id = uuid4()
    completed_at_clause = (
        "NOW()" if status in ("succeeded", "failed") else "NULL"
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO exa.exa_calls (
                    id, endpoint, request_payload, response_payload,
                    status, error, exa_request_id, cost_dollars,
                    duration_ms, objective, objective_ref,
                    triggered_by_job_id, completed_at
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    {completed_at_clause}
                )
                RETURNING id
                """,
                (
                    str(new_id),
                    endpoint,
                    Jsonb(request_payload),
                    Jsonb(response_payload) if response_payload is not None else None,
                    status,
                    error,
                    exa_request_id,
                    cost_dollars,
                    duration_ms,
                    objective,
                    objective_ref,
                    str(job_id),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return UUID(str(row[0]))


async def persist_exa_call_to_dex(
    *,
    job_id: UUID,
    endpoint: str,
    objective: str,
    objective_ref: str | None,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    status: str,
    error: str | None,
    exa_request_id: str | None,
    cost_dollars: float | None,
    duration_ms: int | None,
) -> UUID:
    """POST to DEX's /api/internal/exa/calls. Returns the new row id."""
    base = settings.DEX_BASE_URL
    if not base:
        raise ExaCallPersistenceError(
            "DEX_BASE_URL is not configured — cannot persist to DEX"
        )
    api_key_obj = settings.DEX_SUPER_ADMIN_API_KEY
    if not api_key_obj:
        raise ExaCallPersistenceError(
            "DEX_SUPER_ADMIN_API_KEY is not configured — cannot persist to DEX"
        )
    api_key = (
        api_key_obj.get_secret_value()
        if hasattr(api_key_obj, "get_secret_value")
        else str(api_key_obj)
    )
    body = _payload_for_dex(
        job_id=job_id,
        endpoint=endpoint,
        objective=objective,
        objective_ref=objective_ref,
        request_payload=request_payload,
        response_payload=response_payload,
        status=status,
        error=error,
        exa_request_id=exa_request_id,
        cost_dollars=cost_dollars,
        duration_ms=duration_ms,
    )
    url = f"{base.rstrip('/')}/api/internal/exa/calls"
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
    if resp.status_code >= 400:
        raise ExaCallPersistenceError(
            f"DEX /api/internal/exa/calls returned HTTP {resp.status_code}: "
            f"{resp.text[:500]}"
        )
    payload = resp.json() if resp.text else {}
    data = payload.get("data") or {}
    row_id = data.get("id")
    if not row_id:
        raise ExaCallPersistenceError(
            f"DEX /api/internal/exa/calls did not return an id: {payload!r}"
        )
    return UUID(str(row_id))


__all__ = [
    "ExaCallPersistenceError",
    "persist_exa_call_local",
    "persist_exa_call_to_dex",
]
