"""Vapi-side phone-number CRUD + bind.

Operates on the Vapi record for a phone number that already exists in
``voice_phone_numbers``. The Twilio-side CRUD lives in
``app/routers/phone_numbers.py``; the two routers are parallel surfaces
on the same local row.

Import flow: Twilio number is purchased first (Twilio-side router), then
imported into Vapi here, which (a) registers the number with Vapi using
the brand's Twilio creds, and (b) sets ``server.url`` to hq-x's webhook
ingress so all events for that number land here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.config import settings
from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError
from app.providers.vapi.errors import raise_vapi_error, vapi_key
from app.services import brands as brands_svc

router = APIRouter(
    prefix="/api/brands/{brand_id}/vapi/phone-numbers",
    tags=["vapi-phone-numbers"],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class VapiPhoneNumberImportRequest(BaseModel):
    voice_phone_number_id: UUID
    assistant_id: UUID | None = None
    model_config = {"extra": "forbid"}


class VapiPhoneNumberBindRequest(BaseModel):
    assistant_id: UUID | None = None
    server_url_override: str | None = None
    model_config = {"extra": "forbid"}


_LOCAL_COLS = [
    "id", "brand_id", "phone_number", "twilio_phone_number_sid",
    "vapi_phone_number_id", "voice_assistant_id", "label", "purpose",
    "status", "created_at", "updated_at",
]


def _row(r: tuple) -> dict[str, Any]:
    return dict(zip(_LOCAL_COLS, r, strict=True))


def _server_url() -> str:
    base = (settings.HQX_API_BASE_URL or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail={"error": "HQX_API_BASE_URL not configured"},
        )
    return f"{base}/api/v1/vapi/webhook"


async def _resolve_creds(brand_id: UUID) -> brands_svc.BrandTwilioCreds:
    try:
        creds = await brands_svc.get_twilio_creds(brand_id)
    except brands_svc.BrandCredsKeyMissing as exc:
        raise HTTPException(status_code=503, detail={"error": str(exc)}) from exc
    if creds is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "Brand has no Twilio credentials configured"},
        )
    return creds


async def _load_local(brand_id: UUID, voice_phone_number_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_LOCAL_COLS)}
                FROM voice_phone_numbers
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "phone_number_not_found"})
    return _row(row)


async def _resolve_vapi_assistant_id(brand_id: UUID, assistant_id: UUID) -> str:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT vapi_assistant_id
                FROM voice_assistants
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "assistant_not_found_in_brand"},
        )
    if not row[0]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "assistant_not_synced",
                "message": "assistant has no vapi_assistant_id; sync it first",
            },
        )
    return row[0]


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/import", status_code=201)
async def import_into_vapi(
    brand_id: UUID,
    body: VapiPhoneNumberImportRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Register a Twilio-acquired number in Vapi + wire serverUrl + optional assistant binding."""
    local = await _load_local(brand_id, body.voice_phone_number_id)
    if not local["twilio_phone_number_sid"]:
        raise HTTPException(
            status_code=400,
            detail={"error": "phone_number_not_twilio_owned"},
        )
    if local["vapi_phone_number_id"]:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "already_imported",
                "vapi_phone_number_id": local["vapi_phone_number_id"],
            },
        )

    creds = await _resolve_creds(brand_id)
    api_key = vapi_key()
    server_url = _server_url()

    resolved_vapi_assistant_id: str | None = None
    if body.assistant_id is not None:
        resolved_vapi_assistant_id = await _resolve_vapi_assistant_id(
            brand_id, body.assistant_id,
        )

    try:
        imported = vapi_client.import_phone_number(
            api_key,
            "twilio",
            local["phone_number"],
            creds.account_sid,
            creds.auth_token,
        )
    except VapiProviderError as exc:
        raise_vapi_error("import_phone_number", exc)

    vapi_id = imported.get("id")
    if not vapi_id:
        raise HTTPException(
            status_code=502,
            detail={"error": "vapi_import_returned_no_id"},
        )

    try:
        vapi_client.update_phone_number(
            api_key,
            vapi_id,
            server_url=server_url,
            assistant_id=resolved_vapi_assistant_id,
        )
    except VapiProviderError as exc:
        raise_vapi_error("update_phone_number", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE voice_phone_numbers
                SET vapi_phone_number_id = %s,
                    voice_assistant_id = COALESCE(%s, voice_assistant_id),
                    updated_at = NOW()
                WHERE id = %s AND brand_id = %s
                RETURNING {", ".join(_LOCAL_COLS)}
                """,
                (
                    vapi_id,
                    str(body.assistant_id) if body.assistant_id else None,
                    str(body.voice_phone_number_id),
                    str(brand_id),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()

    return {
        "local": _row(row),
        "vapi_phone_number_id": vapi_id,
        "server_url": server_url,
        "vapi_response": imported,
    }


@router.get("")
async def list_brand_vapi_numbers(
    brand_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> list[dict[str, Any]]:
    """List local rows that have a Vapi mirror."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {", ".join(_LOCAL_COLS)}
                FROM voice_phone_numbers
                WHERE brand_id = %s
                  AND deleted_at IS NULL
                  AND vapi_phone_number_id IS NOT NULL
                ORDER BY created_at
                """,
                (str(brand_id),),
            )
            rows = await cur.fetchall()
    return [_row(r) for r in rows]


@router.get("/{voice_phone_number_id}")
async def get_vapi_number(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Combined view: local row + Vapi's view of the number."""
    local = await _load_local(brand_id, voice_phone_number_id)
    if not local["vapi_phone_number_id"]:
        raise HTTPException(
            status_code=404,
            detail={"error": "phone_number_not_imported_to_vapi"},
        )
    api_key = vapi_key()
    try:
        vapi_view = vapi_client.get_phone_number(api_key, local["vapi_phone_number_id"])
    except VapiProviderError as exc:
        raise_vapi_error("get_phone_number", exc)
    return {"local": local, "vapi": vapi_view}


@router.patch("/{voice_phone_number_id}/bind")
async def bind_assistant(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    body: VapiPhoneNumberBindRequest,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    """Re-bind: sets Vapi's assistantId + (re-)asserts our serverUrl.

    Idempotent: callable repeatedly to recover from server-URL drift even
    if no assistant change is needed.
    """
    local = await _load_local(brand_id, voice_phone_number_id)
    if not local["vapi_phone_number_id"]:
        raise HTTPException(
            status_code=409,
            detail={"error": "phone_number_not_imported_to_vapi"},
        )

    api_key = vapi_key()
    server_url = body.server_url_override or _server_url()

    resolved_vapi_assistant_id: str | None = None
    if body.assistant_id is not None:
        resolved_vapi_assistant_id = await _resolve_vapi_assistant_id(
            brand_id, body.assistant_id,
        )

    try:
        result = vapi_client.update_phone_number(
            api_key,
            local["vapi_phone_number_id"],
            server_url=server_url,
            assistant_id=resolved_vapi_assistant_id,
        )
    except VapiProviderError as exc:
        raise_vapi_error("update_phone_number", exc)

    if body.assistant_id is not None:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    UPDATE voice_phone_numbers
                    SET voice_assistant_id = %s, updated_at = NOW()
                    WHERE id = %s AND brand_id = %s
                    RETURNING {", ".join(_LOCAL_COLS)}
                    """,
                    (
                        str(body.assistant_id),
                        str(voice_phone_number_id),
                        str(brand_id),
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()
        local = _row(row)

    return {"local": local, "server_url": server_url, "vapi_response": result}


@router.delete("/{voice_phone_number_id}", status_code=status.HTTP_204_NO_CONTENT)
async def release_from_vapi(
    brand_id: UUID,
    voice_phone_number_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> None:
    """Delete the Vapi side only. Release the Twilio side via the phone_numbers router."""
    local = await _load_local(brand_id, voice_phone_number_id)
    if not local["vapi_phone_number_id"]:
        raise HTTPException(
            status_code=404,
            detail={"error": "phone_number_not_imported_to_vapi"},
        )

    api_key = vapi_key()
    try:
        vapi_client.delete_phone_number(api_key, local["vapi_phone_number_id"])
    except VapiProviderError as exc:
        raise_vapi_error("delete_phone_number", exc)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE voice_phone_numbers
                SET vapi_phone_number_id = NULL, updated_at = NOW()
                WHERE id = %s AND brand_id = %s
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
        await conn.commit()
