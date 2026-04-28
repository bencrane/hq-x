"""Recording storage service.

Archives call recordings from Vapi's temporary URLs to permanent Supabase
Storage. Storage path is `{brand_id}/{call_id}.wav` (vs OEX's `{org_id}/`).

Prerequisite: a `call-recordings` bucket exists in Supabase Storage.
"""

from __future__ import annotations

import logging
from uuid import UUID

import httpx
from supabase import create_client

from app.config import settings
from app.db import get_db_connection

logger = logging.getLogger(__name__)

STORAGE_BUCKET = "call-recordings"


def _supabase_client():
    return create_client(
        str(settings.HQX_SUPABASE_URL).rstrip("/"),
        settings.HQX_SUPABASE_SERVICE_ROLE_KEY.get_secret_value(),
    )


async def archive_recording(brand_id: UUID, call_id: str, source_url: str) -> str:
    """Download recording from Vapi URL and upload to Supabase Storage.

    Returns the permanent public URL.
    """
    storage_path = f"{brand_id}/{call_id}.wav"

    # 1. Download from source URL (streaming to avoid memory issues).
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", source_url) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    chunks.append(chunk)
                file_bytes = b"".join(chunks)
    except Exception as exc:
        logger.warning(
            "recording_download_failed",
            extra={"brand_id": str(brand_id), "call_id": call_id, "error": str(exc)},
        )
        raise

    # 2. Upload to Supabase Storage. supabase-py is sync; small upload, OK to block.
    sb = _supabase_client()
    try:
        sb.storage.from_(STORAGE_BUCKET).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": "audio/wav"},
        )
    except Exception as exc:
        logger.warning(
            "recording_upload_failed",
            extra={"brand_id": str(brand_id), "call_id": call_id, "error": str(exc)},
        )
        raise

    public_url = sb.storage.from_(STORAGE_BUCKET).get_public_url(storage_path)

    # 3. Update call_logs with permanent URL.
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE call_logs
                    SET recording_url = %s, updated_at = NOW()
                    WHERE vapi_call_id = %s AND brand_id = %s
                    """,
                    (public_url, call_id, str(brand_id)),
                )
            await conn.commit()
    except Exception as exc:
        logger.warning(
            "recording_call_log_update_failed",
            extra={"brand_id": str(brand_id), "call_id": call_id, "error": str(exc)},
        )

    return public_url
