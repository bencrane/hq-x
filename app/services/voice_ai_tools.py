"""Voice AI tool handlers for Vapi tool-calls webhook.

Each handler returns a flat string per Vapi's 7.5s timeout requirement.
On error, returns an error message string rather than raising.

Drift fix §7.4: lookup_carrier reads settings.DEX_BASE_URL (data-engine-x),
not settings.hypertide_base_url (which was the OEX wiring bug — Hypertide
is email infrastructure, not a carrier API).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from app.config import settings
from app.db import get_db_connection

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_callback_time(preferred_time: str, timezone_str: str) -> datetime:
    """Validate and normalize callback scheduling inputs to a UTC datetime."""
    if not preferred_time.strip():
        raise ValueError("preferred_time is required")
    if not timezone_str.strip():
        raise ValueError("timezone is required")

    try:
        tz = ZoneInfo(timezone_str)
    except Exception as exc:
        raise ValueError(f"invalid timezone: {timezone_str}") from exc

    normalized_time = preferred_time.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_time)
    except ValueError as exc:
        raise ValueError("preferred_time must be a valid ISO-8601 datetime") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Tool: lookup_carrier — drift fix §7.4 (DEX_BASE_URL, not hypertide_base_url)
# ---------------------------------------------------------------------------


def lookup_carrier(dot_number: str, brand_id: str) -> str:
    """Look up carrier info from data-engine-x carrier entity API.

    Returns formatted carrier info as flat string (no newlines).
    """
    try:
        base_url = settings.DEX_BASE_URL or ""
        if not base_url:
            return "error: carrier lookup service not configured"

        url = f"{base_url.rstrip('/')}/api/carriers/{dot_number}"
        with httpx.Client(timeout=10.0) as client:
            response = client.get(url, headers={"X-Brand-Id": brand_id})

        if response.status_code == 404:
            return f"carrier not found for DOT {dot_number}"
        if response.status_code >= 400:
            return f"error: carrier lookup returned HTTP {response.status_code}"

        data = response.json()
        parts: list[str] = []
        if data.get("legal_name"):
            parts.append(f"name: {data['legal_name']}")
        if data.get("dot_number"):
            parts.append(f"DOT: {data['dot_number']}")
        if data.get("mc_number"):
            parts.append(f"MC: {data['mc_number']}")
        if data.get("power_units"):
            parts.append(f"power_units: {data['power_units']}")
        if data.get("drivers"):
            parts.append(f"drivers: {data['drivers']}")
        if data.get("state"):
            parts.append(f"state: {data['state']}")
        if data.get("safety_rating"):
            parts.append(f"safety_rating: {data['safety_rating']}")
        if data.get("operation_classification"):
            parts.append(f"classification: {data['operation_classification']}")

        return (
            ", ".join(parts)
            if parts
            else f"carrier found for DOT {dot_number} but no details available"
        )
    except httpx.TimeoutException:
        return f"error: carrier lookup timed out for DOT {dot_number}"
    except Exception as exc:
        logger.warning("lookup_carrier failed", extra={"error": str(exc)})
        return f"error: carrier lookup failed for DOT {dot_number}"


# ---------------------------------------------------------------------------
# Tool: get_transfer_destination
# ---------------------------------------------------------------------------


async def resolve_transfer_destination(
    *,
    brand_id: str,
    campaign_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the best transfer territory for a brand.

    Tries campaign-specific match first, falls back to brand-level
    (campaign_id IS NULL). Returns the row dict, or None.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if campaign_id:
                await cur.execute(
                    """
                    SELECT id, brand_id, campaign_id, name, rules,
                           destination_phone, destination_label, priority, active
                    FROM transfer_territories
                    WHERE brand_id = %s
                      AND active = TRUE
                      AND deleted_at IS NULL
                      AND campaign_id = %s
                    ORDER BY priority DESC
                    LIMIT 1
                    """,
                    (brand_id, campaign_id),
                )
                row = await cur.fetchone()
                if row is not None:
                    return _territory_row(row)

            await cur.execute(
                """
                SELECT id, brand_id, campaign_id, name, rules,
                       destination_phone, destination_label, priority, active
                FROM transfer_territories
                WHERE brand_id = %s
                  AND active = TRUE
                  AND deleted_at IS NULL
                  AND campaign_id IS NULL
                ORDER BY priority DESC
                LIMIT 1
                """,
                (brand_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return _territory_row(row)
    return None


def _territory_row(row: tuple) -> dict[str, Any]:
    return {
        "id": row[0],
        "brand_id": row[1],
        "campaign_id": row[2],
        "name": row[3],
        "rules": row[4],
        "destination_phone": row[5],
        "destination_label": row[6],
        "priority": row[7],
        "active": row[8],
    }


async def get_transfer_destination(
    dot_number: str, campaign_id: str, brand_id: str
) -> str:
    """Tool-call shape (returns flat string)."""
    destination = await resolve_transfer_destination(
        brand_id=brand_id, campaign_id=campaign_id or None
    )
    if not destination:
        return "no matching transfer destination found"
    label = destination.get("destination_label") or "transfer"
    phone = destination.get("destination_phone", "")
    return f"transfer to {label}: {phone}"


# ---------------------------------------------------------------------------
# Tool: log_call_outcome
# ---------------------------------------------------------------------------


async def log_call_outcome(
    vapi_call_id: str,
    outcome: str,
    notes: str,
    qualification_data: dict[str, Any] | None,
    brand_id: str,
) -> str:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE call_logs
                    SET outcome = %s,
                        analysis_summary = COALESCE(%s, analysis_summary),
                        structured_data = COALESCE(%s::jsonb, structured_data),
                        updated_at = NOW()
                    WHERE vapi_call_id = %s AND brand_id = %s
                    """,
                    (
                        outcome,
                        notes if notes else None,
                        json.dumps(qualification_data) if qualification_data else None,
                        vapi_call_id,
                        brand_id,
                    ),
                )
            await conn.commit()
        return f"call outcome logged: {outcome}"
    except Exception as exc:
        logger.warning("log_call_outcome failed", extra={"error": str(exc)})
        return f"error logging call outcome: {exc}"


