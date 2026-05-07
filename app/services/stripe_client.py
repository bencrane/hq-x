"""Stripe API client + webhook signature verification.

Direct httpx client (no `stripe-python` dependency) — keeps install
graph small and matches the pattern used by the Lob, Dub, and Entri
integrations elsewhere in hq-x.

Two surfaces:

  * ``create_checkout_session(...)`` — POST /v1/checkout/sessions to mint
    a hosted checkout URL the prospect is redirected to. Configured for
    one-shot payment (mode=payment), supports card + ACH (us_bank_account
    with instant verification via Plaid), and stamps the proposal id in
    both ``client_reference_id`` and ``metadata`` so the webhook handler
    can resolve back to the proposals row regardless of which field
    Stripe surfaces first.

  * ``verify_webhook_signature(payload, header, secret)`` — manual HMAC
    verification per Stripe's signing scheme (timestamped, prefix-keyed
    in the ``Stripe-Signature`` header). Constant-time compare. Raises
    on tampering, replay (>tolerance), or shape errors.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class StripeError(Exception):
    """Wraps any non-2xx Stripe API response."""

    def __init__(self, status_code: int, body: dict[str, Any] | str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Stripe API error {status_code}: {body!r}")


class StripeWebhookSignatureError(Exception):
    """Raised when a Stripe-Signature header fails verification."""


def _form_encode(params: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe-style form encoding: nested dicts/lists flattened into
    bracketed keys. e.g. {"a":{"b":1}} → "a[b]=1". Lists keep index keys
    when needed; for values we want repeated (payment_method_types[]),
    callers pass a list under a "[]"-suffixed key.
    """
    out: list[tuple[str, str]] = []
    for key, value in params.items():
        full = f"{prefix}[{key}]" if prefix else key
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(_form_encode(value, full))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    # Indexed sub-objects (line_items[0][...]).
                    idx = len([k for k, _ in out if k.startswith(f"{full}[")])
                    out.extend(_form_encode(item, f"{full}[{idx}]"))
                else:
                    out.append((f"{full}[]", str(item)))
        elif isinstance(value, bool):
            out.append((full, "true" if value else "false"))
        else:
            out.append((full, str(value)))
    return out


def _require_secret() -> str:
    key = settings.STRIPE_SECRET_KEY
    if key is None:
        raise StripeError(
            status_code=503,
            body={"error": "stripe_not_configured", "message": "STRIPE_SECRET_KEY unset"},
        )
    return key.get_secret_value()


async def create_checkout_session(
    *,
    proposal_id: str,
    prospect_contact_email: str | None,
    line_item_name: str,
    line_item_description: str | None,
    amount_cents: int,
    success_url: str,
    cancel_url: str,
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Mint a hosted Stripe Checkout session for one-shot payment.

    ``payment_method_types`` includes both ``card`` and ``us_bank_account``;
    the latter with ``verification_method=instant`` enables Plaid-backed
    instant ACH so the webhook fires the same day instead of waiting on
    a multi-day microdeposit cycle.

    Returns the full Stripe session object. The caller persists
    ``id`` (the cs_* checkout-session id) and the URL onto the proposals
    row before redirecting the prospect.
    """
    secret = _require_secret()
    full_metadata = {"proposal_id": proposal_id, **(metadata or {})}
    params: dict[str, Any] = {
        "mode": "payment",
        "payment_method_types[]": ["card", "us_bank_account"],
        "payment_method_options": {
            "us_bank_account": {
                "verification_method": "instant",
                "financial_connections": {"permissions": ["payment_method"]},
            },
        },
        "line_items": [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": amount_cents,
                    "product_data": {
                        "name": line_item_name,
                        "description": line_item_description,
                    },
                },
            }
        ],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "client_reference_id": proposal_id,
        "metadata": full_metadata,
    }
    if prospect_contact_email:
        params["customer_email"] = prospect_contact_email

    encoded = _form_encode(params)
    headers = {
        "Authorization": f"Bearer {secret}",
        "Stripe-Version": settings.STRIPE_API_VERSION,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.STRIPE_API_BASE}/v1/checkout/sessions",
            data=encoded,
            headers=headers,
        )
    if resp.status_code >= 300:
        try:
            body: Any = resp.json()
        except ValueError:
            body = resp.text
        raise StripeError(status_code=resp.status_code, body=body)
    return resp.json()


def verify_webhook_signature(
    *,
    payload: bytes,
    signature_header: str,
    secret: str,
    tolerance_seconds: int | None = None,
) -> int:
    """Verify Stripe-Signature header per the documented scheme.

    Header format: ``t=<unix_ts>,v1=<sig>[,v1=<sig>...][,v0=<sig>]``

    Signed string: ``f"{ts}.{payload_bytes}"`` HMAC-SHA256'd with the
    webhook secret. We accept any v1 signature that matches.

    Raises StripeWebhookSignatureError on:
      * malformed header
      * timestamp older than tolerance
      * no v1 signature matching the computed one

    Returns the verified timestamp (unix seconds) so callers can stamp
    receipt time deterministically.
    """
    if tolerance_seconds is None:
        tolerance_seconds = settings.STRIPE_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS

    pairs = [seg.strip() for seg in signature_header.split(",") if seg.strip()]
    parts: dict[str, list[str]] = {}
    for pair in pairs:
        if "=" not in pair:
            raise StripeWebhookSignatureError(f"malformed signature segment: {pair!r}")
        key, value = pair.split("=", 1)
        parts.setdefault(key, []).append(value)

    ts_strs = parts.get("t") or []
    if not ts_strs:
        raise StripeWebhookSignatureError("missing t= timestamp in Stripe-Signature")
    try:
        ts = int(ts_strs[0])
    except ValueError as exc:
        raise StripeWebhookSignatureError("non-integer t= timestamp") from exc

    age = int(time.time()) - ts
    if age > tolerance_seconds:
        raise StripeWebhookSignatureError(
            f"signature timestamp older than tolerance ({age}s > {tolerance_seconds}s)"
        )

    v1_sigs = parts.get("v1") or []
    if not v1_sigs:
        raise StripeWebhookSignatureError("no v1= signature present")

    signed_payload = f"{ts}.".encode() + payload
    expected = hmac.new(
        secret.encode(), signed_payload, hashlib.sha256
    ).hexdigest()

    for candidate in v1_sigs:
        if hmac.compare_digest(candidate, expected):
            return ts
    raise StripeWebhookSignatureError("no v1 signature matched")


__all__ = [
    "StripeError",
    "StripeWebhookSignatureError",
    "create_checkout_session",
    "verify_webhook_signature",
]
