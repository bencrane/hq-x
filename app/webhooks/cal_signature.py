import hashlib
import hmac
import logging

from app.config import settings

logger = logging.getLogger(__name__)


def verify_cal_signature(raw_body: bytes, signature_header: str | None) -> bool:
    secret = settings.CAL_WEBHOOK_SECRET
    if not secret:
        logger.warning("CAL_WEBHOOK_SECRET not set — skipping HMAC verification")
        return True
    if not signature_header:
        logger.warning("Missing X-Cal-Signature-256 header")
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
