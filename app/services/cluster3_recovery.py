"""Cluster 3 stuck-queue recovery sweep.

Finds lead_transfers in 'queued' status older than the threshold and
either re-fires dispatch or marks them failed after N attempts.

Stuck-queue scenarios:
  * hq-x crashed mid-dispatch (between insert lead_transfer and EB send)
  * Anthropic outage during composer; classifier marked positive but
    dispatch raised before sending (now in pending_review or failed)
  * Trigger.dev task lost / never executed

Recovery rule:
  * if attempts < MAX_RECOVER_ATTEMPTS (3): re-fire dispatch
  * else: mark failed with failure_reason='exceeded_recover_attempts'

Pre-flight: only attempt recovery on rows where the upstream
classification is still 'positive' AND intro_fired_at IS NULL — otherwise
there's nothing to recover (something else marked it).
"""

from __future__ import annotations

import logging
from typing import Any

from app.db import get_db_connection
from app.services import alerts, cluster3_dispatch

logger = logging.getLogger(__name__)


MAX_RECOVER_ATTEMPTS = 3
STUCK_THRESHOLD_SECONDS = 600  # 10 min


async def sweep_stuck_queued(
    *,
    threshold_seconds: int = STUCK_THRESHOLD_SECONDS,
    max_attempts: int = MAX_RECOVER_ATTEMPTS,
    limit: int = 50,
) -> dict[str, Any]:
    """Run one recovery pass. Returns a summary dict."""
    candidates = await _list_stuck(
        threshold_seconds=threshold_seconds, limit=limit
    )
    retried = 0
    abandoned = 0
    succeeded = 0
    errors: list[dict[str, Any]] = []

    for c in candidates:
        if c["recover_attempt_count"] >= max_attempts:
            await _mark_abandoned(c["id"])
            abandoned += 1
            await alerts.fire_alert(
                severity="critical",
                source="cluster3_recovery",
                summary=f"Lead transfer {c['id']} abandoned after {max_attempts} attempts",
                payload={
                    "lead_transfer_id": str(c["id"]),
                    "classification_id": str(c["email_reply_classification_id"]),
                    "queued_at": c["queued_at"].isoformat() if c["queued_at"] else None,
                },
            )
            continue

        await _bump_attempt(c["id"])
        try:
            result = await cluster3_dispatch.dispatch_for_classification(
                classification_id=c["email_reply_classification_id"],
            )
            retried += 1
            if result.get("status") == "sent":
                succeeded += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"lead_transfer_id": str(c["id"]), "error": str(exc)[:300]})
            logger.exception("recovery dispatch crashed for lt=%s", c["id"])

    if abandoned > 0 or errors:
        await alerts.fire_alert(
            severity="warning",
            source="cluster3_recovery",
            summary=(
                f"recovery sweep: retried={retried} succeeded={succeeded} "
                f"abandoned={abandoned} errors={len(errors)}"
            ),
            payload={
                "abandoned": abandoned,
                "errors": errors,
                "retried": retried,
                "succeeded": succeeded,
            },
        )

    return {
        "candidates": len(candidates),
        "retried": retried,
        "succeeded": succeeded,
        "abandoned": abandoned,
        "errors": errors,
    }


async def _list_stuck(
    *, threshold_seconds: int, limit: int
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT lt.id, lt.email_reply_classification_id,
                       lt.recover_attempt_count, lt.queued_at
                FROM business.lead_transfers lt
                JOIN business.email_reply_classifications erc
                  ON erc.id = lt.email_reply_classification_id
                WHERE lt.status = 'queued'
                  AND lt.queued_at < NOW() - (%s || ' seconds')::interval
                  AND erc.classification = 'positive'
                  AND erc.intro_fired_at IS NULL
                ORDER BY lt.queued_at ASC
                LIMIT %s
                """,
                (threshold_seconds, limit),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "email_reply_classification_id": r[1],
            "recover_attempt_count": r[2],
            "queued_at": r[3],
        }
        for r in rows or []
    ]


async def _bump_attempt(lead_transfer_id: Any) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET recover_attempt_count = recover_attempt_count + 1,
                    last_recover_attempt_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(lead_transfer_id),),
            )
        await conn.commit()


async def _mark_abandoned(lead_transfer_id: Any) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.lead_transfers
                SET status = 'failed', failed_at = NOW(),
                    failure_reason = 'exceeded_recover_attempts',
                    updated_at = NOW()
                WHERE id = %s AND status = 'queued'
                """,
                (str(lead_transfer_id),),
            )
        await conn.commit()


__all__ = ["sweep_stuck_queued"]
