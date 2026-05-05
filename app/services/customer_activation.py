"""Customer Activation orchestration — Leg 2 + Leg 3 supply-side outreach.

Customer Activation initiatives are operator-authored GTM initiatives where
Ben outreaches to the supply-side audience that a paying demand-side
partner reserved (Leg 2), and on positive reply, introduces the supply-side
member to the demand-side partner (Leg 3).

Two gtm_initiatives are minted as a pair:

  Leg 2 (kind='partner_demand', metadata={authoring_mode: 'manual', leg: 2})
    └─ campaigns (1)
         └─ channel_campaigns (1, channel='email', provider='emailbison')
              └─ channel_campaign_steps (N, hand-authored by operator)

  Leg 3 (kind='partner_demand', parent_initiative_id=Leg2.id,
         metadata={authoring_mode: 'manual', leg: 3})
    └─ campaigns (1)
         └─ channel_campaigns (1, channel='email', provider='emailbison',
                               metadata={trigger: 'positive_reply'})
              └─ channel_campaign_steps (1, content snapshot of the
                                         org's leg3_intro_template at
                                         create-time)

Both initiatives share data_engine_audience_id, partner_id,
partner_contract_id, brand_id, organization_id (Leg 3 inherits from Leg 2).

AI synthesis pipeline stays dormant — operator just doesn't fire
/run-strategic-research or /synthesize-strategy on these initiatives.

Per-org Leg 3 template lives in
business.organizations.metadata.leg3_intro_template:
  {
    "subject": "...",
    "body_text": "...",
    "body_html": "..."   (optional)
  }

Substitution variables surfaced in the template:
  {first_name}        — supply-side member first name
  {partner_company}   — demand-side partner company name
  {partner_contact_name} — demand-side primary contact

The fire-intro flow snapshots whatever is on the Leg 3 channel_campaign
step (which was seeded from the org template at initiative-create) into
the email_messages row, runs the substitution, and sends via EmailBison.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import get_db_connection
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)


class CustomerActivationError(Exception):
    pass


class CustomerActivationNotFound(CustomerActivationError):
    pass


class CustomerActivationValidationError(CustomerActivationError):
    pass


class CustomerActivationLaunchPreconditionFailed(CustomerActivationError):
    pass


_LEG2_DEFAULT_CHANNEL = "email"
_LEG2_DEFAULT_PROVIDER = "emailbison"
_LEG3_DEFAULT_CHANNEL = "email"
_LEG3_DEFAULT_PROVIDER = "emailbison"

# Mirrors self_prospecting._SUPPORTED_CHANNELS — only email today; sms /
# voice_outbound are illegal in operator's context, direct_mail is
# deferred until DMaaS authoring lands.
_SUPPORTED_CHANNELS: dict[str, str] = {
    "email": "emailbison",
}


def supported_channels() -> dict[str, str]:
    return dict(_SUPPORTED_CHANNELS)


# ---------------------------------------------------------------------------
# Per-org automation definition (Leg 2 sequence template + Leg 3 intro template).
#
# v2 model: the OPERATOR defines this once per org (e.g. Freight Expansion).
# Every demand-side partner who pays for an audience reservation under that
# org gets a paired Leg 2 + Leg 3 instance auto-minted from these templates,
# with per-recipient substitutions applied at send time.
#
# Both templates live in business.organizations.metadata so we don't add a
# new table for what is effectively a JSONB blob per org.
# ---------------------------------------------------------------------------


async def _get_org_metadata(organization_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT metadata FROM business.organizations WHERE id = %s",
                (str(organization_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise CustomerActivationNotFound(
            f"organization {organization_id} not found"
        )
    return row[0] or {}


async def _set_org_metadata_key(
    *,
    organization_id: UUID,
    key: str,
    value: Any,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.organizations
                SET metadata = jsonb_set(
                    COALESCE(metadata, '{}'::jsonb),
                    %s,
                    %s::jsonb,
                    true
                ),
                updated_at = NOW()
                WHERE id = %s
                """,
                (
                    "{" + key + "}",
                    Jsonb(value),
                    str(organization_id),
                ),
            )
        await conn.commit()


