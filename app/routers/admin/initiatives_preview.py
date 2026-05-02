"""Read-only preview surface for GTM initiatives. Distinct from the legacy
/api/v1/admin/initiatives router (which is pipeline-runs-oriented and
modeled around a per-partner-per-brand scheme that's being phased out).

This router returns the *shape* of an initiative — campaigns, channel
campaigns, ordered steps, timing, and the JSONB content blobs (postcard
copy, letter copy, email bodies, landing-page config) that downstream
renderers use to draw the actual mailer/email/landing in the frontend.

Mounted at /api/v1/admin/initiatives-preview.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/initiatives-preview", tags=["admin"])


@router.get("")
async def list_preview(
    limit: int = 50,
    offset: int = 0,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Cross-org index of initiatives for the preview UI."""
    args: list[Any] = [min(max(limit, 1), 200), max(offset, 0)]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    i.id,
                    i.organization_id,
                    i.brand_id,
                    i.partner_id,
                    i.partner_contract_id,
                    i.data_engine_audience_id,
                    i.status,
                    i.metadata,
                    i.created_at,
                    i.updated_at,
                    (SELECT name FROM business.brands b WHERE b.id = i.brand_id) AS brand_name,
                    (SELECT display_name FROM business.brands b WHERE b.id = i.brand_id) AS brand_display_name,
                    (SELECT name FROM business.demand_side_partners p WHERE p.id = i.partner_id) AS partner_name,
                    (SELECT COUNT(*) FROM business.campaigns c WHERE c.initiative_id = i.id) AS campaigns_count,
                    (SELECT COUNT(*) FROM business.channel_campaigns cc WHERE cc.initiative_id = i.id) AS channel_campaigns_count,
                    (SELECT COUNT(*)
                       FROM business.channel_campaign_steps s
                       JOIN business.channel_campaigns cc ON cc.id = s.channel_campaign_id
                      WHERE cc.initiative_id = i.id) AS steps_count,
                    (SELECT COUNT(*)
                       FROM business.initiative_recipient_memberships m
                      WHERE m.initiative_id = i.id AND m.removed_at IS NULL) AS members_count
                FROM business.gtm_initiatives i
                ORDER BY i.created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    items = [
        {
            "id": r[0],
            "organization_id": r[1],
            "brand_id": r[2],
            "partner_id": r[3],
            "partner_contract_id": r[4],
            "data_engine_audience_id": r[5],
            "status": r[6],
            "metadata": r[7] or {},
            "created_at": r[8],
            "updated_at": r[9],
            "brand_name": r[10],
            "brand_display_name": r[11],
            "partner_name": r[12],
            "campaigns_count": r[13],
            "channel_campaigns_count": r[14],
            "steps_count": r[15],
            "members_count": r[16],
        }
        for r in rows
    ]
    return {"items": items}


