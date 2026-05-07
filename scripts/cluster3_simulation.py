#!/usr/bin/env python3
"""Cluster 3 end-to-end simulation harness.

One script, three modes (selected via --mode):

  seed       — scaffold a complete pre-Cluster-3 fixture in the DB:
               sim org, brand, partner, contract, proposal (paid),
               Leg 2 + Leg 3 initiatives via customer_activation,
               channel_campaign + steps, recipients, "sent" email_messages,
               and optional source-material artifacts.

  simulate   — for each seeded recipient email_message, build a fake
               inbound reply (positive / negative / question / unsub mix),
               then drive Cluster 3 by calling inbox_orchestrator
               directly (bypasses the EB webhook layer).

  teardown   — wipe everything tagged with the simulation marker.

  full       — run all three: seed → simulate → teardown.

Idempotent and tagged: every row created stamps either
``metadata->>'cluster3_sim'='true'`` or sits under the well-known sim
org id (``cluster3-sim`` slug). Re-running ``seed`` updates in place.

Usage:

  doppler run --project hq-x --config dev -- \\
    uv run python -m scripts.cluster3_simulation --mode=full

  # or step-by-step:
  ... --mode=seed
  ... --mode=simulate
  ... --mode=teardown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.db import close_pool, get_db_connection, init_pool
from app.services import customer_activation, inbox_orchestrator

# ── Fixed simulation identifiers ──────────────────────────────────────────
SIM_TAG_KEY = "cluster3_sim"
SIM_TAG_VALUE = "true"

SIM_ORG_SLUG = "cluster3-sim"
SIM_ORG_NAME = "Cluster 3 Simulation Org"
SIM_BRAND_NAME = "Sim Brand"
SIM_PARTNER_NAME = "Acme Logistics Partners (sim)"

# A stable made-up audience id — no DEX backing needed for this sim.
SIM_AUDIENCE_ID = UUID("c1c1c1c1-0000-0000-0000-000000000001")
SIM_AUDIENCE_TEMPLATE_SLUG = "sim-active-freight-brokerages"

SIM_PROPOSED_TRANSFER_COUNT = 5
SIM_PRICE_PER_TRANSFER_CENTS = 100_000  # $1000
SIM_WINDOW_DAYS = 90

# How many supply-side recipients to seed.
SIM_RECIPIENT_COUNT = 8

# ── Cluster 1 (self_prospecting) sim identifiers ─────────────────────────
SIM_C1_INITIATIVE_NAME = "C1 Sim — freight broker outreach"
SIM_C1_AUDIENCE_TEMPLATE_SLUG = "sim-active-freight-brokerages-c1"
SIM_C1_AUDIENCE_ID = UUID("c1c1c1c1-1111-0000-0000-000000000001")
SIM_C1_RECIPIENT_COUNT = 5

SIM_C1_BROKER_NAMES = [
    ("Alpine Freight Solutions", "ops@alpinefreight.example"),
    ("Bayou Logistics Co", "hello@bayoulogistics.example"),
    ("Cascade Brokerage Partners", "team@cascadebroker.example"),
    ("Delta Lane Logistics", "info@deltalane.example"),
    ("Evergreen Freight Brokers", "ops@evergreenfreight.example"),
]


# Reply scenarios per recipient. Keep one positive sample at index 0 so
# the simulator's "single reply" mode hits a positive-classification path
# by default.
REPLY_SCENARIOS = [
    {
        "recipient_idx": 0,
        "subject": "Re: quick intro request",
        "from_suffix": "+lead0@simcarriers.com",
        "body": (
            "Yes interested — happy to chat. Send me a calendar link and "
            "I'll grab time this week. Let's talk."
        ),
        "expected": "positive",
    },
    {
        "recipient_idx": 1,
        "subject": "Re: quick intro request",
        "from_suffix": "+lead1@simcarriers.com",
        "body": "Tell me more about who's on the other side of this.",
        "expected": "question",
    },
    {
        "recipient_idx": 2,
        "subject": "Re: quick intro request",
        "from_suffix": "+lead2@simcarriers.com",
        "body": "Not interested, thanks.",
        "expected": "negative",
    },
    {
        "recipient_idx": 3,
        "subject": "Re: quick intro request",
        "from_suffix": "+lead3@simcarriers.com",
        "body": "Please remove me from your list. Unsubscribe.",
        "expected": "unsubscribe",
    },
    {
        "recipient_idx": 4,
        "subject": "Out of office",
        "from_suffix": "+lead4@simcarriers.com",
        "body": "I am currently out of office until next week.",
        "expected": "auto_reply",
    },
    {
        "recipient_idx": 5,
        "subject": "Re: quick intro request",
        "from_suffix": "+lead5@simcarriers.com",
        "body": "Sounds good — book a time and we'll see what makes sense.",
        "expected": "positive",
    },
]


REPLY_SCENARIOS_C1 = [
    {
        "recipient_idx": 0,
        "subject": "Re: quick intro",
        "body": (
            "Yes — happy to chat. Send a calendar link and I'll grab "
            "time this week."
        ),
        "expected": "positive",
    },
    {
        "recipient_idx": 1,
        "subject": "Re: quick intro",
        "body": "What does this look like in practice? Tell me more.",
        "expected": "question",
    },
    {
        "recipient_idx": 2,
        "subject": "Re: quick intro",
        "body": "Not a fit for us right now — thanks.",
        "expected": "negative",
    },
    {
        "recipient_idx": 3,
        "subject": "Out of office",
        "body": "I am out of office until next week. Will reply when I return.",
        "expected": "auto_reply",
    },
    {
        "recipient_idx": 4,
        "subject": "Re: quick intro",
        "body": (
            "Sounds good — book a time and we'll see what makes sense."
        ),
        "expected": "positive",
    },
]


# ── Logging helper ────────────────────────────────────────────────────────


def log(msg: str) -> None:
    print(f"[cluster3-sim] {msg}", flush=True)


def _abort(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ── Seed phase ────────────────────────────────────────────────────────────


_LEG2_SEQUENCE_TEMPLATE = [
    {
        "step_order": 1,
        "name": "Leg 2 — outreach 1",
        "delay_days_from_previous": 0,
        "subject": "{first_name}, quick intro request",
        "body_text": (
            "Hi {first_name},\n\nQuick note — Acme Logistics Partners is "
            "actively looking to talk to a small set of brokerages "
            "matching your profile. Open to a 15-min call this week?\n\n"
            "— Ben"
        ),
    }
]

_LEG3_INTRO_TEMPLATE = {
    "subject": "{first_name} x {partner_contact_name}",
    "body_text": (
        "{first_name} and {partner_contact_name},\n\n"
        "Connecting you two — timing here aligned. {partner_contact_name} "
        "at {partner_company} is actively looking for brokerages that "
        "match your profile. Compare notes when you have a minute.\n\n"
        "— Ben"
    ),
}


async def _upsert_org() -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (SIM_ORG_SLUG,),
            )
            row = await cur.fetchone()
            if row:
                org_id = row[0]
            else:
                await cur.execute(
                    """
                    INSERT INTO business.organizations (slug, name, metadata)
                    VALUES (%s, %s, %s)
                    RETURNING id
                    """,
                    (
                        SIM_ORG_SLUG,
                        SIM_ORG_NAME,
                        Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                    ),
                )
                row = await cur.fetchone()
                assert row is not None
                org_id = row[0]

            # Stamp Leg 2 + Leg 3 templates on org.metadata so
            # customer_activation.create_customer_activation can read them.
            await cur.execute(
                """
                UPDATE business.organizations
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    Jsonb(
                        {
                            "leg2_sequence_template": _LEG2_SEQUENCE_TEMPLATE,
                            "leg3_intro_template": _LEG3_INTRO_TEMPLATE,
                            "operator_first_name": "Ben",
                            "operator_calendly_url": "https://cal.com/ben/15min-sim",
                            "operator_signature": "— Ben",
                            "operator_brand_name": "Licensed To Haul (sim)",
                            SIM_TAG_KEY: SIM_TAG_VALUE,
                        }
                    ),
                    str(org_id),
                ),
            )
        await conn.commit()
    return org_id