async def get_org_leg3_intro_template(
    organization_id: UUID,
) -> dict[str, Any]:
    """Per-org Leg 3 intro template (subject + body)."""
    metadata = await _get_org_metadata(organization_id)
    template = metadata.get("leg3_intro_template") or {}
    return {
        "subject": template.get("subject"),
        "body_text": template.get("body_text"),
        "body_html": template.get("body_html"),
    }


async def set_org_leg3_intro_template(
    *,
    organization_id: UUID,
    subject: str | None,
    body_text: str | None,
    body_html: str | None,
) -> dict[str, Any]:
    new_template = {
        "subject": subject,
        "body_text": body_text,
        "body_html": body_html,
    }
    await _set_org_metadata_key(
        organization_id=organization_id,
        key="leg3_intro_template",
        value=new_template,
    )
    return await get_org_leg3_intro_template(organization_id)


# Per-org Leg 2 sequence template — list of step dicts:
#   { step_order, name, delay_days_from_previous, subject, body_text, body_html }
# Empty list = unset; instantiate refuses until ≥1 step exists.


async def get_org_leg2_sequence_template(
    organization_id: UUID,
) -> list[dict[str, Any]]:
    metadata = await _get_org_metadata(organization_id)
    steps = metadata.get("leg2_sequence_template") or []
    if not isinstance(steps, list):
        return []
    return [_normalize_leg2_template_step(s, idx) for idx, s in enumerate(steps)]


