"""Admin Cluster 3 endpoints — operator-facing health dashboard surface.

Auth: platform-operator JWT.

Endpoints:

    GET   /api/v1/admin/cluster3/health
        Single-call dashboard payload — green/yellow/red across all sections.

    GET   /api/v1/admin/cluster3/pending-review
        List lead_transfers in pending_review status with full detail.

    POST  /api/v1/admin/cluster3/pending-review/{lead_transfer_id}/approve
        Operator approves a held intro; dispatches it.

    POST  /api/v1/admin/cluster3/pending-review/{lead_transfer_id}/reject
        Operator rejects; marks failed with reason.

    POST  /api/v1/admin/cluster3/heartbeat/run
        Manual heartbeat trigger.

    POST  /api/v1/admin/cluster3/recovery/run
        Manual recovery sweep.

    POST  /api/v1/admin/cluster3/reconciliation/run
        Manual reconciliation sweep.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from psycopg.types.json import Jsonb

from app.auth.roles import require_platform_operator
from app.auth.supabase_jwt import UserContext
from app.db import get_db_connection
from app.services import (
    alerts,
    cluster3_dispatch,
    cluster3_health,
    cluster3_heartbeat,
    cluster3_recovery,
    cluster3_reconciliation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/admin/cluster3", tags=["admin", "cluster3"])


@router.get("/health")
async def health(
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    return await cluster3_health.overall_snapshot()


@router.get("/pending-review")
async def pending_review(
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT lt.id, lt.created_at, lt.queued_at,
                       lt.partner_id, p.name AS partner_name,
                       lt.recipient_id, r.display_name, r.email,
                       lt.intro_email_message_id,
                       em.subject_snapshot, em.body_snapshot, em.metadata,
                       lt.metadata, lt.allocation_snapshot
                FROM business.lead_transfers lt
                LEFT JOIN business.demand_side_partners p ON p.id = lt.partner_id
                LEFT JOIN business.recipients r ON r.id = lt.recipient_id
                LEFT JOIN business.email_messages em ON em.id = lt.intro_email_message_id
                WHERE lt.status = 'pending_review'
                ORDER BY lt.created_at DESC
                LIMIT 100
                """
            )
            rows = await cur.fetchall()
    items = []
    for r in rows or []:
        items.append(
            {
                "lead_transfer_id": str(r[0]),
                "queued_at": r[2].isoformat() if r[2] else None,
                "partner_id": str(r[3]) if r[3] else None,
                "partner_name": r[4],
                "recipient_id": str(r[5]) if r[5] else None,
                "recipient_display_name": r[6],
                "recipient_email": r[7],
                "intro_email_message_id": str(r[8]) if r[8] else None,
                "intro_subject": r[9],
                "intro_body": r[10],
                "email_metadata": r[11] or {},
                "lead_transfer_metadata": r[12] or {},
                "allocation_snapshot": r[13] or {},
            }
        )
    return {"items": items, "count": len(items)}


@router.post("/pending-review/{lead_transfer_id}/approve")
async def approve_pending_review(
    lead_transfer_id: UUID,
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Approve a held intro: re-runs dispatch with composer_mode='manual_approve'.

    The current implementation re-dispatches the underlying classification.
    The new dispatch will compose, run verdict, and unless the operator's
    stuck on the same prompt issue, ship it through.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT email_reply_classification_id, status
                FROM business.lead_transfers
                WHERE id = %s
                """,
                (str(lead_transfer_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="lead_transfer not found")
    classification_id, status = row
    if status != "pending_review":
        raise HTTPException(
            status_code=400, detail=f"lead_transfer status is {status!r}, not pending_review"
        )

    # Mark current row as cancelled so the unique-index lets a new
    # dispatch attempt through.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET status = 'cancelled',
                    failure_reason = 'replaced_by_operator_approve',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(lead_transfer_id),),
            )
        await conn.commit()

    result = await cluster3_dispatch.dispatch_for_classification(
        classification_id=classification_id,
        composer_mode="auto",
    )
    await alerts.fire_alert(
        severity="info",
        source="cluster3_admin",
        summary=f"Operator approved + redispatched lead_transfer {lead_transfer_id}",
        payload={"new_result": result},
    )
    return {"replaced_lead_transfer_id": str(lead_transfer_id), "new_dispatch": result}


@router.post("/pending-review/{lead_transfer_id}/reject")
async def reject_pending_review(
    lead_transfer_id: UUID,
    body: dict[str, Any] | None = None,
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    reason = (body or {}).get("reason") or "operator_rejected"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET status = 'cancelled',
                    failure_reason = %s,
                    updated_at = NOW()
                WHERE id = %s AND status = 'pending_review'
                RETURNING email_reply_classification_id
                """,
                (reason, str(lead_transfer_id)),
            )
            row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="lead_transfer not pending_review")
    return {"lead_transfer_id": str(lead_transfer_id), "status": "cancelled", "reason": reason}


@router.post("/heartbeat/run")
async def heartbeat_run(
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    return {"heartbeat": await cluster3_heartbeat.run_heartbeat()}


@router.post("/recovery/run")
async def recovery_run(
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    return await cluster3_recovery.sweep_stuck_queued()


@router.post("/reconciliation/run")
async def reconciliation_run(
    body: dict[str, Any] | None = None,
    _user: UserContext = Depends(require_platform_operator),
) -> dict[str, Any]:
    body = body or {}
    return await cluster3_reconciliation.sweep_reconciliation(
        lookback_hours=int(body.get("lookback_hours", 48)),
        per_campaign_limit=int(body.get("per_campaign_limit", 200)),
    )


__all__ = ["router"]