async def _upsert_brand(org_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.brands
                WHERE organization_id = %s AND name = %s
                """,
                (str(org_id), SIM_BRAND_NAME),
            )
            row = await cur.fetchone()
            if row:
                return row[0]
            await cur.execute(
                """
                INSERT INTO business.brands (organization_id, name)
                VALUES (%s, %s)
                RETURNING id
                """,
                (str(org_id), SIM_BRAND_NAME),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_partner(org_id: UUID) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.demand_side_partners
                WHERE organization_id = %s AND name = %s
                """,
                (str(org_id), SIM_PARTNER_NAME),
            )
            row = await cur.fetchone()
            if row:
                return row[0]
            await cur.execute(
                """
                INSERT INTO business.demand_side_partners
                    (organization_id, name, domain, primary_contact_name,
                     primary_contact_email, metadata)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    SIM_PARTNER_NAME,
                    "acmelog-sim.example",
                    "Barry Acme",
                    "barry@acmelog-sim.example",
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_contract(partner_id: UUID) -> UUID:
    amount = SIM_PROPOSED_TRANSFER_COUNT * SIM_PRICE_PER_TRANSFER_CENTS
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.partner_contracts
                WHERE partner_id = %s
                  AND pricing_model = 'per_lead'
                  AND amount_cents = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (str(partner_id), amount),
            )
            row = await cur.fetchone()
            if row:
                cid = row[0]
                # Force active state.
                await cur.execute(
                    """
                    UPDATE business.partner_contracts
                    SET status = 'active',
                        starts_at = COALESCE(starts_at, NOW()),
                        ends_at = COALESCE(ends_at, NOW() + (duration_days || ' days')::interval),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (str(cid),),
                )
                await conn.commit()
                return cid

            await cur.execute(
                """
                INSERT INTO business.partner_contracts
                    (partner_id, pricing_model, amount_cents, duration_days,
                     status, starts_at, ends_at)
                VALUES (%s, 'per_lead', %s, %s, 'active',
                        NOW(), NOW() + (%s || ' days')::interval)
                RETURNING id
                """,
                (
                    str(partner_id),
                    amount,
                    SIM_WINDOW_DAYS,
                    SIM_WINDOW_DAYS,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_proposal(
    *,
    org_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    contract_id: UUID,
) -> UUID:
    """Mint a status='paid' proposal pointing at our sim audience id."""
    import secrets

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.proposals
                WHERE partner_contract_id = %s
                  AND proposed_data_engine_audience_id = %s
                LIMIT 1
                """,
                (str(contract_id), str(SIM_AUDIENCE_ID)),
            )
            row = await cur.fetchone()
            if row:
                pid = row[0]
                await cur.execute(
                    """
                    UPDATE business.proposals
                    SET status = 'paid',
                        paid_at = COALESCE(paid_at, NOW()),
                        paid_amount_cents = COALESCE(paid_amount_cents, %s),
                        final_data_engine_audience_id = COALESCE(
                            final_data_engine_audience_id, %s
                        ),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        SIM_PROPOSED_TRANSFER_COUNT * SIM_PRICE_PER_TRANSFER_CENTS,
                        str(SIM_AUDIENCE_ID),
                        str(pid),
                    ),
                )
                await conn.commit()
                return pid

            await cur.execute(
                """
                INSERT INTO business.proposals (
                    organization_id, brand_id, partner_id, partner_contract_id,
                    proposed_data_engine_audience_id,
                    final_data_engine_audience_id,
                    proposed_transfer_count, proposed_price_per_transfer_cents,
                    proposed_window_days,
                    status, public_token,
                    prospect_company_name, prospect_contact_name, prospect_contact_email,
                    paid_at, paid_amount_cents,
                    metadata
                )
                VALUES (%s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s,
                        'paid', %s,
                        %s, %s, %s,
                        NOW(), %s,
                        %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    str(partner_id),
                    str(contract_id),
                    str(SIM_AUDIENCE_ID),
                    str(SIM_AUDIENCE_ID),
                    SIM_PROPOSED_TRANSFER_COUNT,
                    SIM_PRICE_PER_TRANSFER_CENTS,
                    SIM_WINDOW_DAYS,
                    secrets.token_urlsafe(32),
                    SIM_PARTNER_NAME,
                    "Barry Acme",
                    "barry@acmelog-sim.example",
                    SIM_PROPOSED_TRANSFER_COUNT * SIM_PRICE_PER_TRANSFER_CENTS,
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_audience_reservation(
    *, org_id: UUID, partner_id: UUID
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.org_audience_reservations
                WHERE organization_id = %s
                  AND data_engine_audience_id = %s
                LIMIT 1
                """,
                (str(org_id), str(SIM_AUDIENCE_ID)),
            )
            row = await cur.fetchone()
            if row:
                return row[0]

            await cur.execute(
                """
                INSERT INTO business.org_audience_reservations (
                    organization_id, data_engine_audience_id,
                    source_template_slug, source_template_id,
                    audience_name, status, reserved_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, 'active', NOW(), %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    str(SIM_AUDIENCE_ID),
                    SIM_AUDIENCE_TEMPLATE_SLUG,
                    str(uuid4()),  # fake template_id
                    "Sim active freight brokerages",
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _create_or_reuse_activation(
    *,
    org_id: UUID,
    brand_id: UUID,
    partner_id: UUID,
    contract_id: UUID,
) -> dict[str, Any]:
    """If the activation already exists, find it; else create via the
    real customer_activation pathway."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.gtm_initiatives
                WHERE organization_id = %s
                  AND partner_id = %s
                  AND partner_contract_id = %s
                  AND data_engine_audience_id = %s
                  AND parent_initiative_id IS NULL
                  AND (metadata->>'leg')::int = 2
                ORDER BY created_at DESC LIMIT 1
                """,
                (
                    str(org_id),
                    str(partner_id),
                    str(contract_id),
                    str(SIM_AUDIENCE_ID),
                ),
            )
            row = await cur.fetchone()
            if row:
                leg2_id = row[0]
                await cur.execute(
                    """
                    SELECT id FROM business.gtm_initiatives
                    WHERE parent_initiative_id = %s
                      AND (metadata->>'leg')::int = 3
                    LIMIT 1
                    """,
                    (str(leg2_id),),
                )
                leg3_row = await cur.fetchone()
                leg3_id = leg3_row[0] if leg3_row else None

                await cur.execute(
                    """
                    SELECT cc.id FROM business.channel_campaigns cc
                    WHERE cc.initiative_id = %s
                    LIMIT 1
                    """,
                    (str(leg2_id),),
                )
                cc_row = await cur.fetchone()
                leg2_cc_id = cc_row[0] if cc_row else None

                return {
                    "leg2_initiative_id": leg2_id,
                    "leg3_initiative_id": leg3_id,
                    "leg2_channel_campaign_id": leg2_cc_id,
                }

    return await customer_activation.create_customer_activation(
        organization_id=org_id,
        brand_id=brand_id,
        partner_id=partner_id,
        partner_contract_id=contract_id,
        data_engine_audience_id=SIM_AUDIENCE_ID,
        name="Cluster 3 sim activation",
        metadata={SIM_TAG_KEY: SIM_TAG_VALUE},
    )


async def _activate_leg2(leg2_initiative_id: UUID) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET status = 'active', updated_at = NOW()
                WHERE id = %s AND status != 'active'
                """,
                (str(leg2_initiative_id),),
            )
            await cur.execute(
                """
                UPDATE business.campaigns
                SET status = 'active', updated_at = NOW()
                WHERE initiative_id = %s
                """,
                (str(leg2_initiative_id),),
            )
            await cur.execute(
                """
                UPDATE business.channel_campaign_steps step
                SET status = 'sent', updated_at = NOW()
                FROM business.channel_campaigns cc
                WHERE step.channel_campaign_id = cc.id
                  AND cc.initiative_id = %s
                """,
                (str(leg2_initiative_id),),
            )
        await conn.commit()


