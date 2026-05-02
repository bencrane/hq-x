"""Smoke test for the §7.6 + §7.7 internal voice-callback endpoints.

Boots the FastAPI app via httpx ASGITransport, hits the two internal
endpoints with the trigger shared secret, and (optionally) inserts a
fake voice_callback_requests row 10 minutes in the future to confirm
the reminder path stamps reminder_sent_at on the row even when the
brand has no real Twilio creds (the SmsSuppressedError vs send-failure
branch logs as a row error, but the unit-of-work path itself is what
we're verifying).

Run:
    doppler run --project hq-x --config dev -- \\
        uv run python -m scripts.smoke_voice_callbacks
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings
from app.db import close_pool, get_db_connection, init_pool
from app.main import app


SECRET = settings.TRIGGER_SHARED_SECRET or ""


async def _post(client: httpx.AsyncClient, path: str) -> httpx.Response:
    return await client.post(
        path,
        json={"trigger_run_id": "smoke"},
        headers={"Authorization": f"Bearer {SECRET}"},
    )


async def _seed_callback_row(brand_id: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Insert a callback row 10 minutes in the future + a suppression entry
    so the reminder path stamps the sentinel via SmsSuppressedError.
    """
    callback_id = uuid.uuid4()
    voice_phone_id = uuid.uuid4()
    customer_number = f"+15555{callback_id.int % 100000:05d}"
    in_ten_min = datetime.now(timezone.utc) + timedelta(minutes=10)
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO voice_phone_numbers (
                    id, brand_id, phone_number, provider, status, purpose
                )
                VALUES (%s, %s, %s, 'twilio', 'pending', 'outbound')
                """,
                (str(voice_phone_id), brand_id, f"+1555000{callback_id.int % 10000:04d}"),
            )
            await cur.execute(
                """
                INSERT INTO sms_suppressions (brand_id, phone_number, reason)
                VALUES (%s, %s, 'manual')
                ON CONFLICT (brand_id, phone_number) DO NOTHING
                """,
                (brand_id, customer_number),
            )
            await cur.execute(
                """
                INSERT INTO voice_callback_requests (
                    id, brand_id, source_vapi_call_id,
                    customer_number, preferred_time, timezone, status,
                    voice_phone_number_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, 'scheduled', %s)
                """,
                (
                    str(callback_id), brand_id, f"smoke-{callback_id}",
                    customer_number, in_ten_min, "America/New_York",
                    str(voice_phone_id),
                ),
            )
        await conn.commit()
    return callback_id, voice_phone_id, customer_number


async def _read_reminder_state(callback_id: uuid.UUID) -> tuple:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT reminder_sent_at, reminder_sms_sid, status
                FROM voice_callback_requests WHERE id = %s
                """,
                (str(callback_id),),
            )
            row = await cur.fetchone()
    return row


async def _cleanup(
    callback_id: uuid.UUID, voice_phone_id: uuid.UUID, customer_number: str, brand_id: str
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM voice_callback_requests WHERE id = %s",
                (str(callback_id),),
            )
            await cur.execute(
                "DELETE FROM voice_phone_numbers WHERE id = %s",
                (str(voice_phone_id),),
            )
            await cur.execute(
                "DELETE FROM sms_suppressions WHERE brand_id = %s AND phone_number = %s",
                (brand_id, customer_number),
            )
        await conn.commit()


async def _pick_brand_id() -> str | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM business.brands WHERE deleted_at IS NULL LIMIT 1"
            )
            row = await cur.fetchone()
    return str(row[0]) if row else None


async def main() -> int:
    if not SECRET:
        print("TRIGGER_SHARED_SECRET is not set; cannot run smoke test")
        return 2

    await init_pool()
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://smoke") as client:
            # 1. Empty-table smoke — both endpoints should return processed=0.
            r1 = await _post(client, "/internal/voice/callback/send-reminders")
            print(f"send-reminders (empty): {r1.status_code} {r1.json()}")
            assert r1.status_code == 200, r1.text

            r2 = await _post(client, "/internal/voice/callback/run-due-callbacks")
            print(f"run-due-callbacks (empty): {r2.status_code} {r2.json()}")
            assert r2.status_code == 200, r2.text

            # 2. Seed-and-verify reminder path.
            brand_id = await _pick_brand_id()
            if brand_id is None:
                print("no brand exists — skipping seeded smoke; emptiness path verified")
                return 0

            callback_id, voice_phone_id, customer_number = await _seed_callback_row(brand_id)
            try:
                r3 = await _post(client, "/internal/voice/callback/send-reminders")
                print(f"send-reminders (seeded): {r3.status_code} {r3.json()}")
                assert r3.status_code == 200, r3.text
                state = await _read_reminder_state(callback_id)
                print(
                    "row state after send: "
                    f"reminder_sent_at={state[0]} sid={state[1]} status={state[2]}"
                )
                payload = r3.json()
                assert payload["processed"] >= 1, payload
                # The customer_number was suppressed at seed time, so the
                # reminder must have stamped the sentinel via SmsSuppressedError.
                assert state[0] is not None, "reminder_sent_at was not stamped"
                assert payload["suppressed"] >= 1, payload
            finally:
                await _cleanup(callback_id, voice_phone_id, customer_number, brand_id)
    finally:
        await close_pool()

    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
