"""Proposals — prospect-facing agreement → Stripe payment → Cluster 2 trigger.

Lifecycle:

  1. Operator creates a proposal (admin). Service mints a
     ``business.demand_side_partners`` row + a ``business.partner_contracts``
     row in 'draft', then writes the proposal pointing at both. A unique
     ``public_token`` is generated for the prospect-facing URL.

  2. Prospect lands on partner-platform ``/proposal/<token>``. The page
     loads the proposal via ``get_by_token``. If the prospect chooses to
     refine the audience via the composer, ``set_audience`` flips
     ``final_data_engine_audience_id`` to the chosen spec id.

  3. Prospect clicks pay → ``initiate_checkout`` mints a Stripe Checkout
     session, persists ``stripe_checkout_session_id``, flips status to
     ``checkout_initiated``, and returns the redirect URL.

  4. Stripe ``checkout.session.completed`` webhook calls
     ``mark_paid_and_instantiate``: flips proposal to 'paid', flips
     partner_contracts to 'active' with starts_at/ends_at, and invokes
     ``ca_svc.instantiate_for_payment`` to fire Cluster 2.

The proposal row is the canonical lifecycle anchor — every state change
stamps a row column or audit field, so we can answer "where is this
prospect in the funnel?" without joining four tables.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection
from app.services import customer_activation as ca_svc
from app.services import stripe_client

logger = logging.getLogger(__name__)


class ProposalValidationError(Exception):
    """Raised when caller-provided data is invalid (bad amounts, etc)."""


class ProposalNotFound(Exception):
    """Raised when a proposal lookup misses."""


class ProposalStateError(Exception):
    """Raised when a proposal is in the wrong state for the requested op."""


# ---------------------------------------------------------------------------
# Token generation. 32 bytes URL-safe = ~43 chars. Two reasons not to use
# the proposal UUID directly: (1) UUIDs leak ordering / sequencing, (2)
# we want the URL to be unguessable even if a prospect's id is known.
# ---------------------------------------------------------------------------


def _new_public_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Create — mints partner + contract + proposal in a single transaction.
# ---------------------------------------------------------------------------


async def create_proposal(
    *,
    organization_id: UUID,
    brand_id: UUID,
    prospect_company_name: str,
    prospect_contact_email: str | None,
    prospect_contact_name: str | None,
    proposed_data_engine_audience_id: UUID,
    proposed_transfer_count: int,
    proposed_price_per_transfer_cents: int,
    proposed_window_days: int,
    created_by_user_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint partner + contract + proposal in one transaction.

    The org's ``leg2_sequence_template`` must be authored before payment
    can fire — we do not gate creation on it (operator may iterate on
    the template before the prospect pays), but the webhook handler
    surfaces a clear error if it's still missing at instantiation time.
    """
    if proposed_transfer_count <= 0:
        raise ProposalValidationError("proposed_transfer_count must be positive")
    if proposed_price_per_transfer_cents <= 0:
        raise ProposalValidationError("proposed_price_per_transfer_cents must be positive")
    if proposed_window_days <= 0:
        raise ProposalValidationError("proposed_window_days must be positive")
    if not prospect_company_name.strip():
        raise ProposalValidationError("prospect_company_name required")

    total_cents = proposed_transfer_count * proposed_price_per_transfer_cents
    public_token = _new_public_token()
    md = metadata or {}

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # 1. Mint demand-side partner row. Unique on (org, name) when
            # not deleted — re-using the same name within the same org
            # surfaces as a clean integrity error rather than silent
            # collision.
            await cur.execute(
                """
                INSERT INTO business.demand_side_partners (
                    organization_id, name, primary_contact_name,
                    primary_contact_email, metadata
                )
                VALUES (%s, %s, %s, %s, %s::jsonb)
                RETURNING id
                """,
                (
                    str(organization_id),
                    prospect_company_name.strip(),
                    prospect_contact_name,
                    prospect_contact_email,
                    "{}",
                ),
            )
            partner_id = (await cur.fetchone())[0]

            # 2. Mint contract row in 'draft' — flips to 'active' on payment.
            # pricing_model='per_lead'. amount_cents and
            # max_capital_outlay_cents = total_cents (locked-in commitment).
            await cur.execute(
                """
                INSERT INTO business.partner_contracts (
                    partner_id, pricing_model, amount_cents, duration_days,
                    max_capital_outlay_cents, status,
                    qualification_rules, terms_blob
                )
                VALUES (%s, 'per_lead', %s, %s, %s, 'draft', %s::jsonb, %s)
                RETURNING id
                """,
                (
                    str(partner_id),
                    total_cents,
                    proposed_window_days,
                    total_cents,
                    "{}",
                    None,
                ),
            )
            partner_contract_id = (await cur.fetchone())[0]

            # 3. Write the proposal.
            await cur.execute(
                """
                INSERT INTO business.proposals (
                    organization_id, brand_id, partner_id, partner_contract_id,
                    proposed_data_engine_audience_id,
                    proposed_transfer_count, proposed_price_per_transfer_cents,
                    proposed_window_days,
                    public_token,
                    prospect_company_name, prospect_contact_name, prospect_contact_email,
                    metadata, created_by_user_id
                )
                VALUES (
                    %s, %s, %s, %s,
                    %s,
                    %s, %s, %s,
                    %s,
                    %s, %s, %s,
                    %s::jsonb, %s
                )
                RETURNING id
                """,
                (
                    str(organization_id),
                    str(brand_id),
                    str(partner_id),
                    str(partner_contract_id),
                    str(proposed_data_engine_audience_id),
                    proposed_transfer_count,
                    proposed_price_per_transfer_cents,
                    proposed_window_days,
                    public_token,
                    prospect_company_name.strip(),
                    prospect_contact_name,
                    prospect_contact_email,
                    _json_dump(md),
                    str(created_by_user_id) if created_by_user_id else None,
                ),
            )
            proposal_id = (await cur.fetchone())[0]
        await conn.commit()

    return await get_proposal(proposal_id)


