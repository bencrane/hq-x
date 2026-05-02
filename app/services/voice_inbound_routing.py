"""Inbound call routing service.

Resolves which assistant should handle an inbound call based on the called
phone number. Identifies the caller from prior call_logs to provide
context to the AI assistant.

Note vs OEX: caller identification from leads is bracketed because the
campaigns/leads tables aren't yet ported into hq-x. The previous-call-outcome
enrichment via call_logs is preserved.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.db import get_db_connection

logger = logging.getLogger(__name__)


async def resolve_inbound_assistant(
    to_phone: str,
    from_phone: str,
    *,
    brand_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Resolve assistant config for an inbound call.

    Args:
        to_phone: The number that was called (ours).
        from_phone: The caller's number.
        brand_id: Optional scoping; webhook receivers usually resolve this
            up-front via app.services.brands.get_brand_id_by_phone_number.

    Returns:
        Routing dict with brand_id, assistant_config, caller_context, etc.
        None if the called number isn't configured for inbound routing.
    """
    config = await _lookup_phone_config(to_phone, brand_id)
    if config is None:
        logger.info(
            "inbound_routing_phone_not_found",
            extra={"phone_number": to_phone, "brand_id": str(brand_id) if brand_id else None},
        )
        return None

    resolved_brand_id = config["brand_id"]
    voice_assistant_id = config["voice_assistant_id"]

    assistant = await _lookup_assistant(voice_assistant_id, resolved_brand_id)
    if assistant is None:
        logger.warning(
            "inbound_routing_assistant_not_found",
            extra={
                "brand_id": str(resolved_brand_id),
                "voice_assistant_id": str(voice_assistant_id),
            },
        )
        return None

    caller_context = await _identify_caller(resolved_brand_id, from_phone)

    if assistant.get("vapi_assistant_id"):
        assistant_config: dict[str, Any] = {"assistantId": assistant["vapi_assistant_id"]}
    else:
        assistant_config = _build_inline_config(assistant)

    return {
        "brand_id": str(resolved_brand_id),
        "assistant_config": assistant_config,
        "caller_context": caller_context,
        "routing_mode": config.get("routing_mode") or "static",
        "first_message_mode": config.get("first_message_mode"),
        "inbound_overrides": config.get("inbound_config"),
    }


async def _lookup_phone_config(
    to_phone: str, brand_id: UUID | None
) -> dict[str, Any] | None:
    """Find the active voice_assistant_phone_configs row for `to_phone`.

    Returns None if there is no match. If multiple matches exist (which
    should never happen given the unique partial index, but we still
    defend in case the index is dropped), returns None and logs an
    error.
    """
    sql = """
        SELECT id, brand_id, voice_assistant_id, partner_id,
               routing_mode, first_message_mode, inbound_config
        FROM voice_assistant_phone_configs
        WHERE phone_number = %s
          AND deleted_at IS NULL
          AND is_active = TRUE
    """
    params: list[Any] = [to_phone]
    if brand_id is not None:
        sql += " AND brand_id = %s"
        params.append(str(brand_id))
    sql += " LIMIT 2"

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(params))
            rows = await cur.fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        logger.error(
            "inbound_routing_phone_ambiguous",
            extra={"phone_number": to_phone, "matches": len(rows)},
        )
        return None

    row = rows[0]
    return {
        "id": row[0],
        "brand_id": row[1],
        "voice_assistant_id": row[2],
        "partner_id": row[3],
        "routing_mode": row[4],
        "first_message_mode": row[5],
        "inbound_config": row[6],
    }


async def _lookup_assistant(
    voice_assistant_id: UUID, brand_id: UUID
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, brand_id, name, vapi_assistant_id, system_prompt,
                       first_message, first_message_mode,
                       model_config, voice_config, transcriber_config,
                       tools_config, analysis_config, max_duration_seconds,
                       metadata
                FROM voice_assistants
                WHERE id = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(voice_assistant_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "brand_id": row[1],
        "name": row[2],
        "vapi_assistant_id": row[3],
        "system_prompt": row[4],
        "first_message": row[5],
        "first_message_mode": row[6],
        "model_config": row[7],
        "voice_config": row[8],
        "transcriber_config": row[9],
        "tools_config": row[10],
        "analysis_config": row[11],
        "max_duration_seconds": row[12],
        "metadata": row[13],
    }


async def _identify_caller(brand_id: UUID, from_phone: str) -> dict[str, Any] | None:
    """Caller identification.

    Phase 1: previous-call-outcome enrichment via call_logs only. Lead
    enrichment from `partners`/campaigns/leads is deferred until the
    campaigns surface ports.
    """
    if not from_phone:
        return None
    previous_outcome = await _get_previous_call_outcome(brand_id, from_phone)
    if previous_outcome is None:
        return None
    return {
        "name": None,
        "company": None,
        "email": None,
        "campaign_history": [],
        "previous_outcome": previous_outcome,
    }


async def _get_previous_call_outcome(brand_id: UUID, phone: str) -> str | None:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT outcome
                    FROM call_logs
                    WHERE brand_id = %s
                      AND customer_number = %s
                      AND deleted_at IS NULL
                      AND outcome IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(brand_id), phone),
                )
                row = await cur.fetchone()
        if row is not None:
            return row[0]
    except Exception as exc:
        logger.warning(
            "previous_call_outcome_lookup_failed",
            extra={"phone": phone, "error": str(exc)},
        )
    return None


def _build_inline_config(assistant: dict[str, Any]) -> dict[str, Any]:
    """Build an inline Vapi assistant config from DB fields."""
    config: dict[str, Any] = {"name": assistant["name"]}

    if assistant.get("system_prompt"):
        model_cfg = dict(assistant.get("model_config") or {})
        model_cfg["messages"] = [
            {"role": "system", "content": assistant["system_prompt"]}
        ]
        config["model"] = model_cfg

    if assistant.get("first_message"):
        config["firstMessage"] = assistant["first_message"]
    if assistant.get("first_message_mode"):
        config["firstMessageMode"] = assistant["first_message_mode"]
    if assistant.get("voice_config"):
        config["voice"] = assistant["voice_config"]
    if assistant.get("transcriber_config"):
        config["transcriber"] = assistant["transcriber_config"]
    if assistant.get("tools_config"):
        config["tools"] = assistant["tools_config"]
    if assistant.get("analysis_config"):
        config["analysisPlan"] = assistant["analysis_config"]
    if assistant.get("max_duration_seconds"):
        config["maxDurationSeconds"] = assistant["max_duration_seconds"]
    if assistant.get("metadata"):
        config["metadata"] = assistant["metadata"]

    return config
