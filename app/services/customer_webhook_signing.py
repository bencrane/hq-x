"""HMAC-SHA256 signing for outbound customer webhook deliveries.

Header: ``X-HQX-Signature: sha256=<hex>``
Computed over the raw body bytes (the JSON we send to the customer),
keyed by their subscription secret. Customers verify by recomputing the
same HMAC and ``hmac.compare_digest``-ing against the header.

We only ever store ``secret_hash`` in the DB (a one-way hash of the
secret + a per-env salt). The plaintext secret is returned exactly once
on creation / rotation so the customer can save it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from app.config import settings

_SIGNATURE_HEADER = "X-HQX-Signature"


def generate_secret() -> str:
    """Return a fresh subscription secret. 256 bits of entropy as
    URL-safe base64; 43 chars."""
    return secrets.token_urlsafe(32)


def hash_secret(secret: str) -> str:
    """One-way hash of the secret. Salted with a per-env value so dev
    hashes don't deanon prod hashes."""
    salt = (settings.LANDING_PAGE_IP_HASH_SALT or "hqx-customer-webhook-salt").encode()
    return hashlib.sha256(salt + secret.encode("utf-8")).hexdigest()


def verify_secret(secret: str, secret_hash: str) -> bool:
    """Constant-time compare against a stored hash."""
    return hmac.compare_digest(hash_secret(secret), secret_hash)


def sign_payload(secret: str, body: bytes) -> str:
    """Return the value to set for ``X-HQX-Signature``.

    Format: ``sha256=<hex>``.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Confirm a presented signature matches what we'd compute. Used by
    tests and by any future inbound-acknowledgement flow."""
    if not signature.startswith("sha256="):
        return False
    return hmac.compare_digest(sign_payload(secret, body), signature)


__all__ = [
    "generate_secret",
    "hash_secret",
    "verify_secret",
    "sign_payload",
    "verify_signature",
    "_SIGNATURE_HEADER",
]
