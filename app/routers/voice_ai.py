"""Voice AI assistants — Vapi is the source of truth.

The local ``voice_assistants`` row is a thin pointer holding only the
brand/partner/campaign association + ``vapi_assistant_id``. All
Vapi-shape config (system_prompt, voice/model/transcriber/tools/analysis,
metadata, etc.) lives on Vapi. Reads pass through to Vapi at request
time; edits forward straight to Vapi PATCH.

Brand/partner/campaign association mutations are out of scope here — a
separate workstream owns that endpoint.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key

logger = logging.getLogger(__name__)

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
    channel_campaign_id: UUID | None = None
    model_config = {"extra": "forbid"}


class VoiceAssistantUpdateRequest(BaseModel):
    """PATCH body — Vapi-shape fields only.

    All fields forward straight to Vapi. Brand/partner/campaign
    association edits are handled by a separate endpoint. ``status`` is
    a local-only field and is not accepted here.
    """

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
    model_config = {"extra": "forbid"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_ASSISTANT_COLS = [
    "id", "brand_id", "partner_id", "channel_campaign_id",
    "assistant_type", "vapi_assistant_id", "status",
    "created_at", "updated_at",
]


def _row_to_dict(row: tuple) -> dict[str, Any]:
    return dict(zip(_ASSISTANT_COLS, row, strict=True))


def _build_vapi_config(source: dict[str, Any]) -> dict[str, Any]:
    """Build a Vapi assistant config from a request-shaped dict.

    Accepts either the create/patch request fields directly. Only
    non-None values are forwarded. ``system_prompt`` is rolled into
    ``model.messages`` (with ``model_config_data`` providing the rest of
    the model block, if any).
    """
    config: dict[str, Any] = {}
    if source.get("name") is not None:
        config["name"] = source["name"]
    model_cfg = source.get("model_config_data")
    system_prompt = source.get("system_prompt")
    if system_prompt is not None or model_cfg is not None:
        merged = dict(model_cfg or {})
        if system_prompt is not None:
            merged["messages"] = [{"role": "system", "content": system_prompt}]
        if merged:
            config["model"] = merged
    if source.get("first_message") is not None:
        config["firstMessage"] = source["first_message"]
    if source.get("first_message_mode") is not None:
        config["firstMessageMode"] = source["first_message_mode"]
    if source.get("voice_config") is not None:
        config["voice"] = source["voice_config"]
    if source.get("transcriber_config") is not None:
        config["transcriber"] = source["transcriber_config"]
    if source.get("tools_config") is not None:
        config["tools"] = source["tools_config"]
    if source.get("analysis_config") is not None:
        config["analysisPlan"] = source["analysis_config"]
    if source.get("max_duration_seconds") is not None:
        config["maxDurationSeconds"] = source["max_duration_seconds"]
    if source.get("metadata") is not None:
        config["metadata"] = source["metadata"]
    return config


# ---------------------------------------------------------------------------
# CRUD — Vapi-first
# ---------------------------------------------------------------------------


@router.post("/assistants", status_code=status.HTTP_201_CREATED)
async def create_assistant(
    brand_id: UUID,
    body: VoiceAssistantCreateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Create assistant on Vapi first, then mirror a thin pointer locally."""
    if body.assistant_type not in ("outbound_qualifier", "inbound_ivr", "callback"):
        raise HTTPException(status_code=400, detail={"error": "invalid assistant_type"})

    api_key = vapi_key()
    config = _build_vapi_config(body.model_dump(exclude_none=True))

    try:
        vapi_response = vapi_client.create_assistant(api_key, config)
    except VapiProviderError as exc:
        raise_vapi_error("create_assistant", exc)

    vapi_assistant_id = vapi_response.get("id")
    if not vapi_assistant_id:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "vapi_create_returned_no_id",
                "vapi_response": vapi_response,
            },
        )

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    INSERT INTO voice_assistants (
                        brand_id, partner_id, channel_campaign_id,
                        name, assistant_type, vapi_assistant_id, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, 'active')
                    RETURNING {", ".join(_ASSISTANT_COLS)}
                    """,
                    (
                        str(brand_id),
                        str(body.partner_id) if body.partner_id else None,
                        str(body.channel_campaign_id) if body.channel_campaign_id else None,
                        body.name,
                        body.assistant_type,
                        vapi_assistant_id,
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        logger.warning(
            "voice_assistants insert failed after Vapi create succeeded; "
            "Vapi assistant %s is orphaned: %s",
            vapi_assistant_id, exc,
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "local_insert_failed_after_vapi_create",
                "vapi_assistant_id": vapi_assistant_id,
            },
        ) from exc

    return {"local": _row_to_dict(row), "vapi": vapi_response}


@router.get("/assistants")
async def list_assistants(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List local pointers + their live Vapi config (one Vapi list call)."""
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

    local_rows = [_row_to_dict(r) for r in rows]
    if not local_rows:
        return []

    api_key = vapi_key()
    try:
        vapi_list = vapi_client.list_assistants(api_key, limit=100)
    except VapiProviderError as exc:
        raise_vapi_error("list_assistants", exc)

    vapi_index = {item.get("id"): item for item in vapi_list if item.get("id")}
    return [
        {
            "local": local,
            "vapi": vapi_index.get(local.get("vapi_assistant_id")),
        }
        for local in local_rows
    ]