# ---------------------------------------------------------------------------
# Read — by id (admin) and by token (public, prospect-facing).
# ---------------------------------------------------------------------------


_SELECT_FIELDS = """
    p.id, p.organization_id, p.brand_id, p.partner_id, p.partner_contract_id,
    p.proposed_data_engine_audience_id, p.final_data_engine_audience_id,
    p.proposed_transfer_count, p.proposed_price_per_transfer_cents,
    p.proposed_window_days, p.proposed_total_cents,
    p.status, p.public_token,
    p.prospect_company_name, p.prospect_contact_name, p.prospect_contact_email,
    p.stripe_checkout_session_id, p.stripe_payment_intent_id,
    p.paid_at, p.paid_amount_cents,
    p.instantiated_leg2_initiative_id, p.instantiated_at,
    p.sent_at, p.viewed_at, p.expires_at,
    p.metadata, p.created_at, p.updated_at, p.created_by_user_id
"""


def _row_to_proposal(row: tuple) -> dict[str, Any]:
    keys = [
        "id", "organization_id", "brand_id", "partner_id", "partner_contract_id",
        "proposed_data_engine_audience_id", "final_data_engine_audience_id",
        "proposed_transfer_count", "proposed_price_per_transfer_cents",
        "proposed_window_days", "proposed_total_cents",
        "status", "public_token",
        "prospect_company_name", "prospect_contact_name", "prospect_contact_email",
        "stripe_checkout_session_id", "stripe_payment_intent_id",
        "paid_at", "paid_amount_cents",
        "instantiated_leg2_initiative_id", "instantiated_at",
        "sent_at", "viewed_at", "expires_at",
        "metadata", "created_at", "updated_at", "created_by_user_id",
    ]
    return dict(zip(keys, row, strict=True))


def _effective_audience_id(p: dict[str, Any]) -> UUID:
    return p["final_data_engine_audience_id"] or p["proposed_data_engine_audience_id"]


