"""One-shot seed: fake Cal.com bookings so the admin bookings page has rows.

Usage:
    cd apps/hq-x
    doppler run --project hq-x --config prd -- uv run python -m scripts.seed_fake_bookings

Idempotent: deletes any cal_raw_events rows whose cal_event_uid starts with
'seed-fake-' before inserting, so re-runs don't pile up. To remove the fake
data entirely:
    DELETE FROM cal_raw_events WHERE cal_event_uid LIKE 'seed-fake-%';
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone

from app.db import close_pool, get_db_connection, init_pool

ORGANIZER_EMAIL = "calls@rarestructure.com"
EVENT_TITLE = "Rare Structure — Catalyst Origination Intro"

# (uid suffix, attendee name, attendee email, days from now the call sits)
FAKE_BOOKINGS = [
    ("001", "Marcus Webb", "marcus@summitcivil.com", -4),
    ("002", "Dana Ortiz", "dana@ridgelineinfra.com", -2),
    ("003", "Priya Nair", "priya@calhounpaving.com", -1),
    ("004", "Tom Brennan", "tom@deltabridgesteel.com", 1),
    ("005", "Angela Voss", "angela@cascadeearthworks.com", 3),
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _booking_row(
    uid_suffix: str, name: str, email: str, days: int
) -> tuple[str, str, str, str, str]:
    uid = f"seed-fake-{uid_suffix}"
    start = datetime.now(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ) + timedelta(days=days)
    end = start + timedelta(minutes=30)
    payload = {
        "triggerEvent": "BOOKING_CREATED",
        "createdAt": _iso(datetime.now(timezone.utc)),
        "payload": {
            "uid": uid,
            "title": EVENT_TITLE,
            "startTime": _iso(start),
            "endTime": _iso(end),
            "attendees": [
                {
                    "name": name,
                    "email": email,
                    "timeZone": "America/Los_Angeles",
                }
            ],
            "organizer": {"name": "Rare Structure", "email": ORGANIZER_EMAIL},
        },
    }
    return (
        "BOOKING_CREATED",
        json.dumps(payload),
        uid,
        ORGANIZER_EMAIL,
        json.dumps([email]),
    )


async def main() -> int:
    await init_pool()
    deleted = 0
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM cal_raw_events "
                    "WHERE cal_event_uid LIKE 'seed-fake-%'"
                )
                deleted = cur.rowcount
                for uid_suffix, name, email, days in FAKE_BOOKINGS:
                    await cur.execute(
                        """
                        INSERT INTO cal_raw_events
                            (trigger_event, payload, cal_event_uid,
                             organizer_email, attendee_emails)
                        VALUES (%s, %s::jsonb, %s, %s, %s::jsonb)
                        """,
                        _booking_row(uid_suffix, name, email, days),
                    )
            await conn.commit()
    finally:
        await close_pool()

    print()
    print("=" * 56)
    print("FAKE BOOKINGS SEEDED")
    print("=" * 56)
    print(f"  removed prior seed rows: {deleted}")
    print(f"  inserted:                {len(FAKE_BOOKINGS)}")
    print("  visible at:              /admin/bookings")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
