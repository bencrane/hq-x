"""Brand CRUD + encrypted-credential read/write helpers.

Twilio account_sid / auth_token are stored encrypted in
business.brands.twilio_account_sid_enc / twilio_auth_token_enc via
pgcrypto pgp_sym_encrypt. The master key lives in
settings.BRAND_CREDS_ENCRYPTION_KEY (Doppler-injected).

Plaintext never lands in a SQL log: encryption + decryption happen inside
the Postgres engine via pgp_sym_encrypt / pgp_sym_decrypt with the key
passed as a parameter, not interpolated into the SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.config import settings
from app.db import get_db_connection


class BrandCredsKeyMissing(RuntimeError):
    """BRAND_CREDS_ENCRYPTION_KEY is not set; encrypted columns cannot be read or written."""


def _key() -> str:
    if settings.BRAND_CREDS_ENCRYPTION_KEY is None:
        raise BrandCredsKeyMissing(
            "BRAND_CREDS_ENCRYPTION_KEY not configured — set it in Doppler"
        )
    return settings.BRAND_CREDS_ENCRYPTION_KEY.get_secret_value()


@dataclass(frozen=True)
class Brand:
    id: UUID
    name: str
    display_name: str | None
    domain: str | None
    twilio_messaging_service_sid: str | None
    primary_customer_profile_sid: str | None
    trust_hub_registration_id: UUID | None


@dataclass(frozen=True)
class BrandTwilioCreds:
    account_sid: str
    auth_token: str


async def create_brand(
    *,
    name: str,
    display_name: str | None = None,
    domain: str | None = None,
    twilio_account_sid: str | None = None,
    twilio_auth_token: str | None = None,
    twilio_messaging_service_sid: str | None = None,
) -> UUID:
    """Create a brand. Encrypted creds are optional at creation time —
    Trust Hub registration may complete first and fill them in later."""
    if (twilio_account_sid is None) != (twilio_auth_token is None):
        raise ValueError("must provide twilio_account_sid and twilio_auth_token together, or neither")

    key = _key() if twilio_account_sid is not None else None

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.brands (
                    name, display_name, domain,
                    twilio_account_sid_enc, twilio_auth_token_enc,
                    twilio_messaging_service_sid
                )
                VALUES (
                    %s, %s, %s,
                    CASE WHEN %s::text IS NOT NULL
                         THEN pgp_sym_encrypt(%s::text, %s::text) END,
                    CASE WHEN %s::text IS NOT NULL
                         THEN pgp_sym_encrypt(%s::text, %s::text) END,
                    %s
                )
                RETURNING id
                """,
                (
                    name, display_name, domain,
                    twilio_account_sid, twilio_account_sid, key,
                    twilio_auth_token, twilio_auth_token, key,
                    twilio_messaging_service_sid,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def get_brand(brand_id: UUID) -> Brand | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, display_name, domain,
                       twilio_messaging_service_sid,
                       primary_customer_profile_sid,
                       trust_hub_registration_id
                FROM business.brands
                WHERE id = %s AND deleted_at IS NULL
                """,
                (str(brand_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return Brand(
        id=row[0],
        name=row[1],
        display_name=row[2],
        domain=row[3],
        twilio_messaging_service_sid=row[4],
        primary_customer_profile_sid=row[5],
        trust_hub_registration_id=row[6],
    )


async def list_brands() -> list[Brand]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, display_name, domain,
                       twilio_messaging_service_sid,
                       primary_customer_profile_sid,
                       trust_hub_registration_id
                FROM business.brands
                WHERE deleted_at IS NULL
                ORDER BY created_at
                """
            )
            rows = await cur.fetchall()
    return [
        Brand(
            id=r[0], name=r[1], display_name=r[2], domain=r[3],
            twilio_messaging_service_sid=r[4],
            primary_customer_profile_sid=r[5],
            trust_hub_registration_id=r[6],
        )
        for r in rows
    ]


async def get_twilio_creds(brand_id: UUID) -> BrandTwilioCreds | None:
    """Decrypt and return Twilio account_sid + auth_token for a brand.

    Returns None if no creds are set on the brand. Raises
    BrandCredsKeyMissing if the master key isn't configured.
    """
    key = _key()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    pgp_sym_decrypt(twilio_account_sid_enc, %s),
                    pgp_sym_decrypt(twilio_auth_token_enc, %s)
                FROM business.brands
                WHERE id = %s AND deleted_at IS NULL
                  AND twilio_account_sid_enc IS NOT NULL
                  AND twilio_auth_token_enc IS NOT NULL
                """,
                (key, key, str(brand_id)),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return BrandTwilioCreds(account_sid=row[0], auth_token=row[1])


async def set_twilio_creds(
    brand_id: UUID,
    *,
    account_sid: str,
    auth_token: str,
) -> None:
    """Update encrypted Twilio creds on an existing brand."""
    key = _key()
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.brands
                SET twilio_account_sid_enc = pgp_sym_encrypt(%s, %s),
                    twilio_auth_token_enc = pgp_sym_encrypt(%s, %s),
                    updated_at = NOW()
                WHERE id = %s AND deleted_at IS NULL
                """,
                (account_sid, key, auth_token, key, str(brand_id)),
            )
        await conn.commit()


async def get_brand_id_by_phone_number(phone_number: str) -> UUID | None:
    """Resolve brand_id from a phone number, looking at voice_phone_numbers
    first, then voice_assistant_phone_configs.

    Used by the Vapi webhook brand resolver and the Twilio webhook receiver.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT brand_id FROM voice_phone_numbers
                WHERE phone_number = %s AND deleted_at IS NULL
                LIMIT 1
                """,
                (phone_number,),
            )
            row = await cur.fetchone()
            if row is not None:
                return row[0]

            await cur.execute(
                """
                SELECT brand_id FROM voice_assistant_phone_configs
                WHERE phone_number = %s AND deleted_at IS NULL AND is_active = TRUE
                LIMIT 1
                """,
                (phone_number,),
            )
            row = await cur.fetchone()
            if row is not None:
                return row[0]
    return None