async def set_org_leg2_sequence_template(
    *,
    organization_id: UUID,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized = [
        _normalize_leg2_template_step(s, idx) for idx, s in enumerate(steps)
    ]
    await _set_org_metadata_key(
        organization_id=organization_id,
        key="leg2_sequence_template",
        value=normalized,
    )
    return await get_org_leg2_sequence_template(organization_id)


def _normalize_leg2_template_step(step: Any, idx: int) -> dict[str, Any]:
    if not isinstance(step, dict):
        step = {}
    return {
        "step_order": int(step.get("step_order") or (idx + 1)),
        "name": step.get("name"),
        # Step 1 always fires immediately on launch — delay only meaningful
        # from step 2 onward.
        "delay_days_from_previous": (
            0 if idx == 0 else int(step.get("delay_days_from_previous") or 0)
        ),
        "subject": step.get("subject"),
        "body_text": step.get("body_text"),
        "body_html": step.get("body_html"),
    }


# ---------------------------------------------------------------------------
# Customer Activation create — mints Leg 2 + Leg 3 paired initiatives.
# ---------------------------------------------------------------------------


async def create_customer_activation(
    *,
    organization_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
    name: str,
    leg2_channel: str = _LEG2_DEFAULT_CHANNEL,
    leg2_provider: str | None = None,
    leg3_channel: str = _LEG3_DEFAULT_CHANNEL,
    leg3_provider: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create paired Leg 2 + Leg 3 initiatives for a paying customer × audience.

    Creates two `gtm_initiatives` rows (kind='partner_demand'), each with
    its own campaign + channel_campaign tree. Leg 3 inherits Leg 2's
    data_engine_audience_id, partner_id, partner_contract_id, and points
    back via `parent_initiative_id`. Leg 3's single channel_campaign_step
    is seeded with the per-org `leg3_intro_template`.

    Returns ``{leg2_initiative_id, leg3_initiative_id, ...}``. Both
    initiatives start in status='draft'.
    """
    if leg2_channel not in _SUPPORTED_CHANNELS:
        raise CustomerActivationValidationError(
            f"leg2_channel {leg2_channel!r} not supported; "
            f"choose one of {sorted(_SUPPORTED_CHANNELS)}"
        )
    if leg3_channel not in _SUPPORTED_CHANNELS:
        raise CustomerActivationValidationError(
            f"leg3_channel {leg3_channel!r} not supported"
        )
    leg2_resolved_provider = leg2_provider or _SUPPORTED_CHANNELS[leg2_channel]
    leg3_resolved_provider = leg3_provider or _SUPPORTED_CHANNELS[leg3_channel]

    leg3_template = await get_org_leg3_intro_template(organization_id)
    leg2_template_steps = await get_org_leg2_sequence_template(organization_id)

    # ── Leg 2 ─────────────────────────────────────────────────────────
    leg2 = await gtm_svc.create_initiative(
        organization_id=organization_id,
        brand_id=brand_id,
        partner_id=partner_id,
        partner_contract_id=partner_contract_id,
        data_engine_audience_id=data_engine_audience_id,
        kind="partner_demand",
        metadata={
            **(metadata or {}),
            "name": name,
            "authoring_mode": "manual",
            "leg": 2,
        },
    )
    leg2_id = UUID(str(leg2["id"]))

    leg2_campaign_id, leg2_cc_id = await _create_campaign_and_channel_campaign(
        organization_id=organization_id,
        brand_id=brand_id,
        initiative_id=leg2_id,
        name=name,
        channel=leg2_channel,
        provider=leg2_resolved_provider,
        audience_spec_id=data_engine_audience_id,
        channel_campaign_metadata={"leg": 2},
    )

    # ── Leg 3 ─────────────────────────────────────────────────────────
    leg3 = await gtm_svc.create_initiative(
        organization_id=organization_id,
        brand_id=brand_id,
        partner_id=partner_id,
        partner_contract_id=partner_contract_id,
        data_engine_audience_id=data_engine_audience_id,
        kind="partner_demand",
        metadata={
            **(metadata or {}),
            "name": f"{name} — Intros",
            "authoring_mode": "manual",
            "leg": 3,
        },
        parent_initiative_id=leg2_id,
    )
    leg3_id = UUID(str(leg3["id"]))

    leg3_campaign_id, leg3_cc_id = await _create_campaign_and_channel_campaign(
        organization_id=organization_id,
        brand_id=brand_id,
        initiative_id=leg3_id,
        name=f"{name} — Intros",
        channel=leg3_channel,
        provider=leg3_resolved_provider,
        audience_spec_id=data_engine_audience_id,
        channel_campaign_metadata={"leg": 3, "trigger": "positive_reply"},
    )

    # Snapshot org templates into channel_campaign_steps. Future per-org
    # template edits don't retroactively change existing instantiations —
    # each activation carries its own snapshot at create time.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Leg 2 — N steps from leg2_sequence_template.
            for tpl_step in leg2_template_steps:
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaign_steps
                        (channel_campaign_id, campaign_id, organization_id, brand_id,
                         step_order, name, delay_days_from_previous,
                         content_mode, channel_specific_config, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'manual', %s, %s)
                    """,
                    (
                        str(leg2_cc_id),
                        str(leg2_campaign_id),
                        str(organization_id),
                        str(brand_id),
                        tpl_step["step_order"],
                        tpl_step.get("name"),
                        tpl_step["delay_days_from_previous"],
                        Jsonb(
                            {
                                "subject": tpl_step.get("subject"),
                                "body_text": tpl_step.get("body_text"),
                                "body_html": tpl_step.get("body_html"),
                            }
                        ),
                        Jsonb({"snapshotted_from_org_template_at": "create"}),
                    ),
                )

            # Leg 3 — single intro step from leg3_intro_template.
            await cur.execute(
                """
                INSERT INTO business.channel_campaign_steps
                    (channel_campaign_id, campaign_id, organization_id, brand_id,
                     step_order, name, delay_days_from_previous,
                     content_mode, channel_specific_config, metadata)
                VALUES (%s, %s, %s, %s, 1, 'Intro', 0, 'manual', %s, %s)
                """,
                (
                    str(leg3_cc_id),
                    str(leg3_campaign_id),
                    str(organization_id),
                    str(brand_id),
                    Jsonb(
                        {
                            "subject": leg3_template.get("subject"),
                            "body_text": leg3_template.get("body_text"),
                            "body_html": leg3_template.get("body_html"),
                        }
                    ),
                    Jsonb({"snapshotted_from_org_template_at": "create"}),
                ),
            )
        await conn.commit()

    return {
        "leg2_initiative_id": leg2_id,
        "leg3_initiative_id": leg3_id,
        "leg2_campaign_id": leg2_campaign_id,
        "leg2_channel_campaign_id": leg2_cc_id,
        "leg3_campaign_id": leg3_campaign_id,
        "leg3_channel_campaign_id": leg3_cc_id,
        "data_engine_audience_id": data_engine_audience_id,
    }


async def _create_campaign_and_channel_campaign(
    *,
    organization_id: UUID,
    brand_id: UUID,
    initiative_id: UUID,
    name: str,
    channel: str,
    provider: str,
    audience_spec_id: UUID,
    channel_campaign_metadata: dict[str, Any],
) -> tuple[UUID, UUID]:
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
                    channel,
                    provider,
                    str(audience_spec_id),
                    Jsonb({}),
                    Jsonb({}),
                    Jsonb(channel_campaign_metadata),
                    str(initiative_id),
                ),
            )
            row = await cur.fetchone()
            assert row is not None
            cc_id = row[0]
        await conn.commit()
    return UUID(str(campaign_id)), UUID(str(cc_id))