async def _seed_recipients_and_messages(
    *,
    org_id: UUID,
    brand_id: UUID,
    leg2_channel_campaign_id: UUID,
) -> list[dict[str, Any]]:
    """Create N recipients, link them to the Leg 2 first step, and create
    matching email_messages in 'sent' status — these are the supply-side
    messages that 'went out'."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, campaign_id FROM business.channel_campaign_steps
                WHERE channel_campaign_id = %s
                ORDER BY step_order ASC LIMIT 1
                """,
                (str(leg2_channel_campaign_id),),
            )
            row = await cur.fetchone()
    if row is None:
        _abort("seed_recipients: no Leg 2 step")
    leg2_step_id, leg2_campaign_id = row

    seeded: list[dict[str, Any]] = []
    for i in range(SIM_RECIPIENT_COUNT):
        recipient_id = await _upsert_recipient(
            org_id=org_id, idx=i
        )
        await _upsert_step_member(
            step_id=leg2_step_id,
            org_id=org_id,
            recipient_id=recipient_id,
            status="sent",
        )
        em_id = await _upsert_sent_email_message(
            org_id=org_id,
            brand_id=brand_id,
            campaign_id=leg2_campaign_id,
            channel_campaign_id=leg2_channel_campaign_id,
            step_id=leg2_step_id,
            recipient_id=recipient_id,
            idx=i,
        )
        seeded.append(
            {
                "idx": i,
                "recipient_id": str(recipient_id),
                "email_message_id": str(em_id),
            }
        )
    return seeded


