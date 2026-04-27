"""Test helpers for ES256-signed JWTs.

Supabase signs tokens with an asymmetric key (ES256/EC P-256). Tests
generate their own EC keypair and monkeypatch `_get_signing_key` so
verification uses the matching public key without hitting the JWKS
endpoint.
"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

# A second keypair for "wrong signature" tests.
_OTHER_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
PUBLIC_KEY = _PRIVATE_KEY.public_key()
WRONG_SIGNER_PRIVATE_KEY = _OTHER_PRIVATE_KEY


def make_token(
    *,
    sub: str | None = None,
    aud: str = "authenticated",
    exp_offset: int = 3600,
    extra: dict | None = None,
    signer=_PRIVATE_KEY,
) -> str:
    now = int(time.time())
    payload = {
        "sub": sub or str(uuid4()),
        "aud": aud,
        "iat": now,
        "exp": now + exp_offset,
        "role": "authenticated",
        "email": "test@example.com",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, signer, algorithm="ES256")
