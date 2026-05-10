"""Branded proposal email — sent to the prospect on form submit.

Plaintext only. Carries the totals and the payment-page link. Operator
gets a copy via internal ping so the audit trail isn't only in Resend.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import settings
from app.services import resend_client

logger = logging.getLogger(__name__)


def _money(cents: int) -> str:
    return f"${cents / 100:,.0f}"


def _payment_url(public_token: str) -> str:
    base = settings.PARTNER_PLATFORM_BASE_URL.rstrip("/")
    return f"{base}/proposal/{public_token}/payment"


def _attendee_template(proposal: dict[str, Any]) -> tuple[str, str]:
    contact = proposal.get("prospect_contact_name") or ""
    first = contact.split(" ", 1)[0] if contact else "there"
    company = proposal["prospect_company_name"]
    transfers = proposal["proposed_transfer_count"]
    per_lead = _money(proposal["proposed_price_per_transfer_cents"])
    total = _money(proposal["proposed_total_cents"])
    window = proposal["proposed_window_days"]
    url = _payment_url(proposal["public_token"])

    subject = f"Lead transfer agreement — {company}"
    body = (
        f"Hi {first},\n\n"
        f"Following up on our call. Here's the proposal:\n\n"
        f"  • {transfers} qualified lead transfers\n"
        f"  • {per_lead} per transfer\n"
        f"  • {window}-day delivery window\n"
        f"  • Total: {total}\n\n"
        f"Payment link (pay by card or US bank account):\n{url}\n\n"
        f"Reply to this email if anything looks off or you want to "
        f"adjust scope.\n\n"
        f"Talk soon,\n"
        f"Benjamin Crane"
    )
    return subject, body


def _operator_template(proposal: dict[str, Any]) -> tuple[str, str]:
    company = proposal["prospect_company_name"]
    contact_email = proposal.get("prospect_contact_email") or "—"
    total = _money(proposal["proposed_total_cents"])
    url = _payment_url(proposal["public_token"])
    subject = f"[proposal sent] {company} — {total}"
    body = (
        f"Proposal sent to {contact_email}.\n\n"
        f"Company:    {company}\n"
        f"Total:      {total}\n"
        f"Transfers:  {proposal['proposed_transfer_count']}\n"
        f"Per lead:   {_money(proposal['proposed_price_per_transfer_cents'])}\n"
        f"Window:     {proposal['proposed_window_days']}d\n"
        f"Token:      {proposal['public_token']}\n"
        f"Pay link:   {url}\n"
    )
    return subject, body


async def send_proposal_email(proposal: dict[str, Any]) -> bool:
    """Send proposal to prospect + ping operator. Returns True if the
    prospect-facing send succeeded. Operator ping failures are logged
    only — they don't block the flow.
    """
    if not proposal.get("prospect_contact_email"):
        logger.warning(
            "send_proposal_email: no prospect_contact_email on proposal %s",
            proposal.get("id"),
        )
        return False

    try:
        op_subject, op_body = _operator_template(proposal)
        await resend_client.send_email(
            to=settings.RESEND_OPERATOR_ADDRESS,
            subject=op_subject,
            text=op_body,
        )
    except Exception:
        logger.exception("operator ping failed for proposal %s", proposal.get("id"))

    subject, body = _attendee_template(proposal)
    await resend_client.send_email(
        to=proposal["prospect_contact_email"],
        subject=subject,
        text=body,
        reply_to=settings.RESEND_OPERATOR_ADDRESS,
    )
    return True