async def _upsert_recipient(*, org_id: UUID, idx: int) -> UUID:
    external_id = f"sim-lead-{idx:02d}"
    display = f"Lead {idx:02d} Brokerage"
    email = f"lead{idx}+sim@simcarriers.example"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.recipients (
                    organization_id, recipient_type, external_source, external_id,
                    display_name, email, metadata
                )
                VALUES (%s, 'business', 'cluster3_sim', %s, %s, %s, %s)
                ON CONFLICT (organization_id, external_source, external_id)
                  DO UPDATE SET display_name = EXCLUDED.display_name,
                                email = EXCLUDED.email,
                                metadata = EXCLUDED.metadata,
                                updated_at = NOW()
                RETURNING id
                """,
                (
                    str(org_id),
                    external_id,
                    display,
                    email,
                    Jsonb(
                        {
                            SIM_TAG_KEY: SIM_TAG_VALUE,
                            "first_name": display.split()[0] + str(idx),
                            "last_name": "Brokerage",
                        }
                    ),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_step_member(
    *, step_id: UUID, org_id: UUID, recipient_id: UUID, status: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.channel_campaign_step_recipients
                    (channel_campaign_step_id, recipient_id, organization_id,
                     status, processed_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (channel_campaign_step_id, recipient_id)
                  DO UPDATE SET status = EXCLUDED.status,
                                updated_at = NOW()
                """,
                (str(step_id), str(recipient_id), str(org_id), status),
            )
        await conn.commit()


