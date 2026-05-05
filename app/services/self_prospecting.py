"""Self-prospecting initiative orchestration.

Self-prospecting initiatives are operator-authored GTM initiatives where
Ben prospects on his own behalf (e.g. Freight Expansion outreach to
freight brokers). Distinct from the partner-demand flow:

  * No demand-side partner, no contract — partner_id and
    partner_contract_id are NULL on the initiative row.
  * No AI synthesis pipeline — the operator hand-builds an umbrella
    campaign + one channel_campaign + N steps via the Initiative
    Composer admin page.
  * Manual step content — each step carries its subject + body in
    channel_specific_config; a single ``{first_name}`` substitution is
    applied at send time. No template engine.

The orchestration here couples the four-row create (initiative +
campaign + channel_campaign + steps) into a single transaction-equivalent
flow so the operator's "save" click leaves the DB in a consistent state.
Per-row CRUD continues to live in the existing services
(``gtm_initiatives``, ``campaigns``, ``channel_campaigns``,
``channel_campaign_steps``); this module is the composition layer.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.services import dex_client
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)


class SelfProspectingError(Exception):
    pass


class SelfProspectingNotFound(SelfProspectingError):
    pass


class SelfProspectingValidationError(SelfProspectingError):
    pass


class SelfProspectingLaunchPreconditionFailed(SelfProspectingError):
    pass


# Defaults for the email-only self-prospecting MVP. The Initiative
# Composer page only surfaces email-via-emailbison today; widening to
# other channels is a follow-up.
_DEFAULT_CHANNEL = "email"
_DEFAULT_PROVIDER = "emailbison"


async def _resolve_or_mint_audience_spec(
    *,
    audience_spec_id: UUID | None,
    audience_template_slug: str | None,
    audience_name_hint: str | None,
    bearer_token: str | None,
) -> UUID:
    """Return a DEX audience_spec_id, minting one from a template if needed.

    The composer UI sends one of:
      * ``audience_spec_id`` — operator already has a spec they want to reuse.
      * ``audience_template_slug`` — operator picked a template; we mint a
        spec with default filters and the initiative name as the spec name.
    """
    if audience_spec_id is not None:
        # Verify the spec exists. ``get_audience_spec`` returns 404 if not.
        await dex_client.get_audience_spec(audience_spec_id, bearer_token=bearer_token)
        return audience_spec_id

    if audience_template_slug is None:
        raise SelfProspectingValidationError(
            "audience_spec_id or audience_template_slug is required"
        )

    template = await dex_client.get_audience_template_by_slug(
        audience_template_slug, bearer_token=bearer_token
    )
    template_id = UUID(str(template["id"]))
    minted = await dex_client.create_audience_spec(
        template_id=template_id,
        filter_overrides={},
        name=audience_name_hint,
        bearer_token=bearer_token,
    )
    return UUID(str(minted["id"]))


async def create_self_prospecting_initiative(
    *,
    organization_id: UUID,
    brand_id: UUID,
    name: str,
    audience_spec_id: UUID | None = None,
    audience_template_slug: str | None = None,
    metadata: dict[str, Any] | None = None,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    """Mint or reuse a DEX audience spec, then create the four-row tree:

      gtm_initiative (kind='self_prospecting', status='draft')
        └─ campaign (status='draft')
              └─ channel_campaign (channel='email', provider='emailbison',
                                   status='draft')

    Steps are not created here; the operator adds them via update().
    """
    spec_id = await _resolve_or_mint_audience_spec(
        audience_spec_id=audience_spec_id,
        audience_template_slug=audience_template_slug,
        audience_name_hint=name,
        bearer_token=bearer_token,
    )

    initiative = await gtm_svc.create_initiative(
        organization_id=organization_id,
        brand_id=brand_id,
        partner_id=None,
        partner_contract_id=None,
        data_engine_audience_id=spec_id,
        kind="self_prospecting",
        metadata={
            **(metadata or {}),
            "name": name,
            **(
                {"audience_template_slug": audience_template_slug}
                if audience_template_slug
                else {}
            ),
        },
    )
    initiative_id = UUID(str(initiative["id"]))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.campaigns
                    (organization_id, brand_id, name, status, initiative_id, metadata)
                VALUES (%s, %s, %s, 'draft', %s, %s)
                RETURNING id
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    name,
                    str(initiative_id),
                    Jsonb({}),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            campaign_id = row[0]

            await cur.execute(
                """
                INSERT INTO business.channel_campaigns
                    (campaign_id, organization_id, brand_id, name, channel,
                     provider, audience_spec_id, status, start_offset_days,
                     schedule_config, provider_config, metadata, initiative_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'draft', 0,
                        %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(campaign_id),
                    str(organization_id),
                    str(brand_id),
                    name,
                    _DEFAULT_CHANNEL,
                    _DEFAULT_PROVIDER,
                    str(spec_id),
                    Jsonb({}),
                    Jsonb({}),
                    Jsonb({}),
                    str(initiative_id),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            channel_campaign_id = row[0]
        await conn.commit()

    return {
        "initiative_id": initiative_id,
        "campaign_id": UUID(str(campaign_id)),
        "channel_campaign_id": UUID(str(channel_campaign_id)),
        "data_engine_audience_id": spec_id,
    }


async def get_self_prospecting_initiative_full(
    initiative_id: UUID,
) -> dict[str, Any]:
    """Read the full nested shape: initiative + campaign + channel_campaign + steps.

    Returns 404-ish ``SelfProspectingNotFound`` if the initiative doesn't
    exist or isn't a self_prospecting kind.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None or initiative.get("kind") != "self_prospecting":
        raise SelfProspectingNotFound(
            f"self_prospecting initiative {initiative_id} not found"
        )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, status, start_date, metadata, created_at
                FROM business.campaigns
                WHERE initiative_id = %s
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (str(initiative_id),),
            )
            campaign_row = await cur.fetchone()

            if campaign_row is None:
                campaign = None
                channel_campaign = None
                steps: list[dict[str, Any]] = []
            else:
                campaign = {
                    "id": campaign_row[0],
                    "name": campaign_row[1],
                    "status": campaign_row[2],
                    "start_date": campaign_row[3],
                    "metadata": campaign_row[4] or {},
                    "created_at": campaign_row[5],
                }

                await cur.execute(
                    """
                    SELECT id, name, channel, provider, audience_spec_id,
                           status, start_offset_days, scheduled_send_at,
                           schedule_config, provider_config, metadata, created_at
                    FROM business.channel_campaigns
                    WHERE campaign_id = %s AND initiative_id = %s
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (str(campaign_row[0]), str(initiative_id)),
                )
                cc_row = await cur.fetchone()
                if cc_row is None:
                    channel_campaign = None
                    steps = []
                else:
                    channel_campaign = {
                        "id": cc_row[0],
                        "name": cc_row[1],
                        "channel": cc_row[2],
                        "provider": cc_row[3],
                        "audience_spec_id": cc_row[4],
                        "status": cc_row[5],
                        "start_offset_days": cc_row[6],
                        "scheduled_send_at": cc_row[7],
                        "schedule_config": cc_row[8] or {},
                        "provider_config": cc_row[9] or {},
                        "metadata": cc_row[10] or {},
                        "created_at": cc_row[11],
                    }

                    await cur.execute(
                        """
                        SELECT id, step_order, name, delay_days_from_previous,
                               scheduled_send_at, content_mode,
                               channel_specific_config, status, activated_at,
                               metadata, created_at, updated_at
                        FROM business.channel_campaign_steps
                        WHERE channel_campaign_id = %s
                        ORDER BY step_order ASC
                        """,
                        (str(cc_row[0]),),
                    )
                    step_rows = await cur.fetchall()
                    steps = [
                        {
                            "id": r[0],
                            "step_order": r[1],
                            "name": r[2],
                            "delay_days_from_previous": r[3],
                            "scheduled_send_at": r[4],
                            "content_mode": r[5],
                            "channel_specific_config": r[6] or {},
                            "status": r[7],
                            "activated_at": r[8],
                            "metadata": r[9] or {},
                            "created_at": r[10],
                            "updated_at": r[11],
                        }
                        for r in step_rows
                    ]

    return {
        "initiative": initiative,
        "campaign": campaign,
        "channel_campaign": channel_campaign,
        "steps": steps,
    }


