"""Dual-write service for call completion analytics.

Records call data to both Supabase (call_logs — handled by caller) and
ClickHouse (call_events — fire-and-forget here). Never raises.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app import clickhouse

logger = logging.getLogger(__name__)


def record_call_completion(
    brand_id: str,
    call_data: dict[str, Any],
    cost_breakdown: dict[str, Any],
) -> None:
    """Write call completion data to ClickHouse call_events table.

    Fire-and-forget: logs warnings on failure but never raises.
    Postgres call_logs is already updated by the caller (webhook handler).
    """
    row: dict[str, Any] = {
        "event_id": str(uuid.uuid4()),
        "brand_id": brand_id,
        "partner_id": str(call_data.get("partner_id") or ""),
        "campaign_id": str(call_data.get("campaign_id") or ""),
        "call_sid": call_data.get("call_sid", "") or "",
        "direction": call_data.get("direction", "outbound") or "outbound",
        "amd_strategy": call_data.get("amd_strategy", "") or "",
        "amd_result": call_data.get("amd_result", "") or "",
        "outcome": call_data.get("outcome", "") or "",
        "duration_seconds": int(call_data.get("duration_seconds") or 0),
        "cost_transport": float(cost_breakdown.get("transport") or 0),
        "cost_stt": float(cost_breakdown.get("stt") or 0),
        "cost_llm": float(cost_breakdown.get("llm") or 0),
        "cost_tts": float(cost_breakdown.get("tts") or 0),
        "cost_vapi": float(cost_breakdown.get("vapi") or 0),
        "cost_total": float(cost_breakdown.get("total") or 0),
        "vapi_call_id": call_data.get("vapi_call_id", "") or "",
        "ended_reason": call_data.get("ended_reason", "") or "",
        "success_evaluation": call_data.get("success_evaluation", "") or "",
    }

    if not clickhouse.insert_row("call_events", row):
        logger.info(
            "call_analytics_write_skipped",
            extra={"brand_id": brand_id, "vapi_call_id": row["vapi_call_id"]},
        )
