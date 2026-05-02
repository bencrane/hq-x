"""Plan-out → hq-x-execute layer for the materializer subagents.

Two top-level functions:

  * ``execute_channel_step_plan`` — converts the channel-step
    materializer's JSON plan into rows in
    ``business.campaigns`` + ``business.channel_campaigns`` +
    ``business.channel_campaign_steps``. Single-transaction.
  * ``execute_audience_plan`` — pages through DEX members, upserts
    ``business.recipients``, inserts
    ``business.channel_campaign_step_recipients`` (per DM step) and
    ``business.initiative_recipient_memberships`` (per recipient),
    then bulk-mints Dub links per (DM step × recipient). Batched —
    one transaction per page rather than one for the whole audience.

Both are idempotent on re-run: the unique-key + ON CONFLICT semantics
of the underlying tables let a partially-completed materialization
resume cleanly.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.dmaas import step_link_minting
from app.services import dex_client
from app.services import gtm_initiatives as gtm_svc
from app.services import initiative_recipient_memberships as irm
from app.services.recipients import bulk_upsert_recipients
from app.models.recipients import RecipientSpec

logger = logging.getLogger(__name__)


_DEX_PAGE_SIZE = 500
_VALID_CHANNELS = {"direct_mail", "email", "voice_inbound"}
_VALID_PROVIDERS = {
    "direct_mail": {"lob"},
    "email": {"emailbison"},
    "voice_inbound": {"vapi"},
}


class MaterializerExecutionError(Exception):
    pass


# ---------------------------------------------------------------------------
# 1. channel-step plan execution
# ---------------------------------------------------------------------------


async def execute_channel_step_plan(
    initiative_id: UUID,
    plan: dict[str, Any],
) -> dict[str, Any]:
    """Insert the campaigns / channel_campaigns / channel_campaign_steps
    rows in a single transaction. Sets initiative_id on both layers.

    Returns:
        {
            campaign_id: UUID,
            channel_campaign_ids: {channel: UUID, ...},
            channel_campaign_step_ids: [UUID, ...],   # in pipeline order
            dm_step_ids: [UUID, ...],                  # the direct_mail subset
        }

    Idempotent: if a campaign for this initiative already exists with
    the materializer's name, the function returns the existing ids
    without re-inserting. Caller is the audience-materializer step or
    the test harness; both are fine with idempotent re-runs.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise MaterializerExecutionError(
            f"initiative {initiative_id} not found"
        )

    campaign_in = plan.get("campaign") or {}
    channel_campaigns_in = plan.get("channel_campaigns") or []
    steps_in = plan.get("steps") or []

    if not campaign_in or not channel_campaigns_in or not steps_in:
        raise MaterializerExecutionError(
            "plan missing campaign / channel_campaigns / steps sections"
        )

    organization_id = initiative["organization_id"]
    brand_id = initiative["brand_id"]

    # Validate channels + providers up-front so we never write a half-formed plan.
    for cc in channel_campaigns_in:
        ch = cc.get("channel")
        prov = cc.get("provider")
        if ch not in _VALID_CHANNELS:
            raise MaterializerExecutionError(
                f"invalid channel={ch!r} in plan; expected one of {_VALID_CHANNELS}"
            )
        if prov not in _VALID_PROVIDERS.get(ch, set()):
            raise MaterializerExecutionError(
                f"invalid provider={prov!r} for channel={ch!r}"
            )

    channel_to_index: dict[str, int] = {
        cc["channel"]: i for i, cc in enumerate(channel_campaigns_in)
    }
    for step in steps_in:
        if step.get("channel") not in channel_to_index:
            raise MaterializerExecutionError(
                f"step references unknown channel={step.get('channel')!r}"
            )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # 1. Idempotency check on campaigns: same initiative_id + same
            # campaign name → reuse.
            campaign_name = campaign_in.get("name") or _default_campaign_name(
                initiative
            )
            await cur.execute(
                """
                SELECT id FROM business.campaigns
                WHERE initiative_id = %s AND name = %s
                LIMIT 1
                """,
                (str(initiative_id), campaign_name),
            )
            existing = await cur.fetchone()
            if existing is not None:
                return await _load_existing_materialization(
                    cur, initiative_id, existing[0]
                )

            await cur.execute(
                """
                INSERT INTO business.campaigns
                    (organization_id, brand_id, name, description,
                     metadata, initiative_id, created_by_user_id)
                VALUES (%s, %s, %s, %s, %s, %s, NULL)
                RETURNING id
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    campaign_name,
                    campaign_in.get("description") or "",
                    Jsonb(campaign_in.get("metadata") or {}),
                    str(initiative_id),
                ),
            )
            campaign_id_row = await cur.fetchone()
            campaign_id = campaign_id_row[0]

            # 2. channel_campaigns — initiative_id denormalized onto each.
            channel_campaign_ids: dict[str, UUID] = {}
            for cc in channel_campaigns_in:
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaigns
                        (campaign_id, organization_id, brand_id, name,
                         channel, provider, schedule_config, provider_config,
                         metadata, initiative_id, audience_spec_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(campaign_id),
                        str(organization_id),
                        str(brand_id),
                        cc.get("name") or f"{cc['channel']} — {campaign_name}",
                        cc["channel"],
                        cc["provider"],
                        Jsonb(cc.get("schedule_config") or {}),
                        Jsonb(cc.get("provider_config") or {}),
                        Jsonb(cc.get("metadata") or {}),
                        str(initiative_id),
                        str(initiative["data_engine_audience_id"]),
                    ),
                )
                row = await cur.fetchone()
                channel_campaign_ids[cc["channel"]] = row[0]

            # 3. channel_campaign_steps — preserve the plan's order, derive
            # 1-based step_order per channel_campaign.
            steps_in_pipeline_order: list[UUID] = []
            dm_step_ids: list[UUID] = []
            per_channel_order: dict[str, int] = {}
            for step in steps_in:
                channel = step["channel"]
                cc_id = channel_campaign_ids[channel]
                per_channel_order[channel] = per_channel_order.get(channel, 0) + 1
                step_order = per_channel_order[channel]
                channel_specific_config = step.get("channel_specific_config") or {}
                # Carry the doctrine-aware landing-page placeholder onto the
                # step's metadata so a downstream subagent can fill it in.
                step_metadata = step.get("metadata") or {}
                if step.get("landing_page_config_placeholder"):
                    step_metadata = {
                        **step_metadata,
                        "landing_page_config_placeholder":
                            step["landing_page_config_placeholder"],
                    }
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaign_steps
                        (channel_campaign_id, campaign_id, organization_id,
                         brand_id, step_order, name,
                         delay_days_from_previous, creative_ref,
                         channel_specific_config, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s)
                    RETURNING id
                    """,
                    (
                        str(cc_id),
                        str(campaign_id),
                        str(organization_id),
                        str(brand_id),
                        step_order,
                        step.get("name") or f"Touch {step_order}",
                        int(step.get("delay_days_from_previous") or 0),
                        Jsonb(channel_specific_config),
                        Jsonb(step_metadata),
                    ),
                )
                step_row = await cur.fetchone()
                step_id = step_row[0]
                steps_in_pipeline_order.append(step_id)
                if channel == "direct_mail":
                    dm_step_ids.append(step_id)

        await conn.commit()

    return {
        "campaign_id": campaign_id,
        "channel_campaign_ids": channel_campaign_ids,
        "channel_campaign_step_ids": steps_in_pipeline_order,
        "dm_step_ids": dm_step_ids,
    }