# ---------------------------------------------------------------------------
# Read — full pair view (Leg 2 + Leg 3 + steps + decoration).
# ---------------------------------------------------------------------------


async def get_customer_activation_full(
    leg2_initiative_id: UUID,
) -> dict[str, Any]:
    """Read the full nested shape: Leg 2 initiative + its Leg 3 child +
    each leg's campaign / channel_campaign / steps + brand/partner/org
    decoration for the index page.
    """
    leg2 = await gtm_svc.get_initiative(leg2_initiative_id)
    if leg2 is None or (leg2.get("metadata") or {}).get("leg") != 2:
        raise CustomerActivationNotFound(
            f"customer activation Leg 2 initiative {leg2_initiative_id} not found"
        )

    # Find Leg 3 child via parent_initiative_id.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.gtm_initiatives
                WHERE parent_initiative_id = %s
                  AND (metadata->>'leg')::int = 3
                LIMIT 1
                """,
                (str(leg2_initiative_id),),
            )
            row = await cur.fetchone()
    leg3_id: UUID | None = UUID(str(row[0])) if row else None
    leg3 = await gtm_svc.get_initiative(leg3_id) if leg3_id else None

    leg2_full = await _read_initiative_subtree(leg2_initiative_id)
    leg3_full = (
        await _read_initiative_subtree(leg3_id) if leg3_id else None
    )

    return {
        "leg2": {"initiative": leg2, **leg2_full},
        "leg3": (
            {"initiative": leg3, **leg3_full} if leg3 and leg3_full else None
        ),
    }


async def _read_initiative_subtree(initiative_id: UUID) -> dict[str, Any]:
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
                return {"campaign": None, "channel_campaign": None, "steps": []}
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
                (str(campaign["id"]), str(initiative_id)),
            )
            cc_row = await cur.fetchone()
            if cc_row is None:
                return {"campaign": campaign, "channel_campaign": None, "steps": []}
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
                (str(channel_campaign["id"]),),
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
    return {"campaign": campaign, "channel_campaign": channel_campaign, "steps": steps}


# ---------------------------------------------------------------------------
# Replace Leg 2 steps (multi-step authoring) — same shape as
# self_prospecting.replace_steps but scoped to Leg 2.
# ---------------------------------------------------------------------------


async def replace_leg2_steps(
    *,
    organization_id: UUID,
    leg2_initiative_id: UUID,
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    full = await get_customer_activation_full(leg2_initiative_id)
    initiative = full["leg2"]["initiative"]
    if initiative["organization_id"] != organization_id:
        raise CustomerActivationNotFound(
            f"customer activation {leg2_initiative_id} not in org {organization_id}"
        )
    if initiative["status"] != "draft":
        raise CustomerActivationValidationError(
            f"steps can only be replaced while Leg 2 is 'draft' "
            f"(currently {initiative['status']!r})"
        )
    cc = full["leg2"]["channel_campaign"]
    if cc is None:
        raise CustomerActivationNotFound(
            f"Leg 2 initiative {leg2_initiative_id} has no channel_campaign"
        )
    cc_id = UUID(str(cc["id"]))
    campaign_id = full["leg2"]["campaign"]["id"]
    brand_id = initiative["brand_id"]

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM business.channel_campaign_steps "
                "WHERE channel_campaign_id = %s",
                (str(cc_id),),
            )
            for step in steps:
                await cur.execute(
                    """
                    INSERT INTO business.channel_campaign_steps
                        (channel_campaign_id, campaign_id, organization_id, brand_id,
                         step_order, name, delay_days_from_previous,
                         content_mode, channel_specific_config, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        str(cc_id),
                        str(campaign_id),
                        str(organization_id),
                        str(brand_id),
                        step["step_order"],
                        step.get("name"),
                        step.get("delay_days_from_previous", 0),
                        step.get("content_mode", "manual"),
                        Jsonb(step.get("channel_specific_config") or {}),
                        Jsonb(step.get("metadata") or {}),
                    ),
                )
        await conn.commit()
    return (await get_customer_activation_full(leg2_initiative_id))["leg2"]["steps"]


