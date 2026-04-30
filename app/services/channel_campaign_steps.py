"""CRUD service for business.channel_campaign_steps.

A step is one ordered touch within a channel_campaign. For
``channel='direct_mail'`` each step maps 1:1 to a Lob campaign object.
For single-touch sends the channel_campaign has exactly one step.

Validation rules enforced here:
  * step_order is unique within a channel_campaign (DB also enforces).
  * For ``channel='direct_mail'``, ``creative_ref`` must reference a
    ``dmaas_designs`` row whose ``brand_id`` matches the parent
    channel_campaign's brand.
  * Edits to a step are only allowed while ``status='pending'`` —
    activation freezes the step.

Activation dispatches to the per-channel adapter; only direct_mail is
wired today, other channels raise ``StepActivationNotImplemented``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.models.campaigns import (
    ChannelCampaignStepCreate,
    ChannelCampaignStepResponse,
    ChannelCampaignStepStatus,
    ChannelCampaignStepUpdate,
)
from app.services.channel_campaigns import (
    get_channel_campaign,
)


class StepError(Exception):
    pass


class StepNotFound(StepError):
    pass


class StepImmutable(StepError):
    """Raised when a non-pending step is edited."""


class StepCreativeRefRequired(StepError):
    pass


class StepCreativeRefBrandMismatch(StepError):
    pass


class StepInvalidStatusTransition(StepError):
    pass


class StepActivationNotImplemented(StepError):
    pass


_COLUMNS = (
    "id, channel_campaign_id, campaign_id, organization_id, brand_id, "
    "step_order, name, delay_days_from_previous, scheduled_send_at, "
    "creative_ref, channel_specific_config, external_provider_id, "
    "external_provider_metadata, status, activated_at, metadata, "
    "created_at, updated_at"
)


def _row_to_response(row: tuple[Any, ...]) -> ChannelCampaignStepResponse:
    return ChannelCampaignStepResponse(
        id=row[0],
        channel_campaign_id=row[1],
        campaign_id=row[2],
        organization_id=row[3],
        brand_id=row[4],
        step_order=row[5],
        name=row[6],
        delay_days_from_previous=row[7],
        scheduled_send_at=row[8],
        creative_ref=row[9],
        channel_specific_config=row[10] or {},
        external_provider_id=row[11],
        external_provider_metadata=row[12] or {},
        status=row[13],
        activated_at=row[14],
        metadata=row[15] or {},
        created_at=row[16],
        updated_at=row[17],
    )


async def _validate_creative_ref_for_brand(
    *, creative_ref: UUID, brand_id: UUID
) -> None:
    """Confirm a direct_mail step's creative_ref points at a brand-scoped
    dmaas_designs row. Other channels skip this check (see caller)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT brand_id FROM dmaas_designs WHERE id = %s",
                (str(creative_ref),),
            )
            row = await cur.fetchone()
    if row is None:
        raise StepCreativeRefBrandMismatch(
            f"creative_ref {creative_ref} not found in dmaas_designs"
        )
    if row[0] != brand_id:
        raise StepCreativeRefBrandMismatch(
            f"creative_ref {creative_ref} belongs to brand {row[0]}, not {brand_id}"
        )


async def create_step(
    *,
    channel_campaign_id: UUID,
    organization_id: UUID,
    payload: ChannelCampaignStepCreate,
) -> ChannelCampaignStepResponse:
    cc = await get_channel_campaign(
        channel_campaign_id=channel_campaign_id, organization_id=organization_id
    )

    if cc.channel == "direct_mail":
        if payload.creative_ref is None:
            raise StepCreativeRefRequired(
                "creative_ref (design_id) is required for direct_mail steps"
            )
        await _validate_creative_ref_for_brand(
            creative_ref=payload.creative_ref, brand_id=cc.brand_id
        )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                INSERT INTO business.channel_campaign_steps
                    (channel_campaign_id, campaign_id, organization_id, brand_id,
                     step_order, name, delay_days_from_previous,
                     creative_ref, channel_specific_config, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_COLUMNS}
                """,
                (
                    str(channel_campaign_id),
                    str(cc.campaign_id),
                    str(cc.organization_id),
                    str(cc.brand_id),
                    payload.step_order,
                    payload.name,
                    payload.delay_days_from_previous,
                    str(payload.creative_ref) if payload.creative_ref else None,
                    Jsonb(payload.channel_specific_config),
                    Jsonb(payload.metadata),
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_response(row)


async def get_step_landing_page_config(
    *, step_id: UUID, organization_id: UUID
) -> dict[str, Any] | None:
    """Read the step's landing_page_config JSONB. Returns None if unset.

    Joined to business.channel_campaign_steps.organization_id for org
    isolation; cross-org access returns None (caller maps to 404).
    """
    async with get_db_connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            SELECT landing_page_config
            FROM business.channel_campaign_steps
            WHERE id = %s AND organization_id = %s
            """,
            (str(step_id), str(organization_id)),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    return row[0]


