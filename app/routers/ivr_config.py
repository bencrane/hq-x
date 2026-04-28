"""IVR flow configuration management API.

Brand-scoped via path. All endpoints require ``require_flexible_auth``.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from supabase import create_client

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.db import get_db_connection
from app.models.ivr import (
    IvrFlowCreate,
    IvrFlowStepCreate,
    IvrFlowStepUpdate,
    IvrFlowUpdate,
    IvrPhoneConfigCreate,
    IvrPhoneConfigUpdate,
)

router = APIRouter(prefix="/api/brands/{brand_id}/ivr-config", tags=["ivr-config"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FLOW_COLS = [
    "id", "brand_id", "name", "description", "is_active",
    "default_voice", "default_language",
    "lookup_type", "lookup_config",
    "default_transfer_number", "transfer_timeout_seconds",
    "recording_enabled", "recording_consent_required",
    "created_at", "updated_at",
]

_STEP_COLS = [
    "id", "flow_id", "brand_id", "step_key", "step_type", "position",
    "say_text", "say_voice", "say_language", "audio_url",
    "gather_input", "gather_num_digits", "gather_timeout_seconds",
    "gather_finish_on_key", "gather_max_retries",
    "gather_invalid_message", "gather_validation_regex",
    "next_step_key", "branches",
    "transfer_number", "transfer_caller_id", "transfer_record",
    "record_max_length_seconds", "record_play_beep",
    "lookup_input_key", "lookup_store_key",
    "created_at", "updated_at",
]

_PHONE_CONFIG_COLS = [
    "id", "brand_id", "phone_number", "phone_number_sid",
    "flow_id", "is_active", "created_at", "updated_at",
]

_FLOW_JSON_COLUMNS = {"lookup_config"}
_STEP_JSON_COLUMNS = {"branches"}


def _row_to_dict(cols: list[str], row: tuple) -> dict[str, Any]:
    return dict(zip(cols, row, strict=True))


async def _validate_flow_in_brand(flow_id: UUID, brand_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(_FLOW_COLS)} FROM ivr_flows "
                "WHERE id = %s AND brand_id = %s AND deleted_at IS NULL",
                (str(flow_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "flow_not_found"})
    return _row_to_dict(_FLOW_COLS, row)


# ---------------------------------------------------------------------------
# Flow CRUD
# ---------------------------------------------------------------------------


@router.post("/flows", status_code=status.HTTP_201_CREATED)
async def create_flow(
    brand_id: UUID,
    body: IvrFlowCreate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    payload = body.model_dump(exclude_none=True)
    keys = list(payload.keys())
    placeholders: list[str] = []
    values: list[Any] = [str(brand_id)]
    for k in keys:
        v = payload[k]
        if k in _FLOW_JSON_COLUMNS:
            placeholders.append("%s::jsonb")
            values.append(json.dumps(v))
        else:
            placeholders.append("%s")
            values.append(v)
    cols_clause = ", ".join(["brand_id"] + keys)
    placeholders_clause = ", ".join(["%s"] + placeholders)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO ivr_flows ({cols_clause}) VALUES ({placeholders_clause}) "
                f"RETURNING {', '.join(_FLOW_COLS)}",
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_dict(_FLOW_COLS, row)


@router.get("/flows")
async def list_flows(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(_FLOW_COLS)} FROM ivr_flows "
                "WHERE brand_id = %s AND deleted_at IS NULL ORDER BY created_at",
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(_FLOW_COLS, r) for r in rows]


@router.get("/flows/{flow_id}")
async def get_flow(
    brand_id: UUID,
    flow_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    flow = await _validate_flow_in_brand(flow_id, brand_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(_STEP_COLS)} FROM ivr_flow_steps "
                "WHERE flow_id = %s AND brand_id = %s ORDER BY position",
                (str(flow_id), str(brand_id)),
            )
            rows = await cur.fetchall()
    flow["steps"] = [_row_to_dict(_STEP_COLS, r) for r in rows]
    return flow


@router.put("/flows/{flow_id}")
async def update_flow(
    brand_id: UUID,
    flow_id: UUID,
    body: IvrFlowUpdate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    set_parts: list[str] = []
    values: list[Any] = []
    for k, v in updates.items():
        if k in _FLOW_JSON_COLUMNS:
            set_parts.append(f"{k} = %s::jsonb")
            values.append(json.dumps(v))
        else:
            set_parts.append(f"{k} = %s")
            values.append(v)
    set_parts.append("updated_at = NOW()")
    values.extend([str(flow_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE ivr_flows SET {', '.join(set_parts)} "
                "WHERE id = %s AND brand_id = %s AND deleted_at IS NULL "
                f"RETURNING {', '.join(_FLOW_COLS)}",
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "flow_not_found"})
    return _row_to_dict(_FLOW_COLS, row)


@router.delete("/flows/{flow_id}")
async def delete_flow(
    brand_id: UUID,
    flow_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM ivr_phone_configs
                WHERE flow_id = %s AND brand_id = %s
                  AND is_active = TRUE AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(flow_id), str(brand_id)),
            )
            existing = await cur.fetchone()
            if existing is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Cannot delete flow with active phone configurations.",
                )
            await cur.execute(
                """
                UPDATE ivr_flows
                SET deleted_at = NOW(), updated_at = NOW()
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                RETURNING id
                """,
                (str(flow_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "flow_not_found"})
    return {"deleted": True, "id": str(flow_id)}


# ---------------------------------------------------------------------------
# Step CRUD
# ---------------------------------------------------------------------------


@router.post("/flows/{flow_id}/steps", status_code=status.HTTP_201_CREATED)
async def create_step(
    brand_id: UUID,
    flow_id: UUID,
    body: IvrFlowStepCreate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    await _validate_flow_in_brand(flow_id, brand_id)
    payload = body.model_dump(exclude_none=True)
    keys = list(payload.keys())
    placeholders: list[str] = []
    values: list[Any] = [str(flow_id), str(brand_id)]
    for k in keys:
        v = payload[k]
        if k in _STEP_JSON_COLUMNS:
            placeholders.append("%s::jsonb")
            values.append(json.dumps(v))
        else:
            placeholders.append("%s")
            values.append(v)
    cols_clause = ", ".join(["flow_id", "brand_id"] + keys)
    placeholders_clause = ", ".join(["%s", "%s"] + placeholders)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO ivr_flow_steps ({cols_clause}) VALUES ({placeholders_clause}) "
                f"RETURNING {', '.join(_STEP_COLS)}",
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_dict(_STEP_COLS, row)


@router.put("/flows/{flow_id}/steps/{step_id}")
async def update_step(
    brand_id: UUID,
    flow_id: UUID,
    step_id: UUID,
    body: IvrFlowStepUpdate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})
    set_parts: list[str] = []
    values: list[Any] = []
    for k, v in updates.items():
        if k in _STEP_JSON_COLUMNS:
            set_parts.append(f"{k} = %s::jsonb")
            values.append(json.dumps(v))
        else:
            set_parts.append(f"{k} = %s")
            values.append(v)
    set_parts.append("updated_at = NOW()")
    values.extend([str(step_id), str(flow_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE ivr_flow_steps SET {', '.join(set_parts)} "
                "WHERE id = %s AND flow_id = %s AND brand_id = %s "
                f"RETURNING {', '.join(_STEP_COLS)}",
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "step_not_found"})
    return _row_to_dict(_STEP_COLS, row)


@router.delete("/flows/{flow_id}/steps/{step_id}")
async def delete_step(
    brand_id: UUID,
    flow_id: UUID,
    step_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM ivr_flow_steps "
                "WHERE id = %s AND flow_id = %s AND brand_id = %s RETURNING id",
                (str(step_id), str(flow_id), str(brand_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "step_not_found"})
    return {"deleted": True, "id": str(step_id)}


# ---------------------------------------------------------------------------
# Phone Config CRUD
# ---------------------------------------------------------------------------


@router.post("/phone-configs", status_code=status.HTTP_201_CREATED)
async def create_phone_config(
    brand_id: UUID,
    body: IvrPhoneConfigCreate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    await _validate_flow_in_brand(UUID(body.flow_id), brand_id)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    """
                    INSERT INTO ivr_phone_configs (
                        brand_id, phone_number, phone_number_sid, flow_id, is_active
                    ) VALUES (%s, %s, %s, %s, %s)
                    RETURNING id, brand_id, phone_number, phone_number_sid,
                              flow_id, is_active, created_at, updated_at
                    """,
                    (
                        str(brand_id),
                        body.phone_number,
                        body.phone_number_sid,
                        body.flow_id,
                        body.is_active,
                    ),
                )
                row = await cur.fetchone()
            except Exception as exc:  # noqa: BLE001
                msg = str(exc).lower()
                if "duplicate" in msg or "unique" in msg:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail="This phone number already has an active IVR configuration",
                    ) from exc
                raise
        await conn.commit()
    return _row_to_dict(_PHONE_CONFIG_COLS, row)


@router.get("/phone-configs")
async def list_phone_configs(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {', '.join(_PHONE_CONFIG_COLS)} FROM ivr_phone_configs "
                "WHERE brand_id = %s AND deleted_at IS NULL ORDER BY created_at",
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row_to_dict(_PHONE_CONFIG_COLS, r) for r in rows]


@router.put("/phone-configs/{config_id}")
async def update_phone_config(
    brand_id: UUID,
    config_id: UUID,
    body: IvrPhoneConfigUpdate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(status_code=400, detail={"error": "no fields to update"})

    if "flow_id" in updates:
        await _validate_flow_in_brand(UUID(updates["flow_id"]), brand_id)

    set_parts: list[str] = []
    values: list[Any] = []
    for k, v in updates.items():
        set_parts.append(f"{k} = %s")
        values.append(v)
    set_parts.append("updated_at = NOW()")
    values.extend([str(config_id), str(brand_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE ivr_phone_configs SET {', '.join(set_parts)} "
                "WHERE id = %s AND brand_id = %s AND deleted_at IS NULL "
                f"RETURNING {', '.join(_PHONE_CONFIG_COLS)}",
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_config_not_found"})
    return _row_to_dict(_PHONE_CONFIG_COLS, row)


@router.delete("/phone-configs/{config_id}")
async def delete_phone_config(
    brand_id: UUID,
    config_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE ivr_phone_configs
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
    return {"deleted": True, "id": str(config_id)}


# ---------------------------------------------------------------------------
# Audio file management (Supabase Storage)
# ---------------------------------------------------------------------------

ALLOWED_AUDIO_TYPES = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/vorbis": "ogg",
}
MAX_AUDIO_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
STORAGE_BUCKET = "ivr-audio"


class AudioDeleteRequest(BaseModel):
    storage_path: str


def _supabase_client():
    return create_client(
        str(settings.HQX_SUPABASE_URL).rstrip("/"),
        settings.HQX_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
    )


@router.post("/audio")
async def upload_audio(
    brand_id: UUID,
    file: UploadFile = File(...),
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if file.content_type not in ALLOWED_AUDIO_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio type: {file.content_type}. Allowed: mp3, wav, ogg",
        )

    file_bytes = await file.read()
    if len(file_bytes) > MAX_AUDIO_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Maximum size is {MAX_AUDIO_SIZE_BYTES // (1024 * 1024)} MB",
        )

    ext = ALLOWED_AUDIO_TYPES[file.content_type]
    path = f"{brand_id}/{uuid4()}.{ext}"

    sb = _supabase_client()
    try:
        sb.storage.from_(STORAGE_BUCKET).upload(
            path=path,
            file=file_bytes,
            file_options={"content-type": file.content_type},
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Failed to upload to storage: {exc}",
        ) from exc

    public_url = sb.storage.from_(STORAGE_BUCKET).get_public_url(path)
    return {
        "audio_url": public_url,
        "storage_path": path,
        "file_name": file.filename,
        "content_type": file.content_type,
        "size_bytes": len(file_bytes),
    }


@router.delete("/audio")
async def delete_audio(
    brand_id: UUID,
    body: AudioDeleteRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    if not body.storage_path.startswith(f"{brand_id}/"):
        raise HTTPException(
            status_code=403,
            detail="Cannot delete audio files belonging to another brand",
        )
    _supabase_client().storage.from_(STORAGE_BUCKET).remove([body.storage_path])
    return {"deleted": True, "storage_path": body.storage_path}


@router.get("/audio")
async def list_audio(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    sb = _supabase_client()
    files = sb.storage.from_(STORAGE_BUCKET).list(str(brand_id))
    result = []
    for f in files:
        storage_path = f"{brand_id}/{f['name']}"
        public_url = sb.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)
        result.append({
            "name": f["name"],
            "storage_path": storage_path,
            "audio_url": public_url,
            "size_bytes": f.get("metadata", {}).get("size"),
            "created_at": f.get("created_at"),
        })
    return result
