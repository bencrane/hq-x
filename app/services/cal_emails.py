"""Cal.com webhook → branded email dispatch.

Disables Cal.com's built-in attendee emails (operator turns those off
on the Cal side) and replaces them with branded equivalents from our
own domain. Three triggers handled today:

  - BOOKING_CREATED     → confirmation to attendee + ping to operator
  - BOOKING_RESCHEDULED → reschedule notice to attendee + ping
  - BOOKING_CANCELLED   → cancellation notice to attendee + ping

Anything else (MEETING_STARTED/ENDED, unknown triggers) is a no-op.
Sends are fire-and-forget — exceptions are logged but never propagated
back to the webhook caller, so a Resend hiccup can't make Cal retry.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from app.config import settings
from app.services import resend_client

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Field extraction. Cal's payload shape is dictated by their docs:
# https://cal.com/docs/core-features/webhooks
# ---------------------------------------------------------------------------


def _normalize_name(name: str | None) -> str:
    if not name:
        return ""
    name = name.strip()
    if name.isupper() or name.islower():
        return name.title()
    return name


def _first_name(full: str) -> str:
    full = _normalize_name(full)
    return full.split(" ", 1)[0] if full else ""


def _format_dt(iso: str | None, tz: str | None) -> str:
    """Cal sends ISO UTC. We render in the attendee's tz when present."""
    if not iso:
        return "TBD"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if tz:
        try:
            from zoneinfo import ZoneInfo

            dt = dt.astimezone(ZoneInfo(tz))
        except Exception:  # noqa: BLE001 — bad tz string, fall back to UTC
            pass
    return dt.strftime("%A, %B %-d at %-I:%M %p %Z").strip()


def _extract(payload: dict[str, Any]) -> dict[str, Any]:
    """Pull every field we use across the three templates into one dict."""
    inner = payload.get("payload") or {}
    if not isinstance(inner, dict):
        inner = {}

    attendees = inner.get("attendees") or []
    primary = attendees[0] if attendees else {}
    attendee_email = primary.get("email") or ""
    attendee_name = _normalize_name(primary.get("name"))
    attendee_tz = primary.get("timeZone")

    organizer = inner.get("organizer") or {}
    organizer_name = _normalize_name(organizer.get("name"))

    return {
        "uid": inner.get("uid"),
        "title": inner.get("title") or "Meeting",
        "start_iso": inner.get("startTime"),
        "end_iso": inner.get("endTime"),
        "start_human": _format_dt(inner.get("startTime"), attendee_tz),
        "attendee_email": attendee_email,
        "attendee_name": attendee_name,
        "attendee_first": _first_name(primary.get("name") or ""),
        "organizer_name": organizer_name or "Benjamin Crane",
        "cancellation_reason": inner.get("cancellationReason"),
    }


# ---------------------------------------------------------------------------
# Templates. Plaintext only for v1 — no HTML, no marketing chrome.
# ---------------------------------------------------------------------------


def _attendee_created(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Confirmed: {ctx['start_human']}"
    body = (
        f"Hi {ctx['attendee_first'] or 'there'},\n\n"
        f"Your meeting with {ctx['organizer_name']} is confirmed for "
        f"{ctx['start_human']}.\n\n"
        f"You should have a calendar invite from Cal.com — if it didn't "
        f"arrive, reply to this email and I'll send it again.\n\n"
        f"Talk soon,\n"
        f"{ctx['organizer_name']}"
    )
    return subject, body


def _attendee_rescheduled(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = f"Updated time: {ctx['start_human']}"
    body = (
        f"Hi {ctx['attendee_first'] or 'there'},\n\n"
        f"Our meeting was rescheduled. New time: {ctx['start_human']}.\n\n"
        f"Calendar invite was updated automatically. Reply if anything "
        f"looks off.\n\n"
        f"Talk soon,\n"
        f"{ctx['organizer_name']}"
    )
    return subject, body


def _attendee_cancelled(ctx: dict[str, Any]) -> tuple[str, str]:
    subject = "Meeting cancelled"
    reason = (ctx.get("cancellation_reason") or "").strip()
    reason_line = f"\n\nReason: {reason}\n" if reason else "\n"
    body = (
        f"Hi {ctx['attendee_first'] or 'there'},\n\n"
        f"Our meeting on {ctx['start_human']} was cancelled.{reason_line}"
        f"If you'd like to reschedule, just reply to this email.\n\n"
        f"Talk soon,\n"
        f"{ctx['organizer_name']}"
    )
    return subject, body


def _operator_ping(trigger: str, ctx: dict[str, Any]) -> tuple[str, str]:
    label = {
        "BOOKING_CREATED": "new booking",
        "BOOKING_RESCHEDULED": "reschedule",
        "BOOKING_CANCELLED": "cancellation",
    }.get(trigger, trigger.lower())
    subject = (
        f"[cal {label}] {ctx['attendee_name'] or ctx['attendee_email']} — "
        f"{ctx['start_human']}"
    )
    body_lines = [
        f"Trigger:   {trigger}",
        f"Attendee:  {ctx['attendee_name']} <{ctx['attendee_email']}>",
        f"When:      {ctx['start_human']}",
        f"Title:     {ctx['title']}",
        f"Cal uid:   {ctx['uid']}",
    ]
    if ctx.get("cancellation_reason"):
        body_lines.append(f"Reason:    {ctx['cancellation_reason']}")
    return subject, "\n".join(body_lines)


_ATTENDEE_TEMPLATES = {
    "BOOKING_CREATED": _attendee_created,
    "BOOKING_RESCHEDULED": _attendee_rescheduled,
    "BOOKING_CANCELLED": _attendee_cancelled,
}


# ---------------------------------------------------------------------------
# Dispatch. Called from the webhook handler after the row is stored.
# ---------------------------------------------------------------------------


async def dispatch_for_event(payload: dict[str, Any]) -> None:
    trigger = payload.get("triggerEvent")
    if trigger not in _ATTENDEE_TEMPLATES:
        return

    ctx = _extract(payload)

    # Operator ping — always fires, even if attendee email is missing or
    # send fails.
    try:
        op_subject, op_body = _operator_ping(trigger, ctx)
        await resend_client.send_email(
            to=settings.RESEND_OPERATOR_ADDRESS,
            subject=op_subject,
            text=op_body,
        )
    except Exception:
        logger.exception("cal operator ping failed trigger=%s", trigger)

    # Attendee-facing email.
    if not ctx["attendee_email"]:
        logger.warning(
            "cal %s: no attendee email in payload — skipping attendee send",
            trigger,
        )
        return
    try:
        subject, body = _ATTENDEE_TEMPLATES[trigger](ctx)
        await resend_client.send_email(
            to=ctx["attendee_email"],
            subject=subject,
            text=body,
            reply_to=settings.RESEND_OPERATOR_ADDRESS,
        )
    except Exception:
        logger.exception(
            "cal attendee email failed trigger=%s to=%s",
            trigger,
            ctx["attendee_email"],
        )