@router.get("/assistants/{assistant_id}")
async def get_assistant(
    brand_id: UUID,
    assistant_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Read pointer locally + live config from Vapi."""
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

    local = _row_to_dict(row)
    vapi_assistant_id = local.get("vapi_assistant_id")
    if not vapi_assistant_id:
        # Orphan / legacy data — pointer with no Vapi binding.
        return {"local": local, "vapi": None}

    api_key = vapi_key()
    try:
        vapi_response = vapi_client.get_assistant(api_key, vapi_assistant_id)
    except VapiProviderError as exc:
        raise_vapi_error("get_assistant", exc)
    return {"local": local, "vapi": vapi_response}


@router.patch("/assistants/{assistant_id}")
async def update_assistant(
    brand_id: UUID,
    assistant_id: UUID,
    body: VoiceAssistantUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Forward to Vapi PATCH; only ``updated_at`` changes locally."""
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})

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

    local = _row_to_dict(row)
    vapi_assistant_id = local.get("vapi_assistant_id")
    if not vapi_assistant_id:
        raise HTTPException(status_code=404, detail={"error": "assistant_not_found"})

    config = _build_vapi_config(updates)
    api_key = vapi_key()
    try:
        vapi_response = vapi_client.update_assistant(api_key, vapi_assistant_id, config)
    except VapiProviderError as exc:
        raise_vapi_error("update_assistant", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE voice_assistants
                SET updated_at = NOW()
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING {", ".join(_ASSISTANT_COLS)}
                """,
                (str(assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "assistant_not_found"})

    return {"local": _row_to_dict(row), "vapi": vapi_response}


class AssistantReassignBrandRequest(BaseModel):
    new_brand_id: UUID
    model_config = {"extra": "forbid"}


@router.patch("/assistants/{assistant_id}/brand")
async def reassign_assistant_brand(
    brand_id: UUID,
    assistant_id: UUID,
    body: AssistantReassignBrandRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Move an assistant to a different brand.

    partner_id and channel_campaign_id are nulled on transfer because their
    composite FKs (partner_id, brand_id) / (channel_campaign_id, brand_id)
    require those rows to belong to the same brand. Re-link partner /
    campaign separately under the new brand if needed.

    Vapi-side config is unaffected — the Vapi assistant has no brand
    awareness; this endpoint only re-keys the local pointer row.
    """
    if body.new_brand_id == brand_id:
        raise HTTPException(status_code=400, detail={"error": "new_brand_id_same_as_current"})

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Verify destination brand exists.
            await cur.execute(
                "SELECT 1 FROM business.brands WHERE id = %s AND deleted_at IS NULL",
                (str(body.new_brand_id),),
            )
            if await cur.fetchone() is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "destination_brand_not_found"},
                )

            await cur.execute(
                f"""
                UPDATE voice_assistants
                SET brand_id = %s,
                    partner_id = NULL,
                    channel_campaign_id = NULL,
                    updated_at = NOW()
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING {", ".join(_ASSISTANT_COLS)}
                """,
                (str(body.new_brand_id), str(assistant_id), str(brand_id)),
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
    """Delete on Vapi, then soft-delete the local pointer."""
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

    local = _row_to_dict(row)
    vapi_assistant_id = local.get("vapi_assistant_id")
    if vapi_assistant_id:
        api_key = vapi_key()
        try:
            vapi_client.delete_assistant(api_key, vapi_assistant_id)
        except VapiProviderError as exc:
            # Already gone on Vapi: proceed with local soft-delete.
            if exc.category == "terminal" and "endpoint not found" in str(exc).lower():
                logger.info(
                    "Vapi assistant %s already gone; soft-deleting local row",
                    vapi_assistant_id,
                )
            else:
                # Transient (5xx, etc.) — let the caller retry. Don't touch
                # local state so a retry can re-issue the Vapi delete.
                raise_vapi_error("delete_assistant", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_assistants
                SET deleted_at = NOW(), updated_at = NOW(), status = 'archived'
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(assistant_id), str(brand_id)),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# Vapi-side passthrough — account-wide reconciliation list
# ---------------------------------------------------------------------------


@router.get("/vapi/assistants")
async def list_vapi_assistants(
    brand_id: UUID,
    limit: int = 100,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List Vapi's full account-level set of assistants — useful for reconciliation."""
    api_key = vapi_key()
    try:
        return vapi_client.list_assistants(api_key, limit=limit)
    except VapiProviderError as exc:
        raise_vapi_error("list_assistants", exc)
