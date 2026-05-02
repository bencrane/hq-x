import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.webhooks import storage
from app.webhooks.emailbison_parsing import compute_event_key, extract_event_type
from app.webhooks.emailbison_processor import project_emailbison_event
from app.webhooks.emailbison_trust import verify_emailbison_trust

logger = logging.getLogger(__name__)

router = APIRouter()


async def _safe_project(*, event_id: UUID, payload: dict[str, Any]) -> None:
    try:
        await project_emailbison_event(
            webhook_event_id=event_id, payload=payload
        )
    except Exception:
        logger.exception(
            "emailbison_processor failed event_id=%s", event_id
        )


@router.post("/emailbison")
async def emailbison_missing_path_token() -> JSONResponse:
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "type": "webhook_auth_failed",
            "provider": "emailbison",
            "reason": "missing_path_token",
            "message": "EmailBison webhook requires a secret path token",
        },
    )


@router.post("/emailbison/{path_token}")
async def receive_emailbison_webhook(path_token: str, request: Request) -> JSONResponse:
    raw_body = await request.body()
    origin_host = verify_emailbison_trust(request, path_token)

    malformed_json = False
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError:
        parsed = {"raw_body": raw_body.decode("utf-8", errors="replace")}
        malformed_json = True

    payload = parsed if isinstance(parsed, dict) else {"raw_payload": parsed}
    event_type = extract_event_type(payload)
    event_key = compute_event_key(payload, raw_body)

    enriched = dict(payload)
    enriched["_ingestion"] = {
        "provider_slug": "emailbison",
        "trust_mode": "unsigned_origin_plus_path_token",
        "origin_host": origin_host,
        "received_at": datetime.now(UTC).isoformat(),
        "request_headers": dict(request.headers),
        "raw_body": raw_body.decode("utf-8", errors="replace"),
        "request_id": request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id"),
    }
    if malformed_json:
        enriched["malformed_json"] = True

    try:
        event_id = await storage.insert_emailbison_event(
            event_key=event_key,
            event_type=event_type,
            payload=enriched,
        )
    except storage.DuplicateEventError:
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "duplicate_ignored",
                "event_type": event_type,
                "event_key": event_key,
            },
        )

    if not malformed_json:
        # Fire-and-forget projection: receiver must ack < 1s (coverage doc
        # §4b — EB retry behavior is undocumented). Errors inside the
        # projector are caught + logged in _safe_project.
        asyncio.create_task(_safe_project(event_id=event_id, payload=enriched))

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "accepted",
            "event_type": event_type,
            "event_key": event_key,
            "trust_mode": "unsigned_origin_plus_path_token",
            "non_cryptographic_trust": True,
        },
    )