def _default_campaign_name(initiative: dict[str, Any]) -> str:
    return f"initiative-{str(initiative['id'])[:8]}"


async def _load_existing_materialization(
    cur, initiative_id: UUID, campaign_id: UUID,
) -> dict[str, Any]:
    """Recover the row-id triple when execute_channel_step_plan is
    re-invoked for an already-materialized campaign. Used so the
    function is idempotent.
    """
    await cur.execute(
        """
        SELECT id, channel FROM business.channel_campaigns
        WHERE campaign_id = %s
        ORDER BY created_at
        """,
        (str(campaign_id),),
    )
    cc_rows = await cur.fetchall()
    channel_campaign_ids = {row[1]: row[0] for row in cc_rows}

    await cur.execute(
        """
        SELECT s.id, cc.channel
        FROM business.channel_campaign_steps s
        JOIN business.channel_campaigns cc
            ON cc.id = s.channel_campaign_id
        WHERE s.campaign_id = %s
        ORDER BY cc.created_at, s.step_order
        """,
        (str(campaign_id),),
    )
    step_rows = await cur.fetchall()
    steps_in_pipeline_order = [r[0] for r in step_rows]
    dm_step_ids = [r[0] for r in step_rows if r[1] == "direct_mail"]

    return {
        "campaign_id": campaign_id,
        "channel_campaign_ids": channel_campaign_ids,
        "channel_campaign_step_ids": steps_in_pipeline_order,
        "dm_step_ids": dm_step_ids,
    }


