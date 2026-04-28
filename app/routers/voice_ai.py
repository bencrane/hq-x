"""Voice AI assistant CRUD + Vapi sync.

Brand-scoped via path. Vapi credentials are global (settings.VAPI_API_KEY).
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError

router = APIRouter(prefix="/api/brands/{brand_id}/voice-ai", tags=["voice-ai"])


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VoiceAssistantCreateRequest(BaseModel):
    name: str
    assistant_type: str  # outbound_qualifier | inbound_ivr | callback
    system_prompt: str | None = None
    first_message: str | None = None
    first_message_mode: str = "assistant-speaks-first"
    model_config_data: dict[str, Any] | None = None
    voice_config: dict[str, Any] | None = None
    transcriber_config: dict[str, Any] | None = None
    tools_config: list[dict[str, Any]] | None = None
    analysis_config: dict[str, Any] | None = None
    max_duration_seconds: int = 600
    metadata: dict[str, Any] | None = None
    partner_id: UUID | None = None
    campaign_id: UUID | None = None
    model_config = {"extra": "forbid"}


class VoiceAssistantUpdateRequest(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    first_message: str | None = None
    first_message_mode: str | None = None
    model_config_data: dict[str, Any] | None = None
    voice_config: dict[str, Any] | None = None
    transcriber_config: dict[str, Any] | None = None
    tools_config: list[dict[str, Any]] | None = None
    analysis_config: dict[str, Any] | None = None
    max_duration_seconds: int | None = None
    metadata: dict[str, Any] | None = None
    status: str | None = None
    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vapi_key() -> str:
    if settings.VAPI_API_KEY is None:
        raise HTTPException(
            status_code=503,
            detail={"error": "VAPI_API_KEY not configured"},
        )
    return settings.VAPI_API_KEY.get_secret_value()


def _raise_vapi_error(operation: str, exc: VapiProviderError) -> None:
    code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.retryable
        else status.HTTP_502_BAD_GATEWAY
    )
    raise HTTPException(
        status_code=code,
        detail={
            "type": "provider_error",
            "provider": "vapi",
            "operation": operation,
            "retryable": exc.retryable,
            "message": str(exc),
        },
    ) from exc


_ASSISTANT_COLS = [
    "id", "brand_id", "partner_id", "campaign_id", "name", "assistant_type",
    "vapi_assistant_id", "system_prompt", "first_message", "first_message_mode",
    "model_config", "voice_config", "transcriber_config", "tools_config",
    "analysis_config", "max_duration_seconds", "metadata", "status",
    "created_at", "updated_at",
]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return dict(zip(_ASSISTANT_COLS, row, strict=True))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("/assistants", status_code=status.HTTP_201_CREATED)
async def create_assistant(
    brand_id: UUID,
    body: VoiceAssistantCreateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if body.assistant_type not in ("outbound_qualifier", "inbound_ivr", "callback"):
        raise HTTPException(status_code=400, detail={"error": "invalid assistant_type"})

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO voice_assistants (
                    brand_id, partner_id, campaign_id, name, assistant_type,
                    system_prompt, first_message, first_message_mode,
                    model_config, voice_config, transcriber_config,
                    tools_config, analysis_config, max_duration_seconds,
                    metadata, status
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s, %s::jsonb, 'draft'
                )
                RETURNING {", ".join(_ASSISTANT_COLS)}
                """,
                (
                    str(brand_id),
                    str(body.partner_id) if body.partner_id else None,
                    str(body.campaign_id) if body.campaign_id else None,
                    body.name,
                    body.assistant_type,
                    body.system_prompt,
                    body.first_message,
                    body.first_message_mode,
                    json.dumps(body.model_config_data) if body.model_config_data else None,
                    json.dumps(body.voice_config) if body.voice_config else None,
                    json.dumps(body.transcriber_config) if body.transcriber_config else None,
                    json.dumps(body.tools_config) if body.tools_config else None,
                    json.dumps(body.analysis_config) if body.analysis_config else None,
                    body.max_duration_seconds,
                    json.dumps(body.metadata) if body.metadata else None,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_dict(row)


@router.get("/assistants")
async def list_assistants(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_ASSISTANT_COLS)}
                FROM voice_assistants
                WHERE brand_id = %s AND deleted_at IS NULL
                ORDER BY created_at
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(r) for r in rows]