async def update_leg3_step(
    *,
    organization_id: UUID,
    leg2_initiative_id: UUID,
    subject: str | None,
    body_text: str | None,
    body_html: str | None,
) -> dict[str, Any]:
    """Overwrite the single Leg 3 step's content. Used when the operator
    wants per-initiative override of the org template."""
    full = await get_customer_activation_full(leg2_initiative_id)
    if full["leg3"] is None:
        raise CustomerActivationNotFound(
            f"customer activation {leg2_initiative_id} has no Leg 3"
        )
    leg3 = full["leg3"]
    if leg3["initiative"]["organization_id"] != organization_id:
        raise CustomerActivationNotFound(
            f"customer activation {leg2_initiative_id} not in org {organization_id}"
        )
    if leg3["initiative"]["status"] != "draft":
        raise CustomerActivationValidationError(
            f"Leg 3 step can only be edited while Leg 3 is 'draft' "
            f"(currently {leg3['initiative']['status']!r})"
        )
    if not leg3["steps"]:
        raise CustomerActivationNotFound("Leg 3 has no steps")
    step_id = leg3["steps"][0]["id"]

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps
                SET channel_specific_config = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (
                    Jsonb(
                        {
                            "subject": subject,
                            "body_text": body_text,
                            "body_html": body_html,
                        }
                    ),
                    str(step_id),
                ),
            )
        await conn.commit()
    return (await get_customer_activation_full(leg2_initiative_id))["leg3"]["steps"][0]


# ---------------------------------------------------------------------------
# Launch Leg 2 — manual "mark paid → launch" button on the admin page.
# Cascading: gtm_initiatives.status draft → active, campaigns draft → active,
# channel_campaigns draft → scheduled. Step scheduler picks up from there.
# Leg 3 stays in draft until first positive reply triggers fire-intro.
# ---------------------------------------------------------------------------


async def launch_leg2(
    *,
    organization_id: UUID,
    leg2_initiative_id: UUID,
    actor_user_id: UUID | None = None,
) -> dict[str, Any]:
    full = await get_customer_activation_full(leg2_initiative_id)
    leg2 = full["leg2"]
    initiative = leg2["initiative"]
    if initiative["organization_id"] != organization_id:
        raise CustomerActivationNotFound(
            f"customer activation {leg2_initiative_id} not in org {organization_id}"
        )
    if initiative["status"] != "draft":
        raise CustomerActivationLaunchPreconditionFailed(
            f"Leg 2 status must be 'draft' to launch (currently {initiative['status']!r})"
        )
    if leg2["campaign"] is None or leg2["channel_campaign"] is None:
        raise CustomerActivationLaunchPreconditionFailed(
            "Leg 2 has no campaign or channel_campaign"
        )
    steps = leg2["steps"]
    if not steps:
        raise CustomerActivationLaunchPreconditionFailed(
            "Leg 2 has no steps; add at least one before launching"
        )
    channel = leg2["channel_campaign"]["channel"]
    for step in steps:
        if step["content_mode"] == "manual":
            cfg = step["channel_specific_config"] or {}
            if channel == "email" and not cfg.get("subject"):
                raise CustomerActivationLaunchPreconditionFailed(
                    f"Leg 2 step {step['step_order']} missing subject"
                )
            if not (cfg.get("body_text") or cfg.get("body_html")):
                raise CustomerActivationLaunchPreconditionFailed(
                    f"Leg 2 step {step['step_order']} missing body_text"
                )

    cc_id = leg2["channel_campaign"]["id"]
    campaign_id = leg2["campaign"]["id"]

    await gtm_svc.transition_status(
        leg2_initiative_id,
        new_status="active",
        history_event={
            "kind": "customer_activation_leg2_launched",
            "actor_user_id": str(actor_user_id) if actor_user_id else None,
            "step_count": len(steps),
        },
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE business.campaigns SET status = 'active', updated_at = NOW() "
                "WHERE id = %s",
                (str(campaign_id),),
            )
            await cur.execute(
                "UPDATE business.channel_campaigns SET status = 'scheduled', updated_at = NOW() "
                "WHERE id = %s",
                (str(cc_id),),
            )
        await conn.commit()
    return await get_customer_activation_full(leg2_initiative_id)