async def set_step_landing_page_config(
    *,
    step_id: UUID,
    organization_id: UUID,
    config: dict[str, Any] | None,
) -> bool:
    """Replace landing_page_config (or clear it when config=None).

    Returns True if a step row was updated. The caller validates the
    config content against `StepLandingPageConfig` before calling.
    Allowed regardless of step status — a draft can be retuned, an
    activated step's page content can be patched mid-campaign.
    """
    import json as _json

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps
                SET landing_page_config = %s::jsonb, updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                """,
                (
                    None if config is None else _json.dumps(config),
                    str(step_id),
                    str(organization_id),
                ),
            )
            updated = cur.rowcount
        await conn.commit()
    return bool(updated)


async def get_step(
    *, step_id: UUID, organization_id: UUID
) -> ChannelCampaignStepResponse:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.channel_campaign_steps
                WHERE id = %s AND organization_id = %s
                """,
                (str(step_id), str(organization_id)),
            )
            row = await cur.fetchone()
    if row is None:
        raise StepNotFound(f"step {step_id} not found in org {organization_id}")
    return _row_to_response(row)


async def list_steps(
    *,
    channel_campaign_id: UUID,
    organization_id: UUID,
    status: ChannelCampaignStepStatus | None = None,
) -> list[ChannelCampaignStepResponse]:
    where = ["channel_campaign_id = %s", "organization_id = %s"]
    args: list[Any] = [str(channel_campaign_id), str(organization_id)]
    if status is not None:
        where.append("status = %s")
        args.append(status)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_COLUMNS}
                FROM business.channel_campaign_steps
                WHERE {' AND '.join(where)}
                ORDER BY step_order
                """,
                args,
            )
            rows = await cur.fetchall()
    return [_row_to_response(r) for r in rows]


async def update_step(
    *,
    step_id: UUID,
    organization_id: UUID,
    payload: ChannelCampaignStepUpdate,
) -> ChannelCampaignStepResponse:
    existing = await get_step(step_id=step_id, organization_id=organization_id)
    if existing.status != "pending":
        raise StepImmutable(
            f"cannot edit step in status={existing.status} (only 'pending' is editable)"
        )

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        return existing

    if "creative_ref" in fields and fields["creative_ref"] is not None:
        cc = await get_channel_campaign(
            channel_campaign_id=existing.channel_campaign_id,
            organization_id=organization_id,
        )
        if cc.channel == "direct_mail":
            await _validate_creative_ref_for_brand(
                creative_ref=fields["creative_ref"], brand_id=cc.brand_id
            )

    set_parts: list[str] = []
    args: list[Any] = []
    json_keys = {"channel_specific_config", "metadata"}
    for key, value in fields.items():
        if key in json_keys:
            set_parts.append(f"{key} = %s")
            args.append(Jsonb(value or {}))
        elif key == "creative_ref":
            set_parts.append(f"{key} = %s")
            args.append(str(value) if value is not None else None)
        else:
            set_parts.append(f"{key} = %s")
            args.append(value)
    set_parts.append("updated_at = NOW()")
    args.extend([str(step_id), str(organization_id)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaign_steps
                SET {', '.join(set_parts)}
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                args,
            )
            row = await cur.fetchone()
    if row is None:
        raise StepNotFound(f"step {step_id} not found in org {organization_id}")
    return _row_to_response(row)


def compute_step_scheduled_send_at(
    *,
    campaign_start_date: Any,
    previous_step_send_at: datetime | None,
    delay_days_from_previous: int,
    step_order: int,
) -> datetime | None:
    """Compute scheduled_send_at for a step.

    Rules:
      * step_order=1, no previous step → campaign_start_date + 0 days, or
        None if campaign has no start_date.
      * step_order>1 → previous_step_send_at + delay_days_from_previous.
        If the previous step has no scheduled_send_at, propagate None.
    """
    if step_order == 1:
        if campaign_start_date is None:
            return None
        from datetime import time

        base = datetime.combine(campaign_start_date, time(0, 0), tzinfo=UTC)
        return base + timedelta(days=delay_days_from_previous)
    if previous_step_send_at is None:
        return None
    return previous_step_send_at + timedelta(days=delay_days_from_previous)


async def activate_step(
    *,
    step_id: UUID,
    organization_id: UUID,
) -> ChannelCampaignStepResponse:
    """Transition pending → activating, then scheduled (or sent on
    immediate-send). Dispatches to the per-channel adapter.

    For direct_mail this calls into ``app.providers.lob.adapter.LobAdapter``
    to create the Lob campaign + creative + audience and persist the
    external_provider_id back on the step. Other channels raise
    StepActivationNotImplemented.
    """
    step = await get_step(step_id=step_id, organization_id=organization_id)
    # ``activating`` is allowed so a partial-failure retry resumes
    # mid-flow rather than re-creating Lob objects. The Lob adapter is
    # idempotent on step id and uses ``external_provider_metadata`` to
    # know which sub-steps (campaign / creative / upload) already
    # succeeded and skip them on the retry.
    if step.status not in ("pending", "activating"):
        raise StepInvalidStatusTransition(
            f"cannot activate step in status={step.status}"
        )

    cc = await get_channel_campaign(
        channel_campaign_id=step.channel_campaign_id,
        organization_id=organization_id,
    )

    if cc.channel == "direct_mail":
        # Local import keeps this module loadable even when the Lob adapter
        # is not configured (e.g. during pure-logic tests).
        from app.providers.lob.adapter import LobAdapter

        result = await LobAdapter().activate_step(step=step, channel_campaign=cc)
    elif cc.channel == "email" and cc.provider == "emailbison":
        from app.providers.emailbison.adapter import EmailBisonAdapter

        result = await EmailBisonAdapter().activate_step(
            step=step, channel_campaign=cc
        )
    else:
        raise StepActivationNotImplemented(
            f"activation for channel={cc.channel} provider={cc.provider} "
            "is not wired"
        )

    # On successful activation, flip every pending membership for this step
    # to 'scheduled'. The Lob send / webhook layer is responsible for moving
    # them onward to 'sent' / 'failed' as pieces fire.
    if result.status not in ("failed",):
        from app.services.recipients import bulk_update_pending_to_scheduled

        await bulk_update_pending_to_scheduled(channel_campaign_step_id=step_id)

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaign_steps
                SET status = %s,
                    external_provider_id = %s,
                    external_provider_metadata = %s,
                    activated_at = COALESCE(activated_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                (
                    result.status,
                    result.external_provider_id,
                    Jsonb(result.metadata),
                    str(step_id),
                    str(organization_id),
                ),
            )
            row = await cur.fetchone()
    assert row is not None
    return _row_to_response(row)


async def cancel_step(
    *,
    step_id: UUID,
    organization_id: UUID,
) -> ChannelCampaignStepResponse:
    step = await get_step(step_id=step_id, organization_id=organization_id)
    if step.status not in ("pending", "scheduled", "activating"):
        raise StepInvalidStatusTransition(
            f"cannot cancel step in status={step.status}"
        )

    # If the step has an external provider id, ask the adapter to cancel it
    # at the provider. We swallow provider errors here on purpose: the user's
    # local state cancellation should not be blocked by Lob being slow.
    if step.external_provider_id:
        cc = await get_channel_campaign(
            channel_campaign_id=step.channel_campaign_id,
            organization_id=organization_id,
        )
        if cc.channel == "direct_mail":
            from app.providers.lob.adapter import LobAdapter

            try:
                await LobAdapter().cancel_step(step=step)
            except Exception:  # pragma: no cover — best-effort provider-side
                pass
        elif cc.channel == "email" and cc.provider == "emailbison":
            from app.providers.emailbison.adapter import EmailBisonAdapter

            try:
                await EmailBisonAdapter().cancel_step(step=step)
            except Exception:  # pragma: no cover — best-effort provider-side
                pass

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.channel_campaign_steps
                SET status = 'cancelled', updated_at = NOW()
                WHERE id = %s AND organization_id = %s
                RETURNING {_COLUMNS}
                """,
                (str(step_id), str(organization_id)),
            )
            row = await cur.fetchone()
    assert row is not None

    # Cancel any non-terminal memberships in lockstep with the step.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_step_recipients
                SET status = 'cancelled', updated_at = NOW()
                WHERE channel_campaign_step_id = %s
                  AND status IN ('pending', 'scheduled')
                """,
                (str(step_id),),
            )

    return _row_to_response(row)


async def get_step_context(
    *, step_id: UUID
) -> dict[str, Any] | None:
    """Resolve org/brand/campaign/channel_campaign/channel/provider for
    analytics tagging.

    Returns None when the step is not found. Channel + provider come from
    the parent channel_campaign — they're not duplicated on the step row.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT s.organization_id, s.brand_id, s.campaign_id,
                       s.channel_campaign_id,
                       cc.channel, cc.provider
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                  ON cc.id = s.channel_campaign_id
                WHERE s.id = %s
                """,
                (str(step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "organization_id": str(row[0]),
        "brand_id": str(row[1]),
        "campaign_id": str(row[2]),
        "channel_campaign_id": str(row[3]),
        "channel_campaign_step_id": str(step_id),
        "channel": row[4],
        "provider": row[5],
    }


async def lookup_step_by_external_provider_id(
    *, external_provider_id: str
) -> dict[str, Any] | None:
    """Webhook-routing helper: return identifying ids for a step by its
    Lob (or other provider) campaign id.

    The projector uses this when a webhook references a Lob campaign but
    no per-piece direct_mail_pieces row exists for it (campaign-level
    events).
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, channel_campaign_id, campaign_id,
                       organization_id, brand_id, status
                FROM business.channel_campaign_steps
                WHERE external_provider_id = %s
                LIMIT 1
                """,
                (external_provider_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "step_id": row[0],
        "channel_campaign_id": row[1],
        "campaign_id": row[2],
        "organization_id": row[3],
        "brand_id": row[4],
        "status": row[5],
    }


async def get_step_simple(*, step_id: UUID) -> str | None:
    """Lightweight status fetch (no org check). Used by the multi-step
    scheduler hook in app.services.step_scheduler — the caller is
    webhook-authenticated and trusts the resolved step id.

    Returns None when the step is not found.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM business.channel_campaign_steps WHERE id = %s",
                (str(step_id),),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def update_step_status(
    *,
    step_id: UUID,
    new_status: ChannelCampaignStepStatus,
) -> None:
    """Webhook-driven status update. No org check here — webhooks have
    already been authenticated as Lob-originated; the resolved step id is
    trusted."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps
                SET status = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (new_status, str(step_id)),
            )


class StepAudienceImmutable(StepError):
    """Audience cannot be modified once the step has left the pending state."""


async def materialize_step_audience(
    *,
    step_id: UUID,
    organization_id: UUID,
    recipients: list[RecipientSpec],
    replace_existing: bool = True,
) -> list[StepRecipientResponse]:
    """Materialize a step's audience: upsert recipients into
    ``business.recipients``, then create ``pending`` membership rows in
    ``channel_campaign_step_recipients``.

    Two-phase lifecycle: this is *configuration* time. The step must
    still be in ``pending`` status — once activated, the audience is
    frozen. Memberships from a prior call are deleted and re-inserted
    when ``replace_existing`` is True (default), so an operator
    iterating on the audience spec sees a clean view each time.

    Returns the resulting membership rows in insertion order.
    """
    from app.models.recipients import RecipientSpec, StepRecipientResponse  # noqa: F401
    from app.services.recipients import (
        bulk_upsert_recipients,
        list_step_memberships,
    )

    step = await get_step(step_id=step_id, organization_id=organization_id)
    if step.status != "pending":
        raise StepAudienceImmutable(
            f"cannot modify audience for step in status={step.status} "
            "(must be 'pending')"
        )

    upserted = await bulk_upsert_recipients(
        organization_id=organization_id, specs=recipients
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            if replace_existing:
                # Pending rows are safe to delete — nothing has happened yet.
                await cur.execute(
                    """
                    DELETE FROM business.channel_campaign_step_recipients
                    WHERE channel_campaign_step_id = %s AND status = 'pending'
                    """,
                    (str(step_id),),
                )
            for r in upserted:
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaign_step_recipients
                        (channel_campaign_step_id, recipient_id,
                         organization_id, status)
                    VALUES (%s, %s, %s, 'pending')
                    ON CONFLICT (channel_campaign_step_id, recipient_id)
                    DO NOTHING
                    """,
                    (str(step_id), str(r.id), str(organization_id)),
                )

    return await list_step_memberships(channel_campaign_step_id=step_id)


__all__ = [
    "StepError",
    "StepNotFound",
    "StepImmutable",
    "StepCreativeRefRequired",
    "StepCreativeRefBrandMismatch",
    "StepInvalidStatusTransition",
    "StepActivationNotImplemented",
    "StepAudienceImmutable",
    "create_step",
    "get_step",
    "list_steps",
    "update_step",
    "activate_step",
    "cancel_step",
    "compute_step_scheduled_send_at",
    "get_step_context",
    "lookup_step_by_external_provider_id",
    "update_step_status",
    "materialize_step_audience",
]