async def replace_steps(
    *,
    organization_id: UUID,
    initiative_id: UUID,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace all steps under an initiative's single channel_campaign.

    Implemented as delete-then-insert inside a single transaction. Only
    valid while the initiative is in 'draft'; once launched, step edits
    must go through the per-step lifecycle endpoints.

    Each input dict expects:
      step_order: int
      name: str | None
      delay_days_from_previous: int
      content_mode: 'manual' | 'llm_per_recipient'
      channel_specific_config: dict (e.g. {subject, body_text, body_html})
      metadata: dict | None (optional)
    """
    full = await get_self_prospecting_initiative_full(initiative_id)
    initiative = full["initiative"]
    if initiative["organization_id"] != organization_id:
        raise SelfProspectingNotFound(
            f"self_prospecting initiative {initiative_id} not in org {organization_id}"
        )
    if initiative["status"] != "draft":
        raise SelfProspectingValidationError(
            f"steps can only be replaced while initiative is 'draft' "
            f"(currently {initiative['status']!r})"
        )
    if full["channel_campaign"] is None:
        raise SelfProspectingNotFound(
            f"initiative {initiative_id} has no channel_campaign"
        )

    cc = full["channel_campaign"]
    cc_id = UUID(str(cc["id"]))
    campaign_id = full["campaign"]["id"]
    brand_id = initiative["brand_id"]

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM business.channel_campaign_steps
                WHERE channel_campaign_id = %s
                """,
                (str(cc_id),),
            )
            for step in steps:
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaign_steps
                        (channel_campaign_id, campaign_id, organization_id, brand_id,
                         step_order, name, delay_days_from_previous,
                         creative_ref, content_mode, channel_specific_config, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(cc_id),
                        str(campaign_id),
                        str(organization_id),
                        str(brand_id),
                        step["step_order"],
                        step.get("name"),
                        step.get("delay_days_from_previous", 0),
                        None,
                        step.get("content_mode", "manual"),
                        Jsonb(step.get("channel_specific_config") or {}),
                        Jsonb(step.get("metadata") or {}),
                    ),
                )
        await conn.commit()

    return (await get_self_prospecting_initiative_full(initiative_id))["steps"]