# ---------------------------------------------------------------------------
# Tool: schedule_callback
# ---------------------------------------------------------------------------


async def schedule_callback(
    vapi_call_id: str,
    preferred_time: str,
    timezone_str: str,
    notes: str,
    brand_id: str,
) -> str:
    try:
        normalized = _parse_callback_time(preferred_time, timezone_str)

        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id, customer_number, voice_assistant_id,
                           voice_phone_number_id, partner_id
                    FROM call_logs
                    WHERE vapi_call_id = %s AND brand_id = %s
                    LIMIT 1
                    """,
                    (vapi_call_id, brand_id),
                )
                call = await cur.fetchone()

                source_call_log_id = call[0] if call else None
                customer_number = call[1] if call else None
                voice_assistant_id = call[2] if call else None
                voice_phone_number_id = call[3] if call else None
                partner_id = call[4] if call else None

                try:
                    await cur.execute(
                        """
                        INSERT INTO voice_callback_requests (
                            brand_id, partner_id, source_call_log_id,
                            source_vapi_call_id, voice_assistant_id,
                            voice_phone_number_id, customer_number,
                            preferred_time, timezone, notes, status
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'scheduled')
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            brand_id,
                            partner_id,
                            source_call_log_id,
                            vapi_call_id,
                            voice_assistant_id,
                            voice_phone_number_id,
                            customer_number,
                            normalized,
                            timezone_str,
                            notes,
                        ),
                    )
                except Exception as exc:
                    msg = str(exc).lower()
                    if "duplicate" not in msg and "unique" not in msg:
                        raise

                await cur.execute(
                    """
                    UPDATE call_logs
                    SET outcome = 'callback_requested', updated_at = NOW()
                    WHERE vapi_call_id = %s AND brand_id = %s
                    """,
                    (vapi_call_id, brand_id),
                )
            await conn.commit()
        return f"callback scheduled for {preferred_time} {timezone_str}"
    except Exception as exc:
        logger.warning("schedule_callback failed", extra={"error": str(exc)})
        return f"error scheduling callback: {exc}"


# ---------------------------------------------------------------------------
# Tool: check_do_not_call
# ---------------------------------------------------------------------------


async def check_do_not_call(phone_number: str, brand_id: str) -> str:
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT id FROM do_not_call_lists
                    WHERE brand_id = %s AND phone_number = %s AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    (brand_id, phone_number),
                )
                row = await cur.fetchone()
        return "on_dnc_list: true" if row is not None else "on_dnc_list: false"
    except Exception as exc:
        logger.warning("check_do_not_call failed", extra={"error": str(exc)})
        return f"error checking DNC list: {exc}"


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch_tool_calls(
    tool_calls: list[dict[str, Any]],
    brand_id: str | UUID,
) -> list[dict[str, Any]]:
    """Dispatch a list of Vapi tool calls to their handlers.

    Returns list of {toolCallId, result} dicts (Vapi's expected shape).
    """
    bid = str(brand_id)
    results: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        tool_call_id = tool_call.get("id", "")
        function_info = tool_call.get("function", {})
        function_name = function_info.get("name", "")
        arguments = function_info.get("arguments", {})

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except Exception:
                arguments = {}

        try:
            if function_name == "lookup_carrier":
                result: str = lookup_carrier(
                    dot_number=arguments.get("dot_number", ""),
                    brand_id=bid,
                )
            elif function_name == "get_transfer_destination":
                result = await get_transfer_destination(
                    dot_number=arguments.get("dot_number", ""),
                    campaign_id=arguments.get("campaign_id", ""),
                    brand_id=bid,
                )
            elif function_name == "log_call_outcome":
                result = await log_call_outcome(
                    vapi_call_id=arguments.get("vapi_call_id", ""),
                    outcome=arguments.get("outcome", ""),
                    notes=arguments.get("notes", ""),
                    qualification_data=arguments.get("qualification_data"),
                    brand_id=bid,
                )
            elif function_name == "schedule_callback":
                result = await schedule_callback(
                    vapi_call_id=arguments.get("vapi_call_id", ""),
                    preferred_time=arguments.get("preferred_time", ""),
                    timezone_str=arguments.get("timezone", ""),
                    notes=arguments.get("notes", ""),
                    brand_id=bid,
                )
            elif function_name == "check_do_not_call":
                result = await check_do_not_call(
                    phone_number=arguments.get("phone_number", ""),
                    brand_id=bid,
                )
            else:
                result = f"error: unknown tool '{function_name}'"
        except Exception as exc:
            logger.warning(
                "tool_handler_failed",
                extra={"tool": function_name, "error": str(exc)},
            )
            result = f"error: {function_name} failed"

        results.append({"toolCallId": tool_call_id, "result": result})

    return results