# ---------------------------------------------------------------------------
# Auto-instantiation entry point — called when a demand-side partner pays
# for a supply-side audience reservation. Reads the org's pre-authored
# automation (Leg 2 sequence + Leg 3 intro), mints the paired initiatives,
# snapshots both templates onto the new rows, launches Leg 2 immediately
# (payment IS the trigger). Leg 3 stays in draft until first positive
# reply triggers fire-intro.
#
# Refuses with CustomerActivationValidationError if the org templates
# aren't authored yet — operator must set them up first.
# ---------------------------------------------------------------------------


async def instantiate_for_payment(
    *,
    organization_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    partner_contract_id: UUID,
    data_engine_audience_id: UUID,
    name: str | None = None,
    actor_user_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    leg2_template = await get_org_leg2_sequence_template(organization_id)
    leg3_template = await get_org_leg3_intro_template(organization_id)
    if not leg2_template:
        raise CustomerActivationValidationError(
            f"organization {organization_id} has no leg2_sequence_template "
            "authored — set it via the Customer Activation org config page "
            "before payment-driven instantiations can run"
        )
    # Leg 3 template can be empty for a draft state; we won't block
    # instantiation on it, but fire-intro will fail later if it's still
    # empty by the time a positive reply arrives.

    resolved_name = name or f"Activation — {partner_id} × {data_engine_audience_id}"
    created = await create_customer_activation(
        organization_id=organization_id,
        brand_id=brand_id,
        partner_id=partner_id,
        partner_contract_id=partner_contract_id,
        data_engine_audience_id=data_engine_audience_id,
        name=resolved_name,
        metadata={
            **(metadata or {}),
            "instantiated_by": "payment",
        },
    )
    leg2_id = created["leg2_initiative_id"]

    # Launch Leg 2 right away — payment triggered this; no manual gate.
    launched = await launch_leg2(
        organization_id=organization_id,
        leg2_initiative_id=leg2_id,
        actor_user_id=actor_user_id,
    )
    return {
        **created,
        "launched": True,
        "leg2_status": launched["leg2"]["initiative"]["status"],
        "leg3_status": (
            launched["leg3"]["initiative"]["status"] if launched["leg3"] else None
        ),
    }


async def list_customer_activations(
    *,
    organization_id: UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List Leg 2 initiatives (parent of each pair) with brand/org/partner
    decoration, optionally filtered by org. Leg 3 child rows are not
    returned — they're surfaced via the detail view.
    """
    args: list[Any] = []
    where = [
        "i.kind = 'partner_demand'",
        "(i.metadata->>'leg')::int = 2",
    ]
    if organization_id is not None:
        where.append("i.organization_id = %s")
        args.append(str(organization_id))
    args.extend([min(max(limit, 1), 200), max(offset, 0)])

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT i.id, i.organization_id, i.brand_id, i.partner_id,
                       i.partner_contract_id, i.data_engine_audience_id,
                       i.status, i.metadata, i.created_at, i.updated_at,
                       b.name AS brand_name,
                       o.name AS organization_name,
                       p.name AS partner_name
                FROM business.gtm_initiatives i
                LEFT JOIN business.brands b ON b.id = i.brand_id
                LEFT JOIN business.organizations o ON o.id = i.organization_id
                LEFT JOIN business.demand_side_partners p ON p.id = i.partner_id
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
            "partner_id": r[3],
            "partner_contract_id": r[4],
            "data_engine_audience_id": r[5],
            "status": r[6],
            "metadata": r[7] or {},
            "name": (r[7] or {}).get("name"),
            "created_at": r[8],
            "updated_at": r[9],
            "brand_name": r[10],
            "organization_name": r[11],
            "partner_name": r[12],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Fire intro — fires Leg 3 single-shot send for one positive-reply recipient.
# ---------------------------------------------------------------------------


async def fire_intro(
    *,
    leg2_initiative_id: UUID,
    email_message_id: UUID,
    source: str,
) -> dict[str, Any]:
    """Resolve the Leg 3 step + recipient + partner for `email_message_id`,
    render the intro content, send via EmailBison, and mark the
    classification row's intro_fired_at.

    `email_message_id` MUST reference a row in business.email_messages
    that has classification='positive' in
    business.email_reply_classifications. The message's
    channel_campaign_step's channel_campaign's initiative MUST equal
    `leg2_initiative_id`.

    `source` is recorded in the intro email_message's metadata for audit.

    Returns ``{intro_email_message_id, classification_id}``.

    NOTE — actual EmailBison send is not implemented here. This function
    builds the rendered email_messages row and marks the classification
    row, but the network call to EmailBison is left to the
    `intro.send_intro` Trigger task. v1: this function is invoked by the
    Trigger task; the task does the EB call + status updates.
    """
    # Resolve classification row + parent message + recipient.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT erc.id, erc.classification, erc.intro_fired_at,
                       em.id, em.organization_id, em.brand_id,
                       em.channel_campaign_step_id, em.recipient_id
                FROM business.email_reply_classifications erc
                JOIN business.email_messages em ON em.id = erc.email_message_id
                WHERE erc.email_message_id = %s
                """,
                (str(email_message_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise CustomerActivationValidationError(
            f"no email_reply_classifications row for email_message_id={email_message_id}"
        )
    classification_id, classification, intro_fired_at, _em_id, org_id, brand_id, step_id, recipient_id = row
    if classification != "positive":
        raise CustomerActivationValidationError(
            f"classification is {classification!r}, not 'positive'; "
            "only positive replies fire intros"
        )
    if intro_fired_at is not None:
        raise CustomerActivationValidationError(
            f"intro already fired at {intro_fired_at} for classification {classification_id}"
        )

    # Resolve the Leg 3 step (single step on Leg 3's channel_campaign).
    full = await get_customer_activation_full(leg2_initiative_id)
    if full["leg3"] is None or not full["leg3"]["steps"]:
        raise CustomerActivationValidationError(
            f"customer activation {leg2_initiative_id} has no Leg 3 step"
        )
    leg3 = full["leg3"]
    leg3_step = leg3["steps"][0]
    leg3_cc = leg3["channel_campaign"]
    leg3_campaign = leg3["campaign"]
    template = leg3_step["channel_specific_config"] or {}

    # Resolve recipient (for substitutions).
    recipient = await _resolve_recipient(recipient_id) if recipient_id else None
    # Resolve partner (for substitutions).
    leg2_initiative = full["leg2"]["initiative"]
    partner = await _resolve_partner(leg2_initiative.get("partner_id"))

    rendered_subject, rendered_body_text, rendered_body_html = _render_template(
        template=template,
        recipient=recipient,
        partner=partner,
    )

    # Build intro email_messages row in 'pending' state. The Trigger task
    # will dispatch via EmailBison and transition to 'sent'.
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages
                    (organization_id, brand_id, campaign_id, channel_campaign_id,
                     channel_campaign_step_id, recipient_id,
                     subject_snapshot, body_snapshot, status, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    str(leg3_campaign["id"]),
                    str(leg3_cc["id"]),
                    str(leg3_step["id"]),
                    str(recipient_id) if recipient_id else None,
                    rendered_subject,
                    rendered_body_text,
                    Jsonb(
                        {
                            "intro_for_classification_id": str(classification_id),
                            "leg": 3,
                            "fire_intro_source": source,
                        }
                    ),
                ),
            )
            intro_row = await cur.fetchone()
            assert intro_row is not None
            intro_email_message_id = intro_row[0]

            await cur.execute(
                """
                UPDATE business.email_reply_classifications
                SET intro_fired_at = NOW(),
                    intro_email_message_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(intro_email_message_id), str(classification_id)),
            )
        await conn.commit()

    return {
        "intro_email_message_id": intro_email_message_id,
        "classification_id": classification_id,
        "rendered_subject": rendered_subject,
        "rendered_body_text": rendered_body_text,
    }


async def _resolve_recipient(recipient_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, first_name, last_name, email
                FROM business.recipients
                WHERE id = %s
                """,
                (str(recipient_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "first_name": row[1],
        "last_name": row[2],
        "email": row[3],
    }


async def _resolve_partner(partner_id: Any) -> dict[str, Any] | None:
    if partner_id is None:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, primary_contact_name, primary_contact_email
                FROM business.demand_side_partners
                WHERE id = %s
                """,
                (str(partner_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "primary_contact_name": row[2],
        "primary_contact_email": row[3],
    }


def _render_template(
    *,
    template: dict[str, Any],
    recipient: dict[str, Any] | None,
    partner: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    subs = {
        "first_name": (recipient or {}).get("first_name") or "there",
        "partner_company": (partner or {}).get("name") or "our partner",
        "partner_contact_name": (partner or {}).get("primary_contact_name")
        or (partner or {}).get("name")
        or "our partner contact",
    }

    def render(text: str | None) -> str | None:
        if text is None:
            return None
        out = text
        for key, value in subs.items():
            out = out.replace("{" + key + "}", str(value))
        return out

    return (
        render(template.get("subject")),
        render(template.get("body_text")),
        render(template.get("body_html")),
    )


# ---------------------------------------------------------------------------
# Dispatcher tail — find positive replies that haven't been intro'd, return
# them in scheduling order. The Trigger.dev schedule
# `intro.dispatch_pending_positives` calls this and enqueues per-row
# `intro.send_intro` task runs.
# ---------------------------------------------------------------------------


async def list_pending_positive_replies(limit: int = 50) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT erc.id, erc.email_message_id, em.channel_campaign_step_id,
                       cs.channel_campaign_id, cc.initiative_id
                FROM business.email_reply_classifications erc
                JOIN business.email_messages em ON em.id = erc.email_message_id
                JOIN business.channel_campaign_steps cs
                    ON cs.id = em.channel_campaign_step_id
                JOIN business.channel_campaigns cc ON cc.id = cs.channel_campaign_id
                WHERE erc.classification = 'positive'
                  AND erc.intro_fired_at IS NULL
                ORDER BY erc.classified_at ASC
                LIMIT %s
                """,
                (min(max(limit, 1), 200),),
            )
            rows = await cur.fetchall()
    return [
        {
            "classification_id": r[0],
            "email_message_id": r[1],
            "channel_campaign_step_id": r[2],
            "channel_campaign_id": r[3],
            "leg2_initiative_id": r[4],
        }
        for r in rows
    ]


__all__ = [
    "CustomerActivationError",
    "CustomerActivationNotFound",
    "CustomerActivationValidationError",
    "CustomerActivationLaunchPreconditionFailed",
    "supported_channels",
    "get_org_leg2_sequence_template",
    "set_org_leg2_sequence_template",
    "get_org_leg3_intro_template",
    "set_org_leg3_intro_template",
    "create_customer_activation",
    "instantiate_for_payment",
    "get_customer_activation_full",
    "replace_leg2_steps",
    "update_leg3_step",
    "launch_leg2",
    "list_customer_activations",
    "fire_intro",
    "list_pending_positive_replies",
]