async def _upsert_sent_email_message(
    *,
    org_id: UUID,
    brand_id: UUID,
    campaign_id: UUID,
    channel_campaign_id: UUID,
    step_id: UUID,
    recipient_id: UUID,
    idx: int,
) -> UUID:
    fake_eb_scheduled = 9_000_000 + idx
    fake_workspace = "sim-workspace"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages (
                    organization_id, brand_id, campaign_id, channel_campaign_id,
                    channel_campaign_step_id, recipient_id,
                    eb_workspace_id, eb_scheduled_email_id,
                    subject_snapshot, body_snapshot, status, sent_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, 'sent', NOW(), %s)
                ON CONFLICT (eb_workspace_id, eb_scheduled_email_id)
                  WHERE eb_scheduled_email_id IS NOT NULL
                  DO UPDATE SET status = 'sent',
                                updated_at = NOW()
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    str(campaign_id),
                    str(channel_campaign_id),
                    str(step_id),
                    str(recipient_id),
                    fake_workspace,
                    fake_eb_scheduled,
                    f"Lead {idx:02d}, quick intro request",
                    "Hi — quick note from Ben...",
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE, "sim_idx": idx}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _seed_source_artifacts(*, partner_id: UUID) -> None:
    """Optional: stamp partner_research + audience_context artifacts
    so the intro composer's bundle has something to read."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.partner_research_artifacts
                    (partner_id, research_md, generated_by, model, metadata)
                VALUES (%s, %s, 'sim_seed', 'sim-stub', %s)
                ON CONFLICT (partner_id) DO UPDATE
                  SET research_md = EXCLUDED.research_md,
                      updated_at = NOW()
                """,
                (
                    str(partner_id),
                    (
                        "Acme Logistics Partners is an active-freight broker "
                        "buyer focused on small fleets in the 10–50 power-unit "
                        "band, especially newly-authorized carriers in their "
                        "first 12 months. Their differentiator: same-day "
                        "lane quoting and direct shipper relationships in the "
                        "midwest produce corridor. Founder Barry Acme is "
                        "ex-CHR; the company moved $42M GMV last year."
                    ),
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
            await cur.execute(
                """
                INSERT INTO business.audience_context_artifacts
                    (data_engine_audience_id, audience_template_slug, context_md,
                     generated_by, model, metadata)
                VALUES (%s, %s, %s, 'sim_seed', 'sim-stub', %s)
                ON CONFLICT (data_engine_audience_id) DO UPDATE
                  SET context_md = EXCLUDED.context_md,
                      updated_at = NOW()
                """,
                (
                    str(SIM_AUDIENCE_ID),
                    SIM_AUDIENCE_TEMPLATE_SLUG,
                    (
                        "Active freight brokerages with 10–50 power units, "
                        "operating-authority granted within the last 18 "
                        "months, no insurance lapse on file. Common pain: "
                        "uneven lane volume month-to-month; dependency on "
                        "load boards; thin margins on backhauls."
                    ),
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
        await conn.commit()


async def _create_c1_initiative(
    *, org_id: UUID, brand_id: UUID
) -> UUID:
    """Create a self_prospecting initiative + campaign + channel_campaign
    + one channel_campaign_step. Bypasses the real
    self_prospecting.create_self_prospecting_initiative because that
    requires a DEX call to mint the audience spec — we use a fixed sim
    audience id instead."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Existing initiative for this audience?
            await cur.execute(
                """
                SELECT id FROM business.gtm_initiatives
                WHERE organization_id = %s
                  AND kind = 'self_prospecting'
                  AND data_engine_audience_id = %s
                LIMIT 1
                """,
                (str(org_id), str(SIM_C1_AUDIENCE_ID)),
            )
            row = await cur.fetchone()
            if row:
                return row[0]

            # Mint initiative
            await cur.execute(
                """
                INSERT INTO business.gtm_initiatives (
                    organization_id, brand_id, partner_id, partner_contract_id,
                    data_engine_audience_id, kind, status, metadata
                )
                VALUES (%s, %s, NULL, NULL, %s, 'self_prospecting',
                        'active', %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    str(SIM_C1_AUDIENCE_ID),
                    Jsonb(
                        {
                            "name": SIM_C1_INITIATIVE_NAME,
                            "audience_template_slug": SIM_C1_AUDIENCE_TEMPLATE_SLUG,
                            SIM_TAG_KEY: SIM_TAG_VALUE,
                        }
                    ),
                ),
            )
            initiative_id = (await cur.fetchone())[0]

            # Mint campaign
            await cur.execute(
                """
                INSERT INTO business.campaigns
                    (organization_id, brand_id, name, status, initiative_id, metadata)
                VALUES (%s, %s, %s, 'active', %s, %s)
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    SIM_C1_INITIATIVE_NAME,
                    str(initiative_id),
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                ),
            )
            campaign_id = (await cur.fetchone())[0]

            # Mint channel_campaign
            await cur.execute(
                """
                INSERT INTO business.channel_campaigns (
                    campaign_id, organization_id, brand_id, name, channel,
                    provider, audience_spec_id, status, start_offset_days,
                    schedule_config, provider_config, metadata, initiative_id
                )
                VALUES (%s, %s, %s, %s, 'email', 'emailbison', %s,
                        'scheduled', 0, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(campaign_id),
                    str(org_id),
                    str(brand_id),
                    SIM_C1_INITIATIVE_NAME,
                    str(SIM_C1_AUDIENCE_ID),
                    Jsonb({}),
                    Jsonb({}),
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE, "leg": 1}),
                    str(initiative_id),
                ),
            )
            cc_id = (await cur.fetchone())[0]

            # Step 1: hand-authored manual content (operator-style).
            await cur.execute(
                """
                INSERT INTO business.channel_campaign_steps (
                    channel_campaign_id, campaign_id, organization_id, brand_id,
                    step_order, name, delay_days_from_previous,
                    content_mode, channel_specific_config, metadata,
                    status, external_provider_id
                )
                VALUES (%s, %s, %s, %s, 1, %s, 0, 'manual', %s, %s,
                        'sent', %s)
                """,
                (
                    str(cc_id),
                    str(campaign_id),
                    str(org_id),
                    str(brand_id),
                    "Cluster 1 — broker outreach 1",
                    Jsonb(
                        {
                            "subject": "{first_name}, quick intro",
                            "body_text": (
                                "Hi {first_name},\n\nQuick note from Ben at "
                                "Licensed To Haul. We deliver curated lead "
                                "transfers to active freight brokerages on a "
                                "prepaid basis. Worth a 15-min call?\n\n— Ben"
                            ),
                        }
                    ),
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE}),
                    "9999998",  # fake EB campaign id
                ),
            )
        await conn.commit()
    return initiative_id


async def _seed_c1_recipients_and_messages(
    *, org_id: UUID, brand_id: UUID, initiative_id: UUID
) -> list[dict[str, Any]]:
    """Seed N demand-side prospect recipients + sent email_messages
    against the Cluster 1 step."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT step.id, step.campaign_id, step.channel_campaign_id
                FROM business.channel_campaign_steps step
                JOIN business.channel_campaigns cc
                  ON cc.id = step.channel_campaign_id
                WHERE cc.initiative_id = %s
                ORDER BY step.step_order ASC LIMIT 1
                """,
                (str(initiative_id),),
            )
            row = await cur.fetchone()
    if not row:
        _abort("c1 seed: no step")
    step_id, campaign_id, cc_id = row

    seeded: list[dict[str, Any]] = []
    for i, (broker_name, broker_email) in enumerate(SIM_C1_BROKER_NAMES[:SIM_C1_RECIPIENT_COUNT]):
        first_name = broker_name.split()[0]
        rid = await _upsert_c1_recipient(
            org_id=org_id,
            idx=i,
            broker_name=broker_name,
            broker_email=broker_email,
            first_name=first_name,
        )
        await _upsert_step_member(
            step_id=step_id,
            org_id=org_id,
            recipient_id=rid,
            status="sent",
        )
        em_id = await _upsert_c1_sent_email_message(
            org_id=org_id,
            brand_id=brand_id,
            campaign_id=campaign_id,
            channel_campaign_id=cc_id,
            step_id=step_id,
            recipient_id=rid,
            broker_name=broker_name,
            idx=i,
        )
        seeded.append({"idx": i, "recipient_id": str(rid), "email_message_id": str(em_id)})
    return seeded