async def get_proposal(proposal_id: UUID | str) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SELECT_FIELDS} FROM business.proposals p WHERE p.id = %s",
                (str(proposal_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise ProposalNotFound(f"proposal {proposal_id} not found")
    return _row_to_proposal(row)


async def get_proposal_by_token(
    token: str, *, mark_viewed: bool = True
) -> dict[str, Any]:
    """Public lookup. When ``mark_viewed`` is true and the proposal is in
    a pre-view state ('draft' or 'sent'), stamps viewed_at and flips to
    'viewed'. Idempotent — re-views don't update.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_SELECT_FIELDS} FROM business.proposals p WHERE p.public_token = %s",
                (token,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ProposalNotFound("token not found")
            proposal = _row_to_proposal(row)
            if mark_viewed and proposal["status"] in ("draft", "sent"):
                await cur.execute(
                    """
                    UPDATE business.proposals
                    SET status = 'viewed',
                        viewed_at = COALESCE(viewed_at, NOW()),
                        updated_at = NOW()
                    WHERE id = %s AND status IN ('draft', 'sent')
                    RETURNING viewed_at, status
                    """,
                    (str(proposal["id"]),),
                )
                updated = await cur.fetchone()
                if updated:
                    proposal["viewed_at"] = updated[0]
                    proposal["status"] = updated[1]
                await conn.commit()
    return proposal


# ---------------------------------------------------------------------------
# Audience modification (pre-checkout).
# ---------------------------------------------------------------------------


async def set_proposal_audience(
    *,
    token: str,
    new_data_engine_audience_id: UUID,
) -> dict[str, Any]:
    """Prospect refined the audience via the composer; record the new
    spec id on the proposal. Refused once payment has been initiated.
    """
    proposal = await get_proposal_by_token(token, mark_viewed=False)
    if proposal["status"] in ("checkout_initiated", "paid", "expired", "cancelled"):
        raise ProposalStateError(
            f"cannot modify audience in state {proposal['status']!r}"
        )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.proposals
                SET final_data_engine_audience_id = %s,
                    status = CASE WHEN status IN ('draft','sent','viewed')
                                  THEN 'audience_confirmed'
                                  ELSE status END,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(new_data_engine_audience_id), str(proposal["id"])),
            )
        await conn.commit()
    return await get_proposal(proposal["id"])


# ---------------------------------------------------------------------------
# Initiate Stripe Checkout — public; uses token only.
# ---------------------------------------------------------------------------


async def initiate_checkout(*, token: str) -> dict[str, Any]:
    """Mint a Stripe Checkout session, persist the session id, flip
    status to 'checkout_initiated'. Returns ``{checkout_url, proposal}``.
    """
    proposal = await get_proposal_by_token(token, mark_viewed=False)
    if proposal["status"] in ("paid", "expired", "cancelled"):
        raise ProposalStateError(
            f"cannot initiate checkout in state {proposal['status']!r}"
        )

    base = settings.PARTNER_PLATFORM_BASE_URL.rstrip("/")
    success_url = f"{base}/proposal/{token}/success?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{base}/proposal/{token}"

    line_item_name = (
        f"{proposal['proposed_transfer_count']} lead transfers — "
        f"{proposal['prospect_company_name']}"
    )
    line_item_description = (
        f"${proposal['proposed_price_per_transfer_cents'] / 100:,.2f} per transfer · "
        f"{proposal['proposed_window_days']}-day delivery window"
    )

    session = await stripe_client.create_checkout_session(
        proposal_id=str(proposal["id"]),
        prospect_contact_email=proposal["prospect_contact_email"],
        line_item_name=line_item_name,
        line_item_description=line_item_description,
        amount_cents=int(proposal["proposed_total_cents"]),
        success_url=success_url,
        cancel_url=cancel_url,
    )

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.proposals
                SET stripe_checkout_session_id = %s,
                    status = 'checkout_initiated',
                    updated_at = NOW()
                WHERE id = %s
                """,
                (session["id"], str(proposal["id"])),
            )
        await conn.commit()

    return {
        "checkout_url": session.get("url"),
        "checkout_session_id": session["id"],
        "proposal": await get_proposal(proposal["id"]),
    }


# ---------------------------------------------------------------------------
# Mark paid + instantiate Cluster 2. Called by the Stripe webhook handler.
# ---------------------------------------------------------------------------


async def mark_paid_and_instantiate(
    *,
    stripe_checkout_session_id: str,
    paid_amount_cents: int,
    stripe_payment_intent_id: str | None,
) -> dict[str, Any]:
    """Webhook hot path: flip proposal to 'paid', activate the contract,
    and call ``ca_svc.instantiate_for_payment``. Idempotent — re-fired
    events with the same checkout session id no-op after the first
    successful pass (status guard).
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {_SELECT_FIELDS} FROM business.proposals p
                WHERE p.stripe_checkout_session_id = %s
                FOR UPDATE
                """,
                (stripe_checkout_session_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ProposalNotFound(
                    f"no proposal for checkout session {stripe_checkout_session_id}"
                )
            proposal = _row_to_proposal(row)

            if proposal["status"] == "paid":
                # Idempotent replay: nothing more to do.
                logger.info(
                    "stripe webhook replay for already-paid proposal %s",
                    proposal["id"],
                )
                await conn.commit()
                return {
                    "proposal": proposal,
                    "instantiated": False,
                    "reason": "already_paid",
                }

            # Flip proposal and contract atomically before invoking
            # downstream activation. If activation fails we keep the
            # 'paid' marker but surface the error to the webhook handler
            # so it can stamp processing_error on stripe_events.
            await cur.execute(
                """
                UPDATE business.proposals
                SET status = 'paid',
                    paid_at = NOW(),
                    paid_amount_cents = %s,
                    stripe_payment_intent_id = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (
                    paid_amount_cents,
                    stripe_payment_intent_id,
                    str(proposal["id"]),
                ),
            )
            await cur.execute(
                """
                UPDATE business.partner_contracts
                SET status = 'active',
                    starts_at = COALESCE(starts_at, NOW()),
                    ends_at = COALESCE(
                        ends_at, NOW() + (duration_days || ' days')::interval
                    ),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(proposal["partner_contract_id"]),),
            )
        await conn.commit()

    # Now fire Cluster 2. The audience id is final_ if the prospect
    # modified it pre-checkout, else proposed_.
    audience_id = _effective_audience_id(proposal)
    name = (
        f"Activation — {proposal['prospect_company_name']} × audience "
        f"{audience_id}"
    )
    activation = await ca_svc.instantiate_for_payment(
        organization_id=UUID(str(proposal["organization_id"])),
        brand_id=UUID(str(proposal["brand_id"])),
        partner_id=UUID(str(proposal["partner_id"])),
        partner_contract_id=UUID(str(proposal["partner_contract_id"])),
        data_engine_audience_id=UUID(str(audience_id)),
        name=name,
        metadata={
            "proposal_id": str(proposal["id"]),
            "stripe_checkout_session_id": stripe_checkout_session_id,
        },
    )

    leg2_id = activation.get("leg2_initiative_id")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.proposals
                SET instantiated_leg2_initiative_id = %s,
                    instantiated_at = NOW(),
                    updated_at = NOW()
                WHERE id = %s
                """,
                (str(leg2_id) if leg2_id else None, str(proposal["id"])),
            )
        await conn.commit()

    return {
        "proposal": await get_proposal(proposal["id"]),
        "instantiated": True,
        "activation": activation,
    }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _json_dump(value: Any) -> str:
    import json
    return json.dumps(value or {}, default=str)


__all__ = [
    "ProposalValidationError",
    "ProposalNotFound",
    "ProposalStateError",
    "create_proposal",
    "get_proposal",
    "get_proposal_by_token",
    "set_proposal_audience",
    "initiate_checkout",
    "mark_paid_and_instantiate",
]
