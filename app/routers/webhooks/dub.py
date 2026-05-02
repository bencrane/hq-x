"""Dub webhook receiver.

Path: POST /webhooks/dub. Mirrors the Lob receiver flow:
  1. Verify HMAC signature (mode-driven: enforce / permissive_audit / disabled)
  2. Parse + schema-check the payload
  3. Dedup-insert into webhook_events keyed on Dub's event id
  4. Project into dmaas_dub_events (append-only)
  5. Mark webhook_events.status = processed | dead_letter

Dub's HMAC has no signed timestamp, so there is no replay window — the
unique index on (provider_slug, event_key) is the only deduplication.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.observability import incr_metric, log_event
from app.webhooks.dub_processor import project_dub_event
from app.webhooks.dub_signature import validate_dub_payload_schema, verify_dub_signature

logger = logging.getLogger(__name__)

router = APIRouter()


def _request_id(request: Request) -> str | None:
    return request.headers.get("X-Request-ID") or request.headers.get("X-Request-Id")


async def _store_webhook_event(
    *,
    event_key: str,
    event_type: str,
    schema_version: str,
    request_id: str | None,
    payload: dict[str, Any],
) -> tuple[UUID, bool]:
    """Insert into webhook_events. Returns (id, inserted).

    `inserted` is False on UniqueViolation (we already saw this event_key);
    the returned id is then the existing row's id.
    """
    try:
        async with get_db_connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO webhook_events
                    (provider_slug, event_key, event_type, status,
                     replay_count, payload, schema_version, request_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    "dub",
                    event_key,
                    event_type,
                    "received",
                    0,
                    Jsonb(payload),
                    schema_version,
                    request_id,
                ),
            )
            row = await cur.fetchone()
            return row[0], True
    except UniqueViolation:
        async with get_db_connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM webhook_events WHERE provider_slug = %s AND event_key = %s",
                ("dub", event_key),
            )
            row = await cur.fetchone()
            if row is None:
                raise
            return row[0], False


async def _mark_webhook_event(
    *,
    event_db_id: UUID,
    status_value: str,
    reason_code: str | None = None,
    error: str | None = None,
) -> None:
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            UPDATE webhook_events
            SET status = %s,
                reason_code = COALESCE(%s, reason_code),
                last_error = COALESCE(%s, last_error),
                processed_at = NOW()
            WHERE id = %s
            """,
            (status_value, reason_code, error, str(event_db_id)),
        )


@router.post("/dub")
async def receive_dub_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    request_id = _request_id(request)

    incr_metric("webhook.events.received", provider_slug="dub")

    sig_result = verify_dub_signature(raw_body=raw_body, request=request, request_id=request_id)

    try:
        payload_any = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        incr_metric("webhook.events.rejected", provider_slug="dub", reason="malformed_body")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "dub",
                "reason": "malformed_body",
            },
        ) from exc

    if not isinstance(payload_any, dict):
        incr_metric("webhook.events.rejected", provider_slug="dub", reason="payload_not_object")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "dub",
                "reason": "payload_not_object",
            },
        )
    payload: dict[str, Any] = payload_any

    try:
        schema_version, identity = validate_dub_payload_schema(payload)
    except ValueError as exc:
        reason = str(exc)
        incr_metric(
            "webhook.events.rejected", provider_slug="dub", reason=reason.split(":", 1)[0]
        )
        log_event(
            "dub_webhook_schema_invalid",
            level=logging.WARNING,
            request_id=request_id,
            reason=reason,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "dub",
                "reason": reason,
                "signature": sig_result,
            },
        ) from exc

    event_id = identity["event_id"]
    event_type = identity["event_type"]

    event_db_id, inserted = await _store_webhook_event(
        event_key=event_id,
        event_type=event_type,
        schema_version=schema_version,
        request_id=request_id,
        payload={"_signature": sig_result, **payload},
    )
    if not inserted:
        incr_metric("webhook.duplicate_ignored", provider_slug="dub")
        log_event(
            "dub_webhook_duplicate_ignored",
            request_id=request_id,
            event_key=event_id,
            event_type=event_type,
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "duplicate_ignored",
                "event_key": event_id,
                "event_type": event_type,
                "signature": sig_result,
            },
        )

    incr_metric("webhook.events.accepted", provider_slug="dub")

    try:
        projection = await project_dub_event(
            payload=payload,
            event_id=event_id,
            event_type=event_type,
            occurred_at=identity["created_at"],
            webhook_event_id=event_db_id,
        )
    except Exception as exc:  # noqa: BLE001
        await _mark_webhook_event(
            event_db_id=event_db_id,
            status_value="dead_letter",
            reason_code="projection_failed",
            error=str(exc)[:500],
        )
        incr_metric("webhook.dead_letter.created", provider_slug="dub", reason="projection_failed")
        log_event(
            "dub_webhook_projection_failed",
            level=logging.ERROR,
            request_id=request_id,
            event_key=event_id,
            error=str(exc)[:500],
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "dead_letter",
                "event_key": event_id,
                "event_type": event_type,
                "reason": "projection_failed",
                "signature": sig_result,
            },
        )

    await _mark_webhook_event(event_db_id=event_db_id, status_value="processed")
    incr_metric("dub.event.projected", event_type=event_type)
    log_event(
        "dub_webhook_processed",
        request_id=request_id,
        event_key=event_id,
        event_type=event_type,
        outcome="processed",
        projection=projection.get("status"),
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": "processed",
            "event_key": event_id,
            "event_type": event_type,
            "projection": projection,
            "signature": sig_result,
        },
    )