async def _upsert_c1_recipient(
    *, org_id: UUID, idx: int, broker_name: str, broker_email: str, first_name: str
) -> UUID:
    external_id = f"sim-c1-broker-{idx:02d}"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.recipients (
                    organization_id, recipient_type, external_source, external_id,
                    display_name, email, metadata
                )
                VALUES (%s, 'business', 'cluster1_sim', %s, %s, %s, %s)
                ON CONFLICT (organization_id, external_source, external_id)
                  DO UPDATE SET display_name = EXCLUDED.display_name,
                                email = EXCLUDED.email,
                                metadata = EXCLUDED.metadata,
                                updated_at = NOW()
                RETURNING id
                """,
                (
                    str(org_id),
                    external_id,
                    broker_name,
                    broker_email,
                    Jsonb(
                        {
                            SIM_TAG_KEY: SIM_TAG_VALUE,
                            "first_name": first_name,
                            "broker": True,
                        }
                    ),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def _upsert_c1_sent_email_message(
    *,
    org_id: UUID,
    brand_id: UUID,
    campaign_id: UUID,
    channel_campaign_id: UUID,
    step_id: UUID,
    recipient_id: UUID,
    broker_name: str,
    idx: int,
) -> UUID:
    fake_eb_scheduled = 8_500_000 + idx
    fake_workspace = "sim-workspace-c1"
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_messages (
                    organization_id, brand_id, campaign_id, channel_campaign_id,
                    channel_campaign_step_id, recipient_id,
                    eb_workspace_id, eb_scheduled_email_id,
                    subject_snapshot, body_snapshot, status, sent_at, metadata
                )
                VALUES (%s, %s, %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, 'sent', NOW(), %s)
                ON CONFLICT (eb_workspace_id, eb_scheduled_email_id)
                  WHERE eb_scheduled_email_id IS NOT NULL
                  DO UPDATE SET status = 'sent', updated_at = NOW()
                RETURNING id
                """,
                (
                    str(org_id),
                    str(brand_id),
                    str(campaign_id),
                    str(channel_campaign_id),
                    str(step_id),
                    str(recipient_id),
                    fake_workspace,
                    fake_eb_scheduled,
                    f"{broker_name.split()[0]}, quick intro",
                    "Hi — quick note from Ben at Licensed To Haul...",
                    Jsonb({SIM_TAG_KEY: SIM_TAG_VALUE, "sim_idx": idx, "cluster": "cluster_1"}),
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    assert row is not None
    return row[0]


async def cmd_seed() -> dict[str, Any]:
    log("seed: starting")
    org_id = await _upsert_org()
    log(f"seed: org_id={org_id}")
    brand_id = await _upsert_brand(org_id)
    log(f"seed: brand_id={brand_id}")
    partner_id = await _upsert_partner(org_id)
    log(f"seed: partner_id={partner_id}")
    contract_id = await _upsert_contract(partner_id)
    log(f"seed: contract_id={contract_id}")
    await _upsert_audience_reservation(org_id=org_id, partner_id=partner_id)
    proposal_id = await _upsert_proposal(
        org_id=org_id,
        brand_id=brand_id,
        partner_id=partner_id,
        contract_id=contract_id,
    )
    log(f"seed: proposal_id={proposal_id}")
    activation = await _create_or_reuse_activation(
        org_id=org_id,
        brand_id=brand_id,
        partner_id=partner_id,
        contract_id=contract_id,
    )
    leg2_initiative_id = activation["leg2_initiative_id"]
    log(f"seed: leg2={leg2_initiative_id} leg3={activation.get('leg3_initiative_id')}")
    await _activate_leg2(UUID(str(leg2_initiative_id)))
    seeded = await _seed_recipients_and_messages(
        org_id=org_id,
        brand_id=brand_id,
        leg2_channel_campaign_id=UUID(str(activation["leg2_channel_campaign_id"])),
    )
    log(f"seed: seeded {len(seeded)} recipients/email_messages")
    await _seed_source_artifacts(partner_id=partner_id)
    log("seed: source artifacts written")

    # Cluster 1 fixture (self_prospecting demand-side outreach).
    c1_initiative_id = await _create_c1_initiative(org_id=org_id, brand_id=brand_id)
    log(f"seed: c1_initiative={c1_initiative_id}")
    c1_seeded = await _seed_c1_recipients_and_messages(
        org_id=org_id, brand_id=brand_id, initiative_id=c1_initiative_id
    )
    log(f"seed: seeded {len(c1_seeded)} cluster-1 prospects")

    return {
        "org_id": str(org_id),
        "brand_id": str(brand_id),
        "partner_id": str(partner_id),
        "contract_id": str(contract_id),
        "proposal_id": str(proposal_id),
        "leg2_initiative_id": str(leg2_initiative_id),
        "leg3_initiative_id": str(activation.get("leg3_initiative_id")),
        "seeded_recipients": seeded,
        "c1_initiative_id": str(c1_initiative_id),
        "c1_seeded": c1_seeded,
    }


# ── Simulate phase ────────────────────────────────────────────────────────


async def _simulate_inbound_event(
    *,
    email_message_id: UUID,
    scenario: dict[str, Any],
    eb_workspace: str = "sim-workspace",
    eb_reply_id_base: int = 8_000_000,
) -> None:
    """Stamp a synthetic 'replied' row into email_message_events so the
    orchestrator's reply-text resolver finds it via the events scan."""
    _ = eb_workspace  # surfaced via the `from_email_address` line below
    payload = {
        "event": {"type": "lead_replied"},
        "data": {
            "reply": {
                "id": eb_reply_id_base + scenario["recipient_idx"],
                "subject": scenario["subject"],
                "text_body": scenario["body"],
                "from_email_address": f"lead{scenario['recipient_idx']}@simcarriers.example",
                "interested": scenario["expected"] == "positive",
                "automated_reply": scenario["expected"] == "auto_reply",
                "folder": "Inbox",
                "type": "Replied",
            }
        },
    }
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.email_message_events
                    (email_message_id, event_type, raw_event_name, occurred_at, payload)
                VALUES (%s, 'replied', 'lead_replied', NOW(), %s)
                ON CONFLICT (email_message_id, raw_event_name, occurred_at)
                  DO NOTHING
                """,
                (str(email_message_id), Jsonb(payload)),
            )
            await cur.execute(
                """
                UPDATE business.email_messages
                SET status = 'replied',
                    replied_at = COALESCE(replied_at, NOW()),
                    updated_at = NOW()
                WHERE id = %s AND status NOT IN ('replied','bounced','unsubscribed')
                """,
                (str(email_message_id),),
            )
        await conn.commit()


async def cmd_simulate(seed_result: dict[str, Any] | None = None) -> dict[str, Any]:
    log("simulate: starting")
    if seed_result is None:
        org_id_row = await _fetch_sim_org_id()
        if org_id_row is None:
            _abort("simulate: no sim org found — run --mode=seed first")
        seeded_c2 = await _fetch_seeded_email_messages(
            org_id_row, sim_marker_filter="cluster_2_or_legacy"
        )
        seeded_c1 = await _fetch_seeded_email_messages(
            org_id_row, sim_marker_filter="cluster_1"
        )
    else:
        seeded_c2 = seed_result["seeded_recipients"]
        seeded_c1 = seed_result.get("c1_seeded", [])

    # Cluster 2 → Cluster 3 dispatch path.
    c2_by_idx = {s["idx"]: s for s in seeded_c2 if s.get("idx") is not None}
    c2_results: list[dict[str, Any]] = []
    for scenario in REPLY_SCENARIOS:
        idx = scenario["recipient_idx"]
        if idx not in c2_by_idx:
            continue
        em_id = UUID(c2_by_idx[idx]["email_message_id"])
        await _simulate_inbound_event(
            email_message_id=em_id, scenario=scenario, eb_workspace="sim-workspace"
        )
        out = await inbox_orchestrator.handle_inbound_reply(
            email_message_id=em_id,
            eb_reply_id=8_000_000 + idx,
            eb_workspace_id="sim-workspace",
            classifier_mode="stub",
            composer_mode="stub",
            verdict_mode="stub",
        )
        log(
            f"simulate[c2]: idx={idx} expected={scenario['expected']} "
            f"actual={out.get('classification')} status={out.get('status')}"
        )
        c2_results.append(
            {"cluster": "c2", "idx": idx, "expected": scenario["expected"], "result": out}
        )

    # Cluster 1 → auto-reply path.
    c1_by_idx = {s["idx"]: s for s in seeded_c1 if s.get("idx") is not None}
    c1_results: list[dict[str, Any]] = []
    for scenario in REPLY_SCENARIOS_C1:
        idx = scenario["recipient_idx"]
        if idx not in c1_by_idx:
            log(f"simulate[c1]: scenario idx={idx} no seed row")
            continue
        em_id = UUID(c1_by_idx[idx]["email_message_id"])
        await _simulate_inbound_event(
            email_message_id=em_id,
            scenario=scenario,
            eb_workspace="sim-workspace-c1",
            eb_reply_id_base=7_000_000,
        )
        out = await inbox_orchestrator.handle_inbound_reply(
            email_message_id=em_id,
            eb_reply_id=7_000_000 + idx,
            eb_workspace_id="sim-workspace-c1",
            classifier_mode="stub",
            composer_mode="stub",
            verdict_mode="stub",
        )
        log(
            f"simulate[c1]: idx={idx} expected={scenario['expected']} "
            f"actual={out.get('classification')} status={out.get('status')}"
        )
        c1_results.append(
            {"cluster": "c1", "idx": idx, "expected": scenario["expected"], "result": out}
        )

    return {
        "simulated": len(c2_results) + len(c1_results),
        "cluster_2_results": c2_results,
        "cluster_1_results": c1_results,
    }


async def _fetch_sim_org_id() -> UUID | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.organizations WHERE slug = %s",
                (SIM_ORG_SLUG,),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def _fetch_seeded_email_messages(
    org_id: UUID, *, sim_marker_filter: str = "cluster_2_or_legacy"
) -> list[dict[str, Any]]:
    """Fetch seeded email_messages.

    sim_marker_filter:
      'cluster_2_or_legacy' — em.metadata.cluster != 'cluster_1'
      'cluster_1'           — em.metadata.cluster == 'cluster_1'
    """
    if sim_marker_filter == "cluster_1":
        cluster_clause = "(em.metadata->>'cluster') = 'cluster_1'"
    else:
        cluster_clause = "((em.metadata->>'cluster') IS NULL OR (em.metadata->>'cluster') != 'cluster_1')"

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT em.id, em.metadata, r.id, r.external_id
                FROM business.email_messages em
                JOIN business.recipients r ON r.id = em.recipient_id
                WHERE em.organization_id = %s
                  AND em.metadata->>%s = %s
                  AND {cluster_clause}
                ORDER BY (em.metadata->>'sim_idx')::int ASC
                """,
                (str(org_id), SIM_TAG_KEY, SIM_TAG_VALUE),
            )
            rows = await cur.fetchall()
    out = []
    for em_id, em_meta, rid, ext_id in rows or []:
        idx = (em_meta or {}).get("sim_idx") if isinstance(em_meta, dict) else None
        if idx is None and ext_id:
            for prefix in ("sim-lead-", "sim-c1-broker-"):
                if ext_id.startswith(prefix):
                    try:
                        idx = int(ext_id.rsplit("-", 1)[1])
                    except (ValueError, IndexError):
                        idx = None
                    break
        out.append(
            {"idx": idx, "recipient_id": str(rid), "email_message_id": str(em_id)}
        )
    return out


