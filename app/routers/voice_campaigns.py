"""Voice-AI campaign config + metrics CRUD (brand-axis).

Per directive §10 the manual batch-tick endpoint is **not ported** —
it lives on a Trigger.dev task that calls the unit-of-work functions in
``services/voice_campaign_batch.py`` (Agent B owns those).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from app.auth.flexible import FlexibleContext, require_flexible_auth
from app.db import get_db_connection
from app.models.voice_campaigns import (
    VoiceCampaignConfigCreate,
    VoiceCampaignConfigResponse,
    VoiceCampaignMetricsResponse,
)

router = APIRouter(
    prefix="/api/brands/{brand_id}/voice/campaigns",
    tags=["voice-campaigns"],
)


_CONFIG_COLS = [
    "id", "brand_id", "campaign_id",
    "voice_assistant_id", "voice_phone_number_id",
    "amd_strategy", "max_concurrent_calls",
    "call_window_start", "call_window_end", "call_window_timezone",
    "retry_policy", "created_at", "updated_at",
]


def _row_to_config(row: tuple) -> dict[str, Any]:
    return dict(zip(_CONFIG_COLS, row, strict=True))


async def _validate_campaign_in_brand(brand_id: UUID, campaign_id: UUID) -> None:
    # Post-0021 business.campaigns is channel-typed and uses archived_at.
    # Voice config rows only attach to voice_outbound campaigns.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.campaigns
                WHERE id = %s
                  AND brand_id = %s
                  AND archived_at IS NULL
                  AND channel = 'voice_outbound'
                LIMIT 1
                """,
                (str(campaign_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Campaign not found")


# ---------------------------------------------------------------------------
# Config CRUD (upsert by campaign_id)
# ---------------------------------------------------------------------------


@router.post("/{campaign_id}/config", response_model=VoiceCampaignConfigResponse)
async def upsert_voice_campaign_config(
    brand_id: UUID,
    campaign_id: UUID,
    body: VoiceCampaignConfigCreate,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    await _validate_campaign_in_brand(brand_id, campaign_id)

    payload = body.model_dump(exclude_none=True)
    keys = list(payload.keys())
    placeholders: list[str] = []
    values: list[Any] = [str(brand_id), str(campaign_id)]
    json_columns = {"retry_policy"}
    set_parts: list[str] = []
    for k in keys:
        v = payload[k]
        if k in json_columns:
            placeholders.append("%s::jsonb")
            values.append(json.dumps(v))
            set_parts.append(f"{k} = EXCLUDED.{k}")
        else:
            placeholders.append("%s")
            values.append(v)
            set_parts.append(f"{k} = EXCLUDED.{k}")
    cols_clause = ", ".join(["brand_id", "campaign_id"] + keys)
    placeholders_clause = ", ".join(["%s", "%s"] + placeholders)
    update_clause = ", ".join(set_parts + ["updated_at = NOW()"])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO voice_ai_campaign_configs ({cols_clause})
                VALUES ({placeholders_clause})
                ON CONFLICT (campaign_id) DO UPDATE SET {update_clause}
                RETURNING {', '.join(_CONFIG_COLS)}
                """,
                values,
            )
            row = await cur.fetchone()
        await conn.commit()
    return _row_to_config(row)


@router.get("/{campaign_id}/config", response_model=VoiceCampaignConfigResponse)
async def get_voice_campaign_config(
    brand_id: UUID,
    campaign_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> dict[str, Any]:
    await _validate_campaign_in_brand(brand_id, campaign_id)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(_CONFIG_COLS)}
                FROM voice_ai_campaign_configs
                WHERE campaign_id = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(campaign_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Voice campaign config not found")
    return _row_to_config(row)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@router.get("/{campaign_id}/metrics", response_model=VoiceCampaignMetricsResponse)
async def get_voice_campaign_metrics(
    brand_id: UUID,
    campaign_id: UUID,
    _auth: FlexibleContext = Depends(require_flexible_auth),
) -> VoiceCampaignMetricsResponse:
    await _validate_campaign_in_brand(brand_id, campaign_id)

    cols = [
        "campaign_id", "total_calls", "calls_connected", "calls_voicemail",
        "calls_no_answer", "calls_busy", "calls_error",
        "calls_transferred", "calls_qualified",
        "total_duration_seconds", "total_cost_cents", "updated_at",
    ]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {', '.join(cols)}
                FROM voice_campaign_metrics
                WHERE campaign_id = %s AND brand_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(campaign_id), str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return VoiceCampaignMetricsResponse(
            campaign_id=str(campaign_id),
            updated_at=datetime.now(UTC),
        )
    data = dict(zip(cols, row, strict=True))
    data["campaign_id"] = str(data["campaign_id"])
    return VoiceCampaignMetricsResponse(**data)


# ---------------------------------------------------------------------------
# NOTE: POST /{campaign_id}/batch is intentionally NOT ported.
# The batch-tick loop is owned by a Trigger.dev task that calls
# unit-of-work functions in services/voice_campaign_batch.py (Agent B).
# ---------------------------------------------------------------------------