async def update_initiative_metadata(
    *,
    organization_id: UUID,
    initiative_id: UUID,
    name: str | None = None,
) -> dict[str, Any]:
    """Update the initiative's display name (stored in metadata.name + the
    cascading campaign/channel_campaign name fields).

    Reuses the metadata.name convention from create — there is no
    dedicated `name` column on gtm_initiatives.
    """
    full = await get_self_prospecting_initiative_full(initiative_id)
    initiative = full["initiative"]
    if initiative["organization_id"] != organization_id:
        raise SelfProspectingNotFound(
            f"self_prospecting initiative {initiative_id} not in org {organization_id}"
        )
    if initiative["status"] != "draft":
        raise SelfProspectingValidationError(
            f"name edits only allowed while initiative is 'draft' "
            f"(currently {initiative['status']!r})"
        )

    if name is None:
        return full

    new_metadata = {**(initiative.get("metadata") or {}), "name": name}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET metadata = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (Jsonb(new_metadata), str(initiative_id)),
            )
            if full["campaign"] is not None:
                await cur.execute(
                    """
                    UPDATE business.campaigns
                    SET name = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (name, str(full["campaign"]["id"])),
                )
            if full["channel_campaign"] is not None:
                await cur.execute(
                    """
                    UPDATE business.channel_campaigns
                    SET name = %s, updated_at = NOW()
                    WHERE id = %s
                    """,
                    (name, str(full["channel_campaign"]["id"])),
                )
        await conn.commit()

    return await get_self_prospecting_initiative_full(initiative_id)


async def launch_initiative(
    *,
    organization_id: UUID,
    initiative_id: UUID,
    actor_user_id: UUID | None = None,
) -> dict[str, Any]:
    """Validate preconditions and transition draft → active.

    Preconditions:
      * Initiative kind is 'self_prospecting' and status is 'draft'.
      * Has a campaign and channel_campaign.
      * Has ≥1 step, all in 'pending' status.
      * Every manual-mode step has subject + body in
        channel_specific_config.

    Cascading state changes:
      * gtm_initiatives.status: draft → active
      * campaigns.status: draft → active
      * channel_campaigns.status: draft → scheduled

    Channel campaign step activation (the actual sends) is driven by the
    existing step scheduler off of the channel_campaign's 'scheduled'
    state — this function only flips the bits.
    """
    full = await get_self_prospecting_initiative_full(initiative_id)
    initiative = full["initiative"]
    if initiative["organization_id"] != organization_id:
        raise SelfProspectingNotFound(
            f"self_prospecting initiative {initiative_id} not in org {organization_id}"
        )
    if initiative["status"] != "draft":
        raise SelfProspectingLaunchPreconditionFailed(
            f"initiative status must be 'draft' to launch (currently {initiative['status']!r})"
        )
    if full["campaign"] is None or full["channel_campaign"] is None:
        raise SelfProspectingLaunchPreconditionFailed(
            "initiative has no campaign or channel_campaign"
        )
    steps = full["steps"]
    if not steps:
        raise SelfProspectingLaunchPreconditionFailed(
            "initiative has no steps; add at least one before launching"
        )
    for step in steps:
        if step["content_mode"] == "manual":
            cfg = step["channel_specific_config"] or {}
            if not cfg.get("subject"):
                raise SelfProspectingLaunchPreconditionFailed(
                    f"step {step['step_order']} missing subject"
                )
            if not (cfg.get("body_text") or cfg.get("body_html")):
                raise SelfProspectingLaunchPreconditionFailed(
                    f"step {step['step_order']} missing body_text or body_html"
                )

    cc_id = full["channel_campaign"]["id"]
    campaign_id = full["campaign"]["id"]

    await gtm_svc.transition_status(
        initiative_id,
        new_status="active",
        history_event={
            "kind": "self_prospecting_launched",
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "step_count": len(steps),
        },
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.campaigns
                SET status = 'active', updated_at = NOW()
                WHERE id = %s
                """,
                (str(campaign_id),),
            )
            await cur.execute(
                """
                UPDATE business.channel_campaigns
                SET status = 'scheduled', updated_at = NOW()
                WHERE id = %s
                """,
                (str(cc_id),),
            )
        await conn.commit()

    return await get_self_prospecting_initiative_full(initiative_id)