# ── Teardown phase ────────────────────────────────────────────────────────


async def cmd_teardown() -> dict[str, Any]:
    log("teardown: starting")
    org_id = await _fetch_sim_org_id()
    if org_id is None:
        log("teardown: no sim org — nothing to do")
        return {"deleted_org": None}

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Walk down the dependency tree explicitly so we don't trip
            # over RESTRICT FKs.
            tables_in_order = [
                "business.lead_transfers",
                "business.email_reply_classifications",  # cascades from email_messages
                "business.email_message_events",
                "business.email_messages",
                "business.channel_campaign_step_recipients",
                "business.channel_campaign_steps",
                "business.channel_campaigns",
                "business.campaigns",
                "business.gtm_initiatives",
                "business.proposals",
                "business.partner_research_artifacts",
                "business.audience_context_artifacts",
                "business.org_audience_reservations",
                "business.partner_contracts",
                "business.demand_side_partners",
                "business.recipients",
                "business.brands",
            ]
            for t in tables_in_order:
                if t == "business.lead_transfers":
                    await cur.execute(
                        f"DELETE FROM {t} WHERE organization_id = %s",
                        (str(org_id),),
                    )
                elif t == "business.email_reply_classifications":
                    # Will cascade from email_messages delete; explicit pass
                    # is a no-op safety net.
                    await cur.execute(
                        f"""
                        DELETE FROM {t} erc
                        USING business.email_messages em
                        WHERE erc.email_message_id = em.id
                          AND em.organization_id = %s
                        """,
                        (str(org_id),),
                    )
                elif t == "business.email_message_events":
                    await cur.execute(
                        f"""
                        DELETE FROM {t} eme
                        USING business.email_messages em
                        WHERE eme.email_message_id = em.id
                          AND em.organization_id = %s
                        """,
                        (str(org_id),),
                    )
                elif t == "business.partner_research_artifacts":
                    await cur.execute(
                        f"""
                        DELETE FROM {t} pra
                        USING business.demand_side_partners p
                        WHERE pra.partner_id = p.id
                          AND p.organization_id = %s
                        """,
                        (str(org_id),),
                    )
                elif t == "business.audience_context_artifacts":
                    await cur.execute(
                        f"DELETE FROM {t} WHERE data_engine_audience_id = %s",
                        (str(SIM_AUDIENCE_ID),),
                    )
                elif t == "business.partner_contracts":
                    await cur.execute(
                        f"""
                        DELETE FROM {t} pc
                        USING business.demand_side_partners p
                        WHERE pc.partner_id = p.id
                          AND p.organization_id = %s
                        """,
                        (str(org_id),),
                    )
                else:
                    await cur.execute(
                        f"DELETE FROM {t} WHERE organization_id = %s",
                        (str(org_id),),
                    )

            # Finally the org itself.
            await cur.execute(
                "DELETE FROM business.organizations WHERE id = %s", (str(org_id),)
            )
        await conn.commit()
    log(f"teardown: deleted org {org_id}")
    return {"deleted_org": str(org_id)}


# ── Main ──────────────────────────────────────────────────────────────────


async def amain(mode: str) -> None:
    await init_pool()
    try:
        if mode == "seed":
            res = await cmd_seed()
        elif mode == "simulate":
            res = await cmd_simulate()
        elif mode == "teardown":
            res = await cmd_teardown()
        elif mode == "full":
            seeded = await cmd_seed()
            sim = await cmd_simulate(seeded)
            td = await cmd_teardown()
            res = {"seed": seeded, "simulate": sim, "teardown": td}
        else:
            _abort(f"unknown mode: {mode}")
        print(json.dumps(res, default=str, indent=2))
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        required=True,
        choices=["seed", "simulate", "teardown", "full"],
    )
    args = parser.parse_args()
    asyncio.run(amain(args.mode))


if __name__ == "__main__":
    main()
