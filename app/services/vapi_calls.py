"""Vapi-orchestrated outbound calls service.

Distinct from ``app/services/outbound_calls.py``, which is Twilio-driven
(Twilio places the leg, hq-x answers Twilio's TwiML callback). Here Vapi
places the leg via POST /call directly, and the call_logs row carries
``vapi_call_id`` rather than ``twilio_call_sid``.

Idempotency contract: every caller passes a per-call ``idempotency_key``.
The first invocation pre-creates a ``call_logs`` row + a
``vapi_call_idempotency`` row, then dispatches POST /call. Replays with
the same key short-circuit and return the original call_log without
re-invoking Vapi.

Failure semantics: if the Vapi POST raises after we've inserted the
ledger rows, we delete both so the key remains free for a retry.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID, uuid4

from app.db import get_db_connection
from app.providers.vapi import client as vapi_client
from app.providers.vapi._http import VapiProviderError

logger = logging.getLogger(__name__)


_CALL_LOG_COLS = [
    "id", "brand_id", "partner_id", "campaign_id",
    "voice_assistant_id", "voice_phone_number_id",
    "vapi_call_id", "twilio_call_sid",
    "direction", "call_type",
    "customer_number", "from_number",
    "status", "outcome",
    "started_at", "ended_at", "duration_seconds",
    "metadata",
    "created_at", "updated_at",
]


def _call_log_row(r: tuple) -> dict[str, Any]:
    return dict(zip(_CALL_LOG_COLS, r, strict=True))


class VapiOutboundValidationError(Exception):
    """Validation failure converted to HTTP 400 by the router."""

    def __init__(self, error_key: str, message: str | None = None) -> None:
        super().__init__(message or error_key)
        self.error_key = error_key


async def _resolve_assistant(
    brand_id: UUID, assistant_id: UUID,
) -> str:
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
        raise VapiOutboundValidationError("assistant_not_found_in_brand")
    if not row[0]:
        raise VapiOutboundValidationError(
            "assistant_not_synced",
            "assistant has no vapi_assistant_id; sync it first",
        )
    return row[0]


async def _resolve_phone_number(
    brand_id: UUID, voice_phone_number_id: UUID,
) -> tuple[str, str | None]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT vapi_phone_number_id, phone_number
                FROM voice_phone_numbers
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                """,
                (str(voice_phone_number_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise VapiOutboundValidationError("phone_number_not_found_in_brand")
    if not row[0]:
        raise VapiOutboundValidationError(
            "phone_number_not_imported_to_vapi",
            "phone number has no vapi_phone_number_id; import it first",
        )
    return row[0], row[1]


async def _lookup_existing_idempotency(
    brand_id: UUID, idempotency_key: str,
) -> dict[str, Any] | None:
    """Return the call_log + cached idempotency state if a row exists."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT i.call_log_id, i.vapi_call_id
                FROM vapi_call_idempotency i
                WHERE i.brand_id = %s AND i.idempotency_key = %s
                """,
                (str(brand_id), idempotency_key),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            call_log_id, cached_vapi_call_id = row
            await cur.execute(
                f"""
                SELECT {", ".join(_CALL_LOG_COLS)}
                FROM call_logs
                WHERE id = %s
                """,
                (str(call_log_id),),
            )
            log_row = await cur.fetchone()
    if log_row is None:
        return None
    return {
        "call_log": _call_log_row(log_row),
        "cached_vapi_call_id": cached_vapi_call_id,
    }


async def initiate_vapi_call(
    *,
    api_key: str,
    brand_id: UUID,
    assistant_id: UUID,
    voice_phone_number_id: UUID,
    customer_number: str,
    customer_name: str | None = None,
    customer_external_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    partner_id: UUID | None = None,
    campaign_id: UUID | None = None,
    assistant_overrides: dict[str, Any] | None = None,
    idempotency_key: str,
) -> dict[str, Any]:
    """Create an outbound call via Vapi and return the local + Vapi state.

    Replays with the same ``(brand_id, idempotency_key)`` skip the Vapi
    POST and return the cached call_log + (if known) Vapi response.
    """
    if not idempotency_key:
        raise VapiOutboundValidationError("idempotency_key_required")

    existing = await _lookup_existing_idempotency(brand_id, idempotency_key)
    if existing is not None:
        return {
            "call_log": existing["call_log"],
            "vapi_response": None,
            "idempotent_replay": True,
            "cached_vapi_call_id": existing["cached_vapi_call_id"],
        }

    vapi_assistant_id = await _resolve_assistant(brand_id, assistant_id)
    vapi_phone_number_id, _ = await _resolve_phone_number(
        brand_id, voice_phone_number_id,
    )

    enriched_metadata: dict[str, Any] = dict(metadata or {})
    enriched_metadata["idempotency_key"] = idempotency_key
    if customer_name is not None:
        enriched_metadata.setdefault("customer_name", customer_name)
    if customer_external_id is not None:
        enriched_metadata.setdefault("customer_external_id", customer_external_id)

    call_log_id = uuid4()
    idempotency_id = uuid4()

    # 1. Pre-create the call_logs row + idempotency ledger row in one txn.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO call_logs (
                    id, brand_id, partner_id, campaign_id,
                    voice_assistant_id, voice_phone_number_id,
                    direction, call_type,
                    customer_number,
                    status, metadata
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s, %s,
                    'outbound', 'outbound',
                    %s,
                    'queued', %s::jsonb
                )
                RETURNING {", ".join(_CALL_LOG_COLS)}
                """,
                (
                    str(call_log_id), str(brand_id),
                    str(partner_id) if partner_id else None,
                    str(campaign_id) if campaign_id else None,
                    str(assistant_id), str(voice_phone_number_id),
                    customer_number,
                    json.dumps(enriched_metadata),
                ),
            )
            log_row = await cur.fetchone()

            # ON CONFLICT covers the race-loser case: a parallel request
            # with the same idempotency_key already inserted; we surface
            # the winner's call_log instead of pushing through.
            await cur.execute(
                """
                INSERT INTO vapi_call_idempotency (
                    id, brand_id, idempotency_key, call_log_id, vapi_call_id
                )
                VALUES (%s, %s, %s, %s, NULL)
                ON CONFLICT (brand_id, idempotency_key) DO NOTHING
                RETURNING id
                """,
                (
                    str(idempotency_id), str(brand_id),
                    idempotency_key, str(call_log_id),
                ),
            )
            ledger_inserted = await cur.fetchone()
        await conn.commit()

    if ledger_inserted is None:
        # We lost the race — the winner's row owns the key. Roll back the
        # call_logs row we just pre-created, then return the winner.
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM call_logs WHERE id = %s",
                    (str(call_log_id),),
                )
            await conn.commit()
        existing = await _lookup_existing_idempotency(brand_id, idempotency_key)
        if existing is None:
            # Extremely unlikely (winner's row vanished), but be explicit.
            raise VapiOutboundValidationError(
                "idempotency_race_lost_no_winner",
                "lost race but winner row not found",
            )
        return {
            "call_log": existing["call_log"],
            "vapi_response": None,
            "idempotent_replay": True,
            "cached_vapi_call_id": existing["cached_vapi_call_id"],
        }

    # 2. Dispatch to Vapi. On failure, drop the rows so the key is free.
    overrides = dict(assistant_overrides or {})
    try:
        result = vapi_client.create_call(
            api_key,
            vapi_assistant_id,
            customer_number,
            vapi_phone_number_id,
            **overrides,
        )
    except VapiProviderError:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM vapi_call_idempotency WHERE id = %s",
                    (str(idempotency_id),),
                )
                await cur.execute(
                    "DELETE FROM call_logs WHERE id = %s",
                    (str(call_log_id),),
                )
            await conn.commit()
        raise

    real_vapi_call_id = (result or {}).get("id")

    # 3. Stamp the real Vapi call id on both the call_logs row and the
    # idempotency ledger row.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE call_logs
                SET vapi_call_id = %s, updated_at = NOW()
                WHERE id = %s
                RETURNING {", ".join(_CALL_LOG_COLS)}
                """,
                (real_vapi_call_id, str(call_log_id)),
            )
            log_row = await cur.fetchone()
            await cur.execute(
                """
                UPDATE vapi_call_idempotency
                SET vapi_call_id = %s
                WHERE id = %s
                """,
                (real_vapi_call_id, str(idempotency_id)),
            )
        await conn.commit()

    logger.info(
        "vapi_outbound_call_initiated brand=%s call_log=%s vapi_call_id=%s",
        brand_id, call_log_id, real_vapi_call_id,
    )

    return {
        "call_log": _call_log_row(log_row),
        "vapi_response": result,
        "idempotent_replay": False,
    }
