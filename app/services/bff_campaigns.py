"""Atomic enroll-list-into-new-campaign orchestrator for the hq-zone BFF.

A single transaction creates:
  1. business.campaigns          — umbrella outreach effort
  2. business.channel_campaigns  — per-channel execution unit
  3. business.channel_campaign_steps — first step (step_order=1)
  4. business.recipients         — upsert per lead row (org-natural-key)
  5. business.channel_campaign_step_recipients — step memberships in
     status='pending' (audience materialized before activation)

All five inserts share one psycopg connection so a failure in any later
step rolls back the earlier ones — no orphaned campaigns or steps.

The single-operator hq-zone BFF authenticates with ``verify_backend_x_token``;
this service does NOT consult Supabase auth. Org scope comes from the
request body.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.bff_campaigns import (
    BffEnrollListRecipient,
    BffEnrollListRequest,
    BffEnrollListResponse,
)
from app.models.campaigns import VALID_CHANNEL_PROVIDER_PAIRS

logger = logging.getLogger(__name__)


class BffEnrollError(Exception):
    """Base error for the BFF enroll-list orchestrator."""


class BffEnrollBrandMismatch(BffEnrollError):
    """Brand row does not exist under the supplied organization."""


class BffEnrollInvalidChannelProvider(BffEnrollError):
    """The (channel, provider) pair is not in VALID_CHANNEL_PROVIDER_PAIRS."""


def _dedupe_recipients(
    specs: list[BffEnrollListRecipient],
) -> list[BffEnrollListRecipient]:
    """Last-write-wins dedupe on (external_source, external_id)."""
    seen: dict[tuple[str, str], BffEnrollListRecipient] = {}
    for s in specs:
        seen[(s.external_source, s.external_id)] = s
    return list(seen.values())


async def enroll_list_into_new_campaign(
    payload: BffEnrollListRequest,
) -> BffEnrollListResponse:
    """Atomic enroll. Validates brand-in-org + channel/provider, then
    runs all five inserts in one transaction."""
    if (payload.channel, payload.provider) not in VALID_CHANNEL_PROVIDER_PAIRS:
        raise BffEnrollInvalidChannelProvider(
            f"({payload.channel}, {payload.provider}) is not a supported "
            "channel/provider pair"
        )

    deduped = _dedupe_recipients(payload.recipients)
    step_name = payload.step_name or f"Step 1 — {payload.channel}"

    enrollment_metadata = {
        **payload.metadata,
        "enrolled_via": "hq_zone_bff",
        **({"source_label": payload.source_label} if payload.source_label else {}),
    }

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # ── 1. Brand belongs to org? ────────────────────────────────
            await cur.execute(
                """
                SELECT 1
                FROM business.brands
                WHERE id = %s AND organization_id = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (str(payload.brand_id), str(payload.organization_id)),
            )
            if await cur.fetchone() is None:
                raise BffEnrollBrandMismatch(
                    f"brand {payload.brand_id} is not in organization "
                    f"{payload.organization_id}"
                )

            # ── 2. Campaign ─────────────────────────────────────────────
            await cur.execute(
                """
                INSERT INTO business.campaigns
                    (organization_id, brand_id, name, metadata)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(payload.organization_id),
                    str(payload.brand_id),
                    payload.campaign_name,
                    Jsonb(enrollment_metadata),
                ),
            )
            campaign_row = await cur.fetchone()
            assert campaign_row is not None
            campaign_id: UUID = campaign_row[0]

            # ── 3. Channel campaign ─────────────────────────────────────
            channel_campaign_name = (
                f"{payload.campaign_name} — {payload.channel}/{payload.provider}"
            )
            await cur.execute(
                """
                INSERT INTO business.channel_campaigns
                    (campaign_id, organization_id, brand_id, name, channel,
                     provider, audience_snapshot_count, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(campaign_id),
                    str(payload.organization_id),
                    str(payload.brand_id),
                    channel_campaign_name,
                    payload.channel,
                    payload.provider,
                    len(deduped),
                    Jsonb({"enrolled_via": "hq_zone_bff"}),
                ),
            )
            cc_row = await cur.fetchone()
            assert cc_row is not None
            channel_campaign_id: UUID = cc_row[0]

            # ── 4. First step (step_order=1) ────────────────────────────
            await cur.execute(
                """
                INSERT INTO business.channel_campaign_steps
                    (channel_campaign_id, campaign_id, organization_id,
                     brand_id, step_order, name, delay_days_from_previous,
                     content_mode, channel_specific_config, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(channel_campaign_id),
                    str(campaign_id),
                    str(payload.organization_id),
                    str(payload.brand_id),
                    1,
                    step_name,
                    0,
                    payload.content_mode,
                    Jsonb(payload.channel_specific_config),
                    Jsonb({"enrolled_via": "hq_zone_bff"}),
                ),
            )
            step_row = await cur.fetchone()
            assert step_row is not None
            step_id: UUID = step_row[0]

            # ── 5. Recipients — bulk upsert ─────────────────────────────
            #
            # One round trip per recipient is fine at v1 scale (BFF caps
            # the payload at 10 000 rows; typical lead lists are 50–500).
            # ON CONFLICT preserves the existing row's mutable fields when
            # the incoming value is null, matching services/recipients.py
            # semantics. The xmax=0 trick reports whether this was an INSERT
            # (0) or UPDATE (non-zero) so we can split new/existing in the
            # response without a second query.
            recipient_ids: list[UUID] = []
            recipients_new = 0
            recipients_existing = 0
            for spec in deduped:
                await cur.execute(
                    """
                    INSERT INTO business.recipients
                        (organization_id, recipient_type, external_source,
                         external_id, display_name, mailing_address,
                         phone, email, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (organization_id, external_source, external_id)
                    DO UPDATE SET
                        recipient_type = EXCLUDED.recipient_type,
                        display_name = COALESCE(
                            EXCLUDED.display_name,
                            business.recipients.display_name
                        ),
                        mailing_address = CASE
                            WHEN EXCLUDED.mailing_address = '{}'::jsonb
                              THEN business.recipients.mailing_address
                            ELSE EXCLUDED.mailing_address
                        END,
                        phone = COALESCE(
                            EXCLUDED.phone, business.recipients.phone
                        ),
                        email = COALESCE(
                            EXCLUDED.email, business.recipients.email
                        ),
                        metadata =
                            business.recipients.metadata || EXCLUDED.metadata,
                        updated_at = NOW()
                    RETURNING id, (xmax = 0) AS was_insert
                    """,
                    (
                        str(payload.organization_id),
                        spec.recipient_type,
                        spec.external_source,
                        spec.external_id,
                        spec.display_name,
                        Jsonb(spec.mailing_address),
                        spec.phone,
                        spec.email,
                        Jsonb(spec.metadata),
                    ),
                )
                rrow = await cur.fetchone()
                assert rrow is not None
                recipient_ids.append(rrow[0])
                if rrow[1]:
                    recipients_new += 1
                else:
                    recipients_existing += 1

            # ── 6. Step memberships (audience materialization) ──────────
            #
            # Bulk insert with executemany. Status 'pending' is the
            # audience-materialized-not-yet-activated state per the
            # migration's lifecycle docstring.
            membership_rows = [
                (
                    str(step_id),
                    str(rid),
                    str(payload.organization_id),
                    "pending",
                    Jsonb({"enrolled_via": "hq_zone_bff"}),
                )
                for rid in recipient_ids
            ]
            await cur.executemany(
                """
                INSERT INTO business.channel_campaign_step_recipients
                    (channel_campaign_step_id, recipient_id,
                     organization_id, status, metadata)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (channel_campaign_step_id, recipient_id)
                DO NOTHING
                """,
                membership_rows,
            )

    logger.info(
        "bff_enroll_list_complete campaign_id=%s channel_campaign_id=%s "
        "step_id=%s recipients=%d new=%d existing=%d",
        campaign_id, channel_campaign_id, step_id,
        len(deduped), recipients_new, recipients_existing,
    )

    return BffEnrollListResponse(
        campaign_id=campaign_id,
        channel_campaign_id=channel_campaign_id,
        step_id=step_id,
        recipient_count=len(deduped),
        recipients_new=recipients_new,
        recipients_existing=recipients_existing,
        memberships_created=len(deduped),
    )


__all__ = [
    "BffEnrollError",
    "BffEnrollBrandMismatch",
    "BffEnrollInvalidChannelProvider",
    "enroll_list_into_new_campaign",
]