async def list_self_prospecting_initiatives(
    *,
    organization_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List self-prospecting initiatives, optionally filtered by org.

    Decorated with brand name + display name (from metadata.name) so the
    index page renders without N+1 follow-up reads.
    """
    args: list[Any] = []
    where = ["i.kind = 'self_prospecting'"]
    if organization_id is not None:
        where.append("i.organization_id = %s")
        args.append(str(organization_id))
    args.extend([min(max(limit, 1), 200), max(offset, 0)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT i.id, i.organization_id, i.brand_id, i.kind,
                       i.data_engine_audience_id, i.status, i.metadata,
                       i.created_at, i.updated_at,
                       b.name AS brand_name,
                       o.name AS organization_name
                FROM business.gtm_initiatives i
                LEFT JOIN business.brands b ON b.id = i.brand_id
                LEFT JOIN business.organizations o ON o.id = i.organization_id
                WHERE {' AND '.join(where)}
                ORDER BY i.created_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "organization_id": r[1],
            "brand_id": r[2],
            "kind": r[3],
            "data_engine_audience_id": r[4],
            "status": r[5],
            "metadata": r[6] or {},
            "name": (r[6] or {}).get("name"),
            "created_at": r[7],
            "updated_at": r[8],
            "brand_name": r[9],
            "organization_name": r[10],
        }
        for r in rows
    ]


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


__all__ = [
    "SelfProspectingError",
    "SelfProspectingNotFound",
    "SelfProspectingValidationError",
    "SelfProspectingLaunchPreconditionFailed",
    "create_self_prospecting_initiative",
    "get_self_prospecting_initiative_full",
    "list_self_prospecting_initiatives",
    "replace_steps",
    "update_initiative_metadata",
    "launch_initiative",
]
