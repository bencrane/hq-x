"""Inbound phone config CRUD — phone number → assistant mapping."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection

router = APIRouter(
    prefix="/api/brands/{brand_id}/voice/inbound",
    tags=["voice-inbound"],
)


class InboundPhoneConfigCreateRequest(BaseModel):
    phone_number: str
    voice_assistant_id: UUID
    phone_number_sid: str | None = None
    partner_id: UUID | None = None
    routing_mode: str = "static"
    first_message_mode: str | None = None
    inbound_config: dict[str, Any] | None = None
    is_active: bool = True
    model_config = {"extra": "forbid"}


class InboundPhoneConfigUpdateRequest(BaseModel):
    voice_assistant_id: UUID | None = None
    phone_number_sid: str | None = None
    partner_id: UUID | None = None
    routing_mode: str | None = None
    first_message_mode: str | None = None
    inbound_config: dict[str, Any] | None = None
    is_active: bool | None = None
    model_config = {"extra": "forbid"}


_COLS = [
    "id", "brand_id", "phone_number", "phone_number_sid",
    "voice_assistant_id", "partner_id", "routing_mode",
    "first_message_mode", "inbound_config", "is_active",
    "created_at", "updated_at",
]


def _row(r: tuple) -> dict[str, Any]:
    return dict(zip(_COLS, r, strict=True))


async def _validate_assistant(brand_id: UUID, assistant_id: UUID) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM voice_assistants
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "assistant not found or not in this brand"},
        )


@router.post("/phone-configs", status_code=status.HTTP_201_CREATED)
async def create_phone_config(
    brand_id: UUID,
    body: InboundPhoneConfigCreateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    await _validate_assistant(brand_id, body.voice_assistant_id)

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    INSERT INTO voice_assistant_phone_configs (
                        brand_id, phone_number, phone_number_sid,
                        voice_assistant_id, partner_id, routing_mode,
                        first_message_mode, inbound_config, is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING {", ".join(_COLS)}
                    """,
                    (
                        str(brand_id),
                        body.phone_number,
                        body.phone_number_sid,
                        str(body.voice_assistant_id),
                        str(body.partner_id) if body.partner_id else None,
                        body.routing_mode,
                        body.first_message_mode,
                        json.dumps(body.inbound_config) if body.inbound_config else None,
                        body.is_active,
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()
    except Exception as exc:
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg:
            raise HTTPException(
                status_code=409,
                detail={"error": "phone_number_already_configured"},
            ) from exc
        raise
    return _row(row)


@router.get("/phone-configs")
async def list_phone_configs(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_COLS)}
                FROM voice_assistant_phone_configs
                WHERE brand_id = %s AND deleted_at IS NULL
                ORDER BY created_at
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row(r) for r in rows]


@router.get("/phone-configs/{config_id}")
async def get_phone_config(
    brand_id: UUID,
    config_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_COLS)}
                FROM voice_assistant_phone_configs
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(config_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_config_not_found"})
    return _row(row)


@router.patch("/phone-configs/{config_id}")
async def update_phone_config(
    brand_id: UUID,
    config_id: UUID,
    body: InboundPhoneConfigUpdateRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})

    if "voice_assistant_id" in updates:
        await _validate_assistant(brand_id, updates["voice_assistant_id"])
        updates["voice_assistant_id"] = str(updates["voice_assistant_id"])
    if updates.get("partner_id"):
        updates["partner_id"] = str(updates["partner_id"])

    json_cols = {"inbound_config"}
    set_parts: list[str] = []
    values: list[Any] = []
    for key, val in updates.items():
        if key in json_cols:
            set_parts.append(f"{key} = %s::jsonb")
            values.append(json.dumps(val))
        else:
            set_parts.append(f"{key} = %s")
            values.append(val)
    set_parts.append("updated_at = NOW()")
    values.extend([str(config_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE voice_assistant_phone_configs
                SET {", ".join(set_parts)}
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING {", ".join(_COLS)}
                """,
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_config_not_found"})
    return _row(row)


@router.delete("/phone-configs/{config_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_phone_config(
    brand_id: UUID,
    config_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_assistant_phone_configs
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (str(config_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_config_not_found"})
