"""Address hashing + suppression-list IO + pre-send verify gate.

Single source of truth for the normalized-address canonicalization that the
suppression-list unique key (address_hash, reason) is computed against.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.observability import incr_metric, log_event
from app.providers.lob import client as lob_client
from app.providers.lob.client import LobProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class NormalizedAddress:
    line1: str
    line2: str | None
    city: str
    state: str
    zip5: str
    address_hash: str


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _zip5(value: Any) -> str:
    text = "".join(ch for ch in str(value or "") if ch.isdigit())
    return text[:5]


def normalize_address(address: dict[str, Any]) -> NormalizedAddress:
    line1 = _norm(address.get("address_line1"))
    line2_raw = address.get("address_line2")
    line2 = _norm(line2_raw) or None
    city = _norm(address.get("address_city"))
    state = _norm(address.get("address_state"))
    zip5 = _zip5(address.get("address_zip"))
    canonical = f"{line1}|{line2 or ''}|{city}|{state}|{zip5}"
    address_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return NormalizedAddress(
        line1=line1,
        line2=line2,
        city=city,
        state=state,
        zip5=zip5,
        address_hash=address_hash,
    )


def address_hash_for(address: dict[str, Any]) -> str:
    return normalize_address(address).address_hash


async def is_address_suppressed(address_hash: str) -> dict[str, Any] | None:
    """Return the suppression row that blocks this address, or None.

    Picks the highest-severity reason if multiple rows exist; for the MVP we
    just take the most recent.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, reason, suppressed_at, notes
                FROM suppressed_addresses
                WHERE address_hash = %s
                ORDER BY suppressed_at DESC
                LIMIT 1
                """,
                (address_hash,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "reason": row[1],
        "suppressed_at": row[2],
        "notes": row[3],
    }


async def insert_suppression(
    *,
    address: dict[str, Any],
    reason: str,
    source_event_id: str | None = None,
    source_piece_id: UUID | None = None,
    notes: str | None = None,
) -> bool:
    """Insert a suppression row. Returns True on insert, False on dedup."""
    norm = normalize_address(address)
    if not norm.line1 or not norm.city or not norm.state or not norm.zip5:
        # Lob always returns these on inbound webhooks; if an address is
        # this thin, refuse to suppress rather than insert garbage.
        logger.warning("suppression_skip_thin_address reason=%s hash=%s", reason, norm.address_hash)
        return False
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO suppressed_addresses
                    (address_hash, address_line1, address_line2, address_city,
                     address_state, address_zip, reason, source_event_id,
                     source_piece_id, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (address_hash, reason) DO NOTHING
                RETURNING id
                """,
                (
                    norm.address_hash,
                    norm.line1,
                    norm.line2,
                    norm.city,
                    norm.state,
                    norm.zip5,
                    reason,
                    source_event_id,
                    str(source_piece_id) if source_piece_id else None,
                    notes,
                ),
            )
            row = await cur.fetchone()
    inserted = row is not None
    if inserted:
        incr_metric("direct_mail.suppression.inserted", reason=reason)
        log_event(
            "direct_mail_suppression_inserted",
            reason=reason,
            address_hash=norm.address_hash,
            source_event_id=source_event_id,
            source_piece_id=str(source_piece_id) if source_piece_id else None,
        )
    return inserted


@dataclass(frozen=True)
class AddressVerifyResult:
    deliverability: str | None
    raw: dict[str, Any] | None


def _deliverability_class(verdict: str | None) -> str:
    if not verdict:
        return "unknown"
    text = str(verdict).lower()
    if text.startswith("deliverable"):
        return "deliverable"
    if text == "undeliverable":
        return "undeliverable"
    return text


async def verify_or_suppress(
    *,
    api_key: str,
    payload: dict[str, Any],
    skip: bool,
) -> AddressVerifyResult:
    """Pre-send US address-verify gate.

    - When `payload.to` is a string (Lob saved-address ID), skip the gate.
    - When `skip=True`, log a warning and skip.
    - Otherwise call Lob's US verify endpoint. On `undeliverable`, raise
      `AddressUndeliverable` and insert a suppression row keyed
      `(address_hash, reason='undeliverable_at_send')`.
    - On suppression-list match (a prior returned/failed/etc), raise
      `AddressSuppressed` with the suppression reason.
    """
    recipient = payload.get("to")
    if isinstance(recipient, str):
        return AddressVerifyResult(deliverability=None, raw=None)
    if not isinstance(recipient, dict):
        return AddressVerifyResult(deliverability=None, raw=None)

    address_hash = address_hash_for(recipient)
    blocking = await is_address_suppressed(address_hash)
    if blocking:
        incr_metric("direct_mail.address.suppressed", reason=blocking["reason"])
        raise AddressSuppressed(reason=blocking["reason"], address_hash=address_hash)

    if skip:
        log_event(
            "direct_mail_verify_skipped",
            address_hash=address_hash,
            level=logging.WARNING,
        )
        incr_metric("direct_mail.verify.skipped")
        return AddressVerifyResult(deliverability=None, raw=None)

    try:
        result = lob_client.verify_address_us_single(api_key, recipient)
    except LobProviderError as exc:
        # Don't fail the send because verify is down — log + proceed. Fail-open
        # is the right policy here; verify is a nice-to-have, not a precondition.
        log_event(
            "direct_mail_verify_provider_error",
            level=logging.WARNING,
            address_hash=address_hash,
            error=str(exc)[:200],
        )
        incr_metric("direct_mail.verify.provider_error")
        return AddressVerifyResult(deliverability=None, raw=None)

    deliverability = result.get("deliverability")
    klass = _deliverability_class(deliverability)
    incr_metric("direct_mail.verify.outcome", outcome=klass)

    if klass == "undeliverable":
        await insert_suppression(
            address=recipient,
            reason="undeliverable_at_send",
            notes="Auto-suppressed by pre-send verify gate.",
        )
        raise AddressUndeliverable(
            deliverability=deliverability or "undeliverable",
            address_hash=address_hash,
            raw=result,
        )

    return AddressVerifyResult(deliverability=deliverability, raw=result)


class AddressSuppressed(Exception):
    def __init__(self, *, reason: str, address_hash: str) -> None:
        super().__init__(f"address suppressed: {reason}")
        self.reason = reason
        self.address_hash = address_hash


class AddressUndeliverable(Exception):
    def __init__(
        self,
        *,
        deliverability: str,
        address_hash: str,
        raw: dict[str, Any] | None,
    ) -> None:
        super().__init__(f"address undeliverable: {deliverability}")
        self.deliverability = deliverability
        self.address_hash = address_hash
        self.raw = raw
