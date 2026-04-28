"""Voice campaign batch — unit-of-work functions.

This module ports the *callable* unit-of-work helpers out of the OEX
``voice_campaign_batch.py`` (456 LOC). Per directive §10 item #2, the
batch loop itself (``execute_voice_campaign_batch``) is **deliberately
not ported** — a Trigger.dev task in ``src/trigger/`` will compose these
unit-of-work functions instead.

What lives here:
  * ``load_campaign_config(brand_id, campaign_id)`` — voice_ai_campaign_configs
  * ``count_active_calls(brand_id, campaign_id)`` — concurrency check helper
  * ``is_within_call_window(config, now=None)`` — pure timezone helper
  * ``record_call_initiated(...)`` — inserts voice_campaign_active_calls row
    and pre-creates the call_logs row that the Vapi/Twilio webhook handlers
    will later UPDATE.

What is intentionally **not** here:
  * lead selection from ``campaign_lead_progress`` — that table is part of
    the OEX orchestrator surface and is deferred (§10 #2). When the
    Trigger.dev task wraps this module, it will own its own lead-selection
    pass against whatever lead-progress table hq-x ultimately ships.
  * the ``execute_voice_campaign_batch`` driver loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Any
from uuid import UUID, uuid4

try:
    import zoneinfo
    ZoneInfo = zoneinfo.ZoneInfo
except ImportError:  # pragma: no cover — Python <3.9 fallback
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

from app.db import get_db_connection

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Config + concurrency helpers
# ---------------------------------------------------------------------------


async def load_campaign_config(
    brand_id: UUID, campaign_id: UUID
) -> dict[str, Any] | None:
    """Load the ``voice_ai_campaign_configs`` row for a campaign."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, voice_assistant_id, voice_phone_number_id,
                       amd_strategy, max_concurrent_calls,
                       call_window_start, call_window_end,
                       call_window_timezone, retry_policy
                FROM voice_ai_campaign_configs
                WHERE brand_id = %s AND campaign_id = %s
                  AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(brand_id), str(campaign_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "voice_assistant_id": row[1],
        "voice_phone_number_id": row[2],
        "amd_strategy": row[3],
        "max_concurrent_calls": row[4],
        "call_window_start": row[5],
        "call_window_end": row[6],
        "call_window_timezone": row[7],
        "retry_policy": row[8],
    }


async def count_active_calls(brand_id: UUID, campaign_id: UUID) -> int:
    """Count non-completed campaign calls — used by concurrency-limit checks."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*)
                FROM voice_campaign_active_calls
                WHERE brand_id = %s AND campaign_id = %s
                  AND status <> 'completed'
                  AND deleted_at IS NULL
                """,
                (str(brand_id), str(campaign_id)),
            )
            row = await cur.fetchone()
    return int(row[0]) if row else 0


def is_within_call_window(
    config: dict[str, Any], *, now: datetime | None = None
) -> bool:
    """Return True if the current local time is within the configured
    [call_window_start, call_window_end] window. If either bound is
    missing, the window is unrestricted (returns True).
    """
    start = config.get("call_window_start")
    end = config.get("call_window_end")
    if start is None or end is None:
        return True

    tz_name = config.get("call_window_timezone") or "America/New_York"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("America/New_York")

    now_utc = now if now is not None else _now_utc()
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    local_now = now_utc.astimezone(tz).time()

    if isinstance(start, str):
        start = time.fromisoformat(start)
    if isinstance(end, str):
        end = time.fromisoformat(end)

    return start <= local_now <= end


# ---------------------------------------------------------------------------
# Per-call unit of work
# ---------------------------------------------------------------------------


async def record_call_initiated(
    *,
    brand_id: UUID,
    campaign_id: UUID,
    call_id: str,
    provider: str,
    customer_number: str,
    from_number: str | None = None,
    voice_assistant_id: UUID | None = None,
    voice_phone_number_id: UUID | None = None,
    partner_id: UUID | None = None,
    amd_strategy: str | None = None,
    extra_call_log: dict[str, Any] | None = None,
) -> tuple[UUID, UUID]:
    """Insert a voice_campaign_active_calls row + a pre-created call_logs row.

    Returns ``(active_call_id, call_log_id)``. The ``call_logs`` row carries
    either ``vapi_call_id`` or ``twilio_call_sid`` depending on provider —
    later webhook events will UPDATE the same row in place.

    Raises any underlying DB error; caller is responsible for back-out.
    """
    if provider not in ("vapi", "twilio"):
        raise ValueError(f"unknown provider: {provider!r}")

    active_call_id = uuid4()
    metadata: dict[str, Any] = {}
    if amd_strategy is not None:
        metadata["amd_strategy"] = amd_strategy
    if extra_call_log:
        metadata.update(extra_call_log)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO voice_campaign_active_calls (
                    id, brand_id, campaign_id, call_id, provider, status
                )
                VALUES (%s, %s, %s, %s, %s, 'initiated')
                """,
                (
                    str(active_call_id), str(brand_id), str(campaign_id),
                    call_id, provider,
                ),
            )

            await cur.execute(
                """
                INSERT INTO call_logs (
                    brand_id, partner_id, campaign_id,
                    voice_assistant_id, voice_phone_number_id,
                    direction, customer_number, from_number,
                    status, vapi_call_id, twilio_call_sid, metadata
                )
                VALUES (
                    %s, %s, %s, %s, %s,
                    'outbound', %s, %s, 'queued',
                    %s, %s, %s::jsonb
                )
                RETURNING id
                """,
                (
                    str(brand_id),
                    str(partner_id) if partner_id else None,
                    str(campaign_id),
                    str(voice_assistant_id) if voice_assistant_id else None,
                    str(voice_phone_number_id) if voice_phone_number_id else None,
                    customer_number,
                    from_number,
                    call_id if provider == "vapi" else None,
                    call_id if provider == "twilio" else None,
                    _json_or_null(metadata),
                ),
            )
            row = await cur.fetchone()
            call_log_id = row[0]
        await conn.commit()

    logger.info(
        "voice_campaign_call_initiated",
        extra={
            "brand_id": str(brand_id),
            "campaign_id": str(campaign_id),
            "active_call_id": str(active_call_id),
            "call_log_id": str(call_log_id),
            "provider": provider,
            "call_id": call_id,
        },
    )
    return active_call_id, call_log_id


def _json_or_null(d: dict[str, Any]) -> str | None:
    if not d:
        return None
    import json
    return json.dumps(d)
