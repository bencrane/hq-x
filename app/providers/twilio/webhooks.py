from __future__ import annotations

import base64
import hashlib
import hmac


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
