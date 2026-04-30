"""Lob webhook receiver.

Path: POST /webhooks/lob (matches the hq-x convention used by /webhooks/cal
and /webhooks/emailbison; not the OEX /api/webhooks/lob — see ARCHITECTURE).

Flow per event:
  1. Verify HMAC signature (mode-driven: enforce / permissive_audit / disabled)
  2. Parse + schema-check the payload
  3. Compute event_key, dedup-insert into webhook_events
  4. Project into direct_mail_pieces / direct_mail_piece_events / suppressed_addresses
  5. Mark webhook_events.status = processed | dead_letter

Replay: a `POST /webhooks/lob/replay/{event_id}` admin endpoint re-runs step 4
for an existing webhook_events row. Auth gated on `require_operator`.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection
from app.observability import incr_metric, log_event
from app.webhooks.lob_normalization import compute_lob_event_key
from app.webhooks.lob_processor import project_lob_event
from app.webhooks.lob_signature import validate_lob_payload_schema, verify_lob_signature

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
    initial_status: str = "received",
) -> tuple[UUID, bool]:
    """Insert into webhook_events. Returns (id, inserted).

    `inserted` is False on UniqueViolation (we already saw this event_key);
    in that case the returned id is the existing row's id.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO webhook_events
                        (provider_slug, event_key, event_type, status,
                         replay_count, payload, schema_version, request_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        "lob",
                        event_key,
                        event_type,
                        initial_status,
                        0,
                        Jsonb(payload),
                        schema_version,
                        request_id,
                    ),
                )
                row = await cur.fetchone()
                return row[0], True
    except UniqueViolation:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id FROM webhook_events
                    WHERE provider_slug = %s AND event_key = %s
                    """,
                    ("lob", event_key),
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
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
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


@router.post("/lob")
async def receive_lob_webhook(request: Request) -> JSONResponse:
    raw_body = await request.body()
    request_id = _request_id(request)

    incr_metric("webhook.events.received", provider_slug="lob")

    sig_result = verify_lob_signature(raw_body=raw_body, request=request, request_id=request_id)

    try:
        payload_any = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        incr_metric("webhook.events.rejected", provider_slug="lob", reason="malformed_body")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "lob",
                "reason": "malformed_body",
            },
        ) from exc

    if not isinstance(payload_any, dict):
        incr_metric("webhook.events.rejected", provider_slug="lob", reason="payload_not_object")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "lob",
                "reason": "payload_not_object",
            },
        )
    payload: dict[str, Any] = payload_any

    try:
        schema_version, identity = validate_lob_payload_schema(payload)
    except ValueError as exc:
        reason = str(exc)
        incr_metric("webhook.events.rejected", provider_slug="lob", reason=reason.split(":", 1)[0])
        log_event(
            "lob_webhook_schema_invalid",
            level=logging.WARNING,
            request_id=request_id,
            reason=reason,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "type": "webhook_payload_invalid",
                "provider": "lob",
                "reason": reason,
                "signature": sig_result,
            },
        ) from exc

    event_key = compute_lob_event_key(payload, raw_body)
    event_type = identity["event_type"]

    event_db_id, inserted = await _store_webhook_event(
        event_key=event_key,
        event_type=event_type,
        schema_version=schema_version,
        request_id=request_id,
        payload={"_signature": sig_result, **payload},
    )
    if not inserted:
        incr_metric("webhook.duplicate_ignored", provider_slug="lob")
        log_event(
            "lob_webhook_duplicate_ignored",
            request_id=request_id,
            event_key=event_key,
            event_type=event_type,
            event_id=identity["event_id"],
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "status": "duplicate_ignored",
                "event_key": event_key,
                "event_type": event_type,
                "signature": sig_result,
            },
        )

    incr_metric("webhook.events.accepted", provider_slug="lob")

    try:
        projection = await project_lob_event(payload=payload, event_id=identity["event_id"])
    except Exception as exc:  # noqa: BLE001
        await _mark_webhook_event(
            event_db_id=event_db_id,
            status_value="dead_letter",
            reason_code="projection_failed",
            error=str(exc)[:500],
        )
        incr_metric("webhook.dead_letter.created", provider_slug="lob", reason="projection_failed")
        log_event(
            "lob_webhook_projection_failed",
            level=logging.ERROR,
            request_id=request_id,
            event_key=event_key,
            error=str(exc)[:500],
        )
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "status": "dead_letter",
                "event_key": event_key,
                "event_type": event_type,
                "reason": "projection_failed",
                "signature": sig_result,
            },
        )

    final_status = "processed"
    reason_code: str | None = None
    proj_status = projection.get("status")
    if proj_status == "skipped":
        # Could not parse (no resource id at all) — dead_letter so the
        # operator can replay once the receiver is fixed.
        final_status = "dead_letter"
        reason_code = projection.get("reason")
        incr_metric(
            "webhook.dead_letter.created",
            provider_slug="lob",
            reason=reason_code or "skipped",
        )
    elif proj_status == "orphaned":
        # Resource id present but neither piece nor step matched. Surface
        # as a distinct status so operator dashboards can show pending
        # reconciliation work without conflating it with parser failures.
        final_status = "orphaned"
        reason_code = "orphaned"
        incr_metric(
            "webhook.orphaned.created",
            provider_slug="lob",
            normalized_event=projection.get("normalized_event") or "",
        )

    await _mark_webhook_event(
        event_db_id=event_db_id,
        status_value=final_status,
        reason_code=reason_code,
    )

    log_event(
        "lob_webhook_processed",
        request_id=request_id,
        event_key=event_key,
        event_type=event_type,
        outcome=final_status,
        projection=projection.get("status"),
    )

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED,
        content={
            "status": final_status,
            "event_key": event_key,
            "event_type": event_type,
            "projection": projection,
            "signature": sig_result,
        },
    )


@router.post("/lob/replay/{event_id}")
async def replay_lob_event(
    event_id: UUID,
    _user: UserContext = Depends(require_operator),
) -> JSONResponse:
    """Admin: re-run projection for one webhook_events row by id.

    No batch / on-cadence replay in this PR — that's a follow-up. This
    endpoint is the bare minimum so a stuck dead-letter can be unblocked
    after the underlying piece is backfilled.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, payload, replay_count
                FROM webhook_events
                WHERE id = %s AND provider_slug = 'lob'
                """,
                (str(event_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"type": "webhook_event_not_found", "event_id": str(event_id)},
        )

    raw_payload = row[1] or {}
    if isinstance(raw_payload, dict) and "_signature" in raw_payload:
        replay_payload = {k: v for k, v in raw_payload.items() if k != "_signature"}
    else:
        replay_payload = raw_payload if isinstance(raw_payload, dict) else {}

    source_event_id = replay_payload.get("id") or replay_payload.get("event_id") or str(event_id)

    try:
        projection = await project_lob_event(payload=replay_payload, event_id=str(source_event_id))
    except Exception as exc:  # noqa: BLE001
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE webhook_events
                    SET replay_count = replay_count + 1,
                        last_error = %s
                    WHERE id = %s
                    """,
                    (str(exc)[:500], str(event_id)),
                )
        incr_metric("webhook.replay_failed", provider_slug="lob")
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"status": "replay_failed", "error": str(exc)[:500]},
        )

    new_status = "processed" if projection.get("status") != "skipped" else "dead_letter"
    reason_code = projection.get("reason") if new_status == "dead_letter" else None

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE webhook_events
                SET status = %s,
                    reason_code = %s,
                    replay_count = replay_count + 1,
                    processed_at = NOW()
                WHERE id = %s
                """,
                (new_status, reason_code, str(event_id)),
            )
    incr_metric("webhook.replay_processed", provider_slug="lob")

    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "status": new_status,
            "projection": projection,
        },
    )
