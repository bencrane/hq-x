from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


def reconstruct_public_url(request: "Request") -> str:
    """Build the public URL Twilio originally signed.

    Behind a TLS-terminating proxy (Railway, etc.) `request.url` is the
    *internal* URL — `http://internal-host:8000/...` — but Twilio signs the
    *external* `https://api.example.com/...`. Honor `X-Forwarded-Proto`
    and `X-Forwarded-Host` so the signature input matches what Twilio
    computed against, both in dev (no proxy) and prd.
    """
    proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.headers.get("Host", ""))
    path = request.url.path
    query = str(request.url.query) if request.url.query else ""
    return f"{proto}://{host}{path}" + (f"?{query}" if query else "")


def validate_twilio_signature(
    auth_token: str,
    url: str,
    params: dict[str, str],
    signature: str,
) -> bool:
    """
    Validate the X-Twilio-Signature header using HMAC-SHA1.

    Algorithm:
    1. Start with the full webhook URL
    2. Sort POST params by key (case-sensitive) and append key+value to the URL string
    3. HMAC-SHA1 with auth_token as key
    4. Base64 encode the digest
    5. Constant-time compare to the provided signature
    """
    s = url
    for key in sorted(params.keys()):
        s += key + params[key]

    computed = base64.b64encode(
        hmac.new(
            auth_token.encode("utf-8"),
            s.encode("utf-8"),
            hashlib.sha1,
        ).digest()
    ).decode("utf-8")

    return hmac.compare_digest(computed, signature)