@router.get("/{initiative_id}")
async def get_preview(
    initiative_id: UUID,
    user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Full graph for a single initiative — brand/partner/contract on top,
    campaigns → channel_campaigns → steps with their JSONB content blobs
    underneath, plus the active recipient set for personalization context."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Header
            await cur.execute(
                """
                SELECT
                    i.id, i.organization_id, i.brand_id, i.partner_id, i.partner_contract_id,
                    i.data_engine_audience_id, i.status, i.metadata,
                    i.reservation_window_start, i.reservation_window_end,
                    i.created_at, i.updated_at,
                    b.name AS brand_name, b.display_name AS brand_display_name,
                    b.domain AS brand_domain, b.theme_config AS brand_theme_config,
                    p.name AS partner_name, p.domain AS partner_domain,
                    pc.pricing_model, pc.amount_cents, pc.duration_days,
                    pc.qualification_rules, pc.starts_at AS contract_starts_at, pc.ends_at AS contract_ends_at,
                    pc.status AS contract_status,
                    r.audience_name AS audience_name
                FROM business.gtm_initiatives i
                JOIN business.brands b ON b.id = i.brand_id
                JOIN business.demand_side_partners p ON p.id = i.partner_id
                JOIN business.partner_contracts pc ON pc.id = i.partner_contract_id
                LEFT JOIN business.org_audience_reservations r
                       ON r.organization_id = i.organization_id
                      AND r.data_engine_audience_id = i.data_engine_audience_id
                WHERE i.id = %s
                """,
                [str(initiative_id)],
            )
            header = await cur.fetchone()
            if not header:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="initiative_not_found")

            # Campaigns
            await cur.execute(
                """
                SELECT id, name, description, status, created_at
                FROM business.campaigns
                WHERE initiative_id = %s
                ORDER BY created_at
                """,
                [str(initiative_id)],
            )
            campaigns = [
                {
                    "id": r[0],
                    "name": r[1],
                    "description": r[2],
                    "status": r[3],
                    "created_at": r[4],
                }
                for r in await cur.fetchall()
            ]

            # Channel campaigns + steps
            await cur.execute(
                """
                SELECT
                    cc.id, cc.campaign_id, cc.name, cc.channel, cc.provider,
                    cc.status, cc.dub_folder_id, cc.created_at,
                    s.id AS step_id, s.step_order, s.name AS step_name,
                    s.delay_days_from_previous, s.channel_specific_config,
                    s.landing_page_config, s.status AS step_status,
                    s.metadata AS step_metadata
                FROM business.channel_campaigns cc
                LEFT JOIN business.channel_campaign_steps s ON s.channel_campaign_id = cc.id
                WHERE cc.initiative_id = %s
                ORDER BY cc.created_at, s.step_order
                """,
                [str(initiative_id)],
            )
            chcamp_map: dict[str, dict[str, Any]] = {}
            for r in await cur.fetchall():
                cc_id = r[0]
                if cc_id not in chcamp_map:
                    chcamp_map[cc_id] = {
                        "id": cc_id,
                        "campaign_id": r[1],
                        "name": r[2],
                        "channel": r[3],
                        "provider": r[4],
                        "status": r[5],
                        "dub_folder_id": r[6],
                        "created_at": r[7],
                        "steps": [],
                    }
                if r[8] is not None:
                    chcamp_map[cc_id]["steps"].append(
                        {
                            "id": r[8],
                            "step_order": r[9],
                            "name": r[10],
                            "delay_days_from_previous": r[11],
                            "channel_specific_config": r[12] or {},
                            "landing_page_config": r[13] or {},
                            "status": r[14],
                            "metadata": r[15] or {},
                        }
                    )
            channel_campaigns = list(chcamp_map.values())

            # Members + their facts_snapshot for personalization preview
            await cur.execute(
                """
                SELECT
                    rcp.id, rcp.display_name, rcp.external_source, rcp.external_id,
                    rcp.mailing_address, rcp.phone, rcp.email, rcp.metadata
                FROM business.initiative_recipient_memberships m
                JOIN business.recipients rcp ON rcp.id = m.recipient_id
                WHERE m.initiative_id = %s AND m.removed_at IS NULL
                ORDER BY m.added_at
                LIMIT 50
                """,
                [str(initiative_id)],
            )
            recipients = []
            for r in await cur.fetchall():
                meta = r[7] or {}
                recipients.append(
                    {
                        "id": r[0],
                        "display_name": r[1],
                        "external_source": r[2],
                        "external_id": r[3],
                        "mailing_address": r[4] or {},
                        "phone": r[5],
                        "email": r[6],
                        "facts_snapshot": meta.get("facts_snapshot") or {},
                        "recipient_code": meta.get("recipient_code"),
                    }
                )

    return {
        "initiative": {
            "id": header[0],
            "organization_id": header[1],
            "brand_id": header[2],
            "partner_id": header[3],
            "partner_contract_id": header[4],
            "data_engine_audience_id": header[5],
            "status": header[6],
            "metadata": header[7] or {},
            "reservation_window_start": header[8],
            "reservation_window_end": header[9],
            "created_at": header[10],
            "updated_at": header[11],
        },
        "brand": {
            "id": header[2],
            "name": header[12],
            "display_name": header[13],
            "domain": header[14],
            "theme_config": header[15] or {},
        },
        "partner": {
            "id": header[3],
            "name": header[16],
            "domain": header[17],
        },
        "contract": {
            "id": header[4],
            "pricing_model": header[18],
            "amount_cents": header[19],
            "duration_days": header[20],
            "qualification_rules": header[21] or {},
            "starts_at": header[22],
            "ends_at": header[23],
            "status": header[24],
        },
        "audience": {
            "id": header[5],
            "name": header[25],
        },
        "campaigns": campaigns,
        "channel_campaigns": channel_campaigns,
        "recipients": recipients,
    }
