"""Public inbound endpoint for the marketing-site qualify form.

The form is hosted on engineered-demand-site-v1 and posts here via a
thin server-side forwarder. Keeping the Resend integration in hq-x
means the marketing site holds zero secrets.

Flow: form → site `/api/qualify` (proxy) → hq-x `/api/v1/qualify`
       → Resend email to operator.

No persistence today; the operator's inbox is the audit trail. Add a
`business.inquiries` table when there's a reason to (admin list view,
deduplication, etc.).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from app.config import settings
from app.services import resend_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/qualify", tags=["public", "qualify"])


class QualifySubmission(BaseModel):
    company_name: str = Field(min_length=1, max_length=200)
    contact_name: str = Field(min_length=1, max_length=200)
    contact_email: EmailStr
    contact_phone: str | None = Field(default=None, max_length=50)
    website: str | None = Field(default=None, max_length=200)

    annual_revenue_range: str = Field(min_length=1, max_length=80)
    monthly_lead_capacity: str = Field(min_length=1, max_length=80)
    current_lead_sources: str = Field(min_length=1, max_length=2000)

    average_ltv_usd: str = Field(min_length=1, max_length=80)
    close_rate_qualified_meetings: str = Field(min_length=1, max_length=80)
    sales_cycle_length: str | None = Field(default=None, max_length=80)

    ideal_customer: str = Field(min_length=10, max_length=2000)
    anti_fit_notes: str | None = Field(default=None, max_length=2000)
    acknowledged_selective: bool

    meeting_url_or_calendar: str | None = Field(default=None, max_length=500)

    model_config = {"extra": "forbid"}


def _format_body(s: QualifySubmission) -> str:
    lines = [
        "── PROSPECT ──",
        f"Company:      {s.company_name}",
        f"Contact:      {s.contact_name} <{s.contact_email}>",
    ]
    if s.contact_phone:
        lines.append(f"Phone:        {s.contact_phone}")
    if s.website:
        lines.append(f"Website:      {s.website}")

    lines += [
        "",
        "── CAPACITY ──",
        f"Revenue:      {s.annual_revenue_range}",
        f"Lead cap/mo:  {s.monthly_lead_capacity}",
        f"Sources mix:  {s.current_lead_sources}",
        "",
        "── ECONOMICS ──",
        f"LTV:          {s.average_ltv_usd}",
        f"Close rate:   {s.close_rate_qualified_meetings}",
    ]
    if s.sales_cycle_length:
        lines.append(f"Sales cycle:  {s.sales_cycle_length}")

    lines += [
        "",
        "── FIT ──",
        "Ideal customer:",
        s.ideal_customer,
        "",
        f"Anti-fit notes:\n{s.anti_fit_notes}"
        if s.anti_fit_notes
        else "Anti-fit notes: (none)",
    ]
    if s.meeting_url_or_calendar:
        lines += ["", f"Calendar / meeting URL: {s.meeting_url_or_calendar}"]

    return "\n".join(lines)


@router.post("", status_code=status.HTTP_200_OK)
async def submit_qualify(payload: QualifySubmission) -> dict[str, object]:
    if not payload.acknowledged_selective:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "acknowledgement_required"},
        )

    if settings.RESEND_API_KEY is None:
        # Dev / unconfigured envs: don't 500. Log + accept.
        logger.warning(
            "qualify submission accepted but not emailed — RESEND_API_KEY unset",
        )
        return {"ok": True, "emailed": False}

    subject = f"[qualify] {payload.company_name} — {payload.contact_name}"
    body = _format_body(payload)

    try:
        await resend_client.send_email(
            to=settings.RESEND_OPERATOR_ADDRESS,
            subject=subject,
            text=body,
            reply_to=str(payload.contact_email),
        )
    except resend_client.ResendError as exc:
        logger.exception("qualify email send failed")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"error": "email_send_failed", "message": str(exc)},
        ) from exc

    return {"ok": True, "emailed": True}


__all__ = ["router"]