@router.get("/assistants/{assistant_id}")
async def get_assistant(
    brand_id: UUID,
    assistant_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_ASSISTANT_COLS)}
                FROM voice_assistants
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "assistant_not_found"})
    return _row_to_dict(row)


@router.patch("/assistants/{assistant_id}")
async def update_assistant(
    brand_id: UUID,
    assistant_id: UUID,
    body: VoiceAssistantUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})

    set_parts: list[str] = []
    values: list[Any] = []
    column_map = {"model_config_data": "model_config"}
    json_columns = {
        "model_config", "voice_config", "transcriber_config",
        "tools_config", "analysis_config", "metadata",
    }
    for key, value in updates.items():
        col = column_map.get(key, key)
        if col in json_columns:
            set_parts.append(f"{col} = %s::jsonb")
            values.append(json.dumps(value))
        else:
            set_parts.append(f"{col} = %s")
            values.append(value)
    set_parts.append("updated_at = NOW()")
    values.extend([str(assistant_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE voice_assistants
                SET {", ".join(set_parts)}
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING {", ".join(_ASSISTANT_COLS)}
                """,
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "assistant_not_found"})
    return _row_to_dict(row)


@router.delete("/assistants/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assistant(
    brand_id: UUID,
    assistant_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_assistants
                SET deleted_at = NOW(), updated_at = NOW(), status = 'archived'
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (str(assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "assistant_not_found"})


# ---------------------------------------------------------------------------
# Vapi sync — push the local assistant config to Vapi
# ---------------------------------------------------------------------------


def _build_vapi_config(row: dict[str, Any]) -> dict[str, Any]:
    config: dict[str, Any] = {"name": row["name"]}
    if row.get("system_prompt"):
        model_cfg = dict(row.get("model_config") or {})
        model_cfg["messages"] = [{"role": "system", "content": row["system_prompt"]}]
        config["model"] = model_cfg
    if row.get("first_message"):
        config["firstMessage"] = row["first_message"]
    if row.get("first_message_mode"):
        config["firstMessageMode"] = row["first_message_mode"]
    if row.get("voice_config"):
        config["voice"] = row["voice_config"]
    if row.get("transcriber_config"):
        config["transcriber"] = row["transcriber_config"]
    if row.get("tools_config"):
        config["tools"] = row["tools_config"]
    if row.get("analysis_config"):
        config["analysisPlan"] = row["analysis_config"]
    if row.get("max_duration_seconds"):
        config["maxDurationSeconds"] = row["max_duration_seconds"]
    if row.get("metadata"):
        config["metadata"] = row["metadata"]
    return config


@router.post("/assistants/{assistant_id}/sync")
async def sync_assistant_to_vapi(
    brand_id: UUID,
    assistant_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Create or update the assistant on Vapi from the local config."""
    api_key = _vapi_key()

    assistant = await get_assistant(brand_id, assistant_id, _auth)  # type: ignore[arg-type]
    config = _build_vapi_config(assistant)

    try:
        if assistant.get("vapi_assistant_id"):
            result = vapi_client.update_assistant(
                api_key, assistant["vapi_assistant_id"], config,
            )
        else:
            result = vapi_client.create_assistant(api_key, config)
    except VapiProviderError as exc:
        _raise_vapi_error("sync_assistant", exc)

    vapi_id = result.get("id")
    if vapi_id and vapi_id != assistant.get("vapi_assistant_id"):
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE voice_assistants
                    SET vapi_assistant_id = %s, status = 'active', updated_at = NOW()
                    WHERE id = %s AND brand_id = %s
                    """,
                    (vapi_id, str(assistant_id), str(brand_id)),
                )
            await conn.commit()

    return {"vapi_assistant_id": vapi_id, "vapi_response": result}
