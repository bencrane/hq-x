from typing import Any

FLAT_PAYLOAD_TRIGGERS = {"MEETING_STARTED", "MEETING_ENDED"}


def extract_cal_fields(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract storage fields from either Cal.com payload shape.

    Standard envelope:  {triggerEvent, createdAt, payload: {uid, hosts, attendees, ...}}
    Flat (meetings):    {triggerEvent, createdAt, bookingId, roomName, ...}
    """
    trigger_event = payload.get("triggerEvent", "unknown")

    if trigger_event in FLAT_PAYLOAD_TRIGGERS:
        return {
            "trigger_event": trigger_event,
            "cal_event_uid": None,
            "organizer_email": None,
            "attendee_emails": [],
            "event_type_id": None,
        }

    inner = payload.get("payload", {})
    if not isinstance(inner, dict):
        inner = {}

    hosts = inner.get("hosts") or []
    organizer_email = hosts[0].get("email") if hosts else None

    attendees = inner.get("attendees") or []
    attendee_emails = [a.get("email") for a in attendees if a.get("email")]
    guests = inner.get("guests") or []
    attendee_emails.extend(g for g in guests if isinstance(g, str))

    return {
        "trigger_event": trigger_event,
        "cal_event_uid": inner.get("uid"),
        "organizer_email": organizer_email,
        "attendee_emails": attendee_emails,
        "event_type_id": inner.get("eventTypeId"),
    }