# ---------------------------------------------------------------------------
# 2. audience plan execution
# ---------------------------------------------------------------------------


async def execute_audience_plan(
    initiative_id: UUID,
    plan: dict[str, Any],
    *,
    dm_step_ids: list[UUID],
    audience_limit: int | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Page through DEX members and write recipients + memberships +
    manifest + Dub links.

    Args:
        plan: the audience-materializer's actor output. Used for its
            ``decision`` flag — we abort if it's anything other than
            ``materialize_full``.
        dm_step_ids: the direct_mail step ids materialized by
            ``execute_channel_step_plan``. We page memberships per step
            and mint one Dub link per (step × recipient).
        audience_limit: optional dev-only cap (MATERIALIZER_AUDIENCE_LIMIT)
            so a developer can prove the path against 25 rows instead
            of 25,000.

    Returns:
        {
            recipient_count: int,
            membership_count: int,
            manifest_count: int,
            dub_link_count: int,
            decision: str,
        }
    """
    decision = plan.get("decision")
    if decision == "reject_size_mismatch":
        raise MaterializerExecutionError(
            f"audience plan rejected: {plan.get('size_decision_reason') or 'no reason'}"
        )
    if decision not in {"materialize_full", "materialize_capped"}:
        raise MaterializerExecutionError(
            f"unsupported decision={decision!r}"
        )

    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise MaterializerExecutionError(
            f"initiative {initiative_id} not found"
        )

    organization_id = initiative["organization_id"]
    audience_spec_id: UUID = initiative["data_engine_audience_id"]
    partner_contract_id: UUID = initiative["partner_contract_id"]

    # 1. Page audience members from DEX.
    target_count = int(plan.get("actual_audience_size") or 0)
    if audience_limit is not None and audience_limit > 0:
        target_count = min(target_count, audience_limit)

    recipient_count = 0
    membership_count = 0
    manifest_count = 0
    dub_link_count = 0

    offset = 0
    while True:
        page_size = _DEX_PAGE_SIZE
        if audience_limit is not None and audience_limit > 0:
            page_size = min(page_size, audience_limit - recipient_count)
            if page_size <= 0:
                break

        page = await dex_client.list_audience_members(
            audience_spec_id,
            limit=page_size,
            offset=offset,
            bearer_token=bearer_token,
        )
        items: list[dict[str, Any]] = list((page or {}).get("items") or [])
        if not items:
            break

        recipient_specs = [_member_to_spec(m) for m in items]
        # Filter out malformed members (those without a stable natural key).
        recipient_specs = [s for s in recipient_specs if s is not None]
        if not recipient_specs:
            offset += len(items)
            if not page.get("has_more"):
                break
            continue

        recipients = await bulk_upsert_recipients(
            organization_id=organization_id, specs=recipient_specs
        )
        recipient_count += len(recipients)

        # Per-DM-step memberships. Voice and email steps don't get
        # per-step memberships at materialization time — those channels
        # don't fan out the audience the same way (voice is inbound;
        # email gets one-to-many emails handled by EmailBison's lead
        # objects). Direct_mail is per-piece, so per-step memberships
        # are required here.
        rows_inserted = 0
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                for step_id in dm_step_ids:
                    # Multi-VALUES insert with ON CONFLICT DO NOTHING for idempotency.
                    placeholders = ", ".join(
                        ["(%s, %s, %s, %s)"] * len(recipients)
                    )
                    flat: list[Any] = []
                    for r in recipients:
                        flat.extend([
                            str(step_id),
                            str(r.id),
                            str(organization_id),
                            "pending",
                        ])
                    await cur.execute(
                        f"""
                        INSERT INTO business.channel_campaign_step_recipients
                            (channel_campaign_step_id, recipient_id,
                             organization_id, status)
                        VALUES {placeholders}
                        ON CONFLICT (channel_campaign_step_id, recipient_id)
                        DO NOTHING
                        """,
                        flat,
                    )
                    rows_inserted += cur.rowcount or 0
            await conn.commit()
        membership_count += rows_inserted

        # Manifest (one row per recipient per initiative — not per step).
        for r in recipients:
            await irm.add_membership(
                initiative_id=initiative_id,
                partner_contract_id=partner_contract_id,
                recipient_id=r.id,
                data_engine_audience_id=audience_spec_id,
            )
        manifest_count += len(recipients)

        # Dub links per (DM step × recipient). mint_links_for_step is
        # already idempotent + bulk + persists rows internally — call
        # it once per step per page. Destination URL is a placeholder
        # using the brand's domain (per-recipient personalization is
        # filled in by a later directive's render-and-submit step).
        if settings.DUB_API_KEY is not None and dm_step_ids:
            placeholder_destination = await _placeholder_destination_url(
                initiative["brand_id"]
            )
            campaign_id_for_links = await _resolve_campaign_id_for_step(
                dm_step_ids[0]
            )
            for step_id in dm_step_ids:
                cc_id = await _resolve_channel_campaign_id_for_step(step_id)
                try:
                    minted = await step_link_minting.mint_links_for_step(
                        channel_campaign_step_id=step_id,
                        organization_id=organization_id,
                        brand_id=initiative["brand_id"],
                        campaign_id=campaign_id_for_links,
                        channel_campaign_id=cc_id,
                        destination_url=placeholder_destination,
                        initiative_id=initiative_id,
                    )
                    dub_link_count += len(minted)
                except step_link_minting.DubNotConfiguredError:
                    logger.warning(
                        "DUB not configured — skipping link minting "
                        "for initiative=%s step=%s",
                        initiative_id, step_id,
                    )

        offset += len(items)
        if audience_limit is not None and recipient_count >= audience_limit:
            break
        if not page.get("has_more"):
            break
        if target_count and recipient_count >= target_count:
            break

    return {
        "recipient_count": recipient_count,
        "membership_count": membership_count,
        "manifest_count": manifest_count,
        "dub_link_count": dub_link_count,
        "decision": decision,
    }


def _member_to_spec(member: dict[str, Any]) -> RecipientSpec | None:
    """Translate a DEX FMCSA-shaped member dict into a RecipientSpec.

    DEX's FMCSA preview returns rows with at minimum dot_number +
    legal_name + a phys_* address bundle. We use ``fmcsa`` as the
    external_source and the dot_number (stringified) as the
    external_id.
    """
    dot = member.get("dot_number") or member.get("usdot")
    if not dot:
        return None
    legal_name = (
        member.get("legal_name")
        or member.get("dba_name")
        or member.get("name")
        or f"DOT {dot}"
    )
    mailing = {}
    if member.get("phys_street") or member.get("phys_city"):
        mailing = {
            "address_line1": member.get("phys_street"),
            "city": member.get("phys_city"),
            "state": member.get("phys_state"),
            "postal_code": member.get("phys_zip"),
            "country": member.get("phys_country") or "US",
        }
    elif member.get("mail_street") or member.get("mail_city"):
        mailing = {
            "address_line1": member.get("mail_street"),
            "city": member.get("mail_city"),
            "state": member.get("mail_state"),
            "postal_code": member.get("mail_zip"),
            "country": member.get("mail_country") or "US",
        }
    return RecipientSpec(
        external_source="fmcsa",
        external_id=str(dot),
        recipient_type="business",
        display_name=legal_name,
        mailing_address=mailing,
        phone=member.get("phone"),
        email=member.get("email"),
        # The full DEX row is captured in metadata so per-recipient
        # creative agents can reason against power_units, state, etc.
        metadata=dict(member),
    )


async def _placeholder_destination_url(brand_id: UUID) -> str:
    """Per-step landing pages are filled in by a downstream subagent.
    The materializer mints Dub links against a placeholder URL using
    the brand's domain so the (step × recipient) → short-link mapping
    exists; render-and-submit later swaps the destination per piece.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT domain FROM business.brands WHERE id = %s",
                (str(brand_id),),
            )
            row = await cur.fetchone()
    domain = row[0] if row and row[0] else "example.com"
    return f"https://{domain}/"


async def _resolve_channel_campaign_id_for_step(step_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT channel_campaign_id FROM business.channel_campaign_steps "
                "WHERE id = %s",
                (str(step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise MaterializerExecutionError(
            f"step {step_id} not found resolving channel_campaign_id"
        )
    return row[0]


async def _resolve_campaign_id_for_step(step_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT campaign_id FROM business.channel_campaign_steps "
                "WHERE id = %s",
                (str(step_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise MaterializerExecutionError(
            f"step {step_id} not found resolving campaign_id"
        )
    return row[0]


__all__ = [
    "MaterializerExecutionError",
    "execute_channel_step_plan",
    "execute_audience_plan",
]
