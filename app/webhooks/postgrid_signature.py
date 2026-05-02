"""PostGrid webhook signature verification — HMAC-SHA256 over raw body.

PostGrid signs webhooks with a shared secret the integrator supplies at
subscription-create time (POST /v1/webhooks { secret: "..." }). The
signature arrives in the `x-webhook-secret` header (PostGrid's convention)
as a hex-encoded HMAC-SHA256 digest of the raw request body.

Modes (set via POSTGRID_WEBHOOK_SIGNATURE_MODE):
  - enforce          : reject anything that doesn't verify
  - permissive_audit : audit + log failures, but accept the event
  - disabled         : do not verify (insecure; refused at boot in prd)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import settings
from app.observability import incr_metric, log_event

_VALID_MODES = {"enforce", "permissive_audit", "disabled"}

# PostGrid sends the HMAC-SHA256 hex digest of the raw body in this header.
POSTGRID_SIGNATURE_HEADER = "x-webhook-secret"


def _invalid_signature(*, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "type": "webhook_signature_invalid",
            "provider": "postgrid",
            "reason": reason,
            "message": message,
        },
    )


def _normalized_mode() -> str:
    raw = (
        getattr(settings, "POSTGRID_WEBHOOK_SIGNATURE_MODE", None) or "permissive_audit"
    ).strip().lower()
    return raw if raw in _VALID_MODES else "permissive_audit"


def compute_postgrid_signature(raw_body: bytes, secret: str) -> str:
    """Compute the expected HMAC-SHA256 hex signature for a PostGrid webhook payload."""
    return hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def verify_postgrid_signature(
    *,
    raw_body: bytes,
    request: Request,
    request_id: str | None,
) -> dict[str, Any]:
    """Verify the PostGrid webhook signature.

    Returns a result dict with signature metadata. On `enforce` mode,
    raises HTTPException for invalid signatures instead of returning.
    """
    mode = _normalized_mode()
    secret = getattr(settings, "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET", None)
    incoming_sig = request.headers.get(POSTGRID_SIGNATURE_HEADER)

    result: dict[str, Any] = {
        "signature_mode": mode,
        "signature_verified": False,
        "signature_reason": "not_verified",
    }

    def _audit(reason: str, message: str, level: int = logging.WARNING) -> dict[str, Any]:
        incr_metric("webhook.signature.audit_failed", provider_slug="postgrid", reason=reason, mode=mode)
        log_event(
            "webhook_signature_audit_failed",
            level=level,
            request_id=request_id,
            provider_slug="postgrid",
            reason=reason,
            mode=mode,
            message=message,
        )
        result["signature_reason"] = reason
        return result

    if mode == "disabled":
        return _audit("disabled", "Signature verification disabled", level=logging.INFO)

    if not secret:
        if mode == "enforce":
            incr_metric("webhook.signature.enforce_config_error", provider_slug="postgrid")
            incr_metric(
                "webhook.events.rejected",
                provider_slug="postgrid",
                reason="signature_configuration_error",
            )
            log_event(
                "webhook_signature_enforce_config_error",
                level=logging.ERROR,
                request_id=request_id,
                provider_slug="postgrid",
                mode=mode,
                message="POSTGRID_PRINT_MAIL_WEBHOOK_SECRET required when mode=enforce",
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "type": "webhook_signature_configuration_error",
                    "provider": "postgrid",
                    "message": (
                        "Webhook signature enforcement is enabled but "
                        "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET is not configured"
                    ),
                },
            )
        return _audit("secret_not_configured", "POSTGRID_PRINT_MAIL_WEBHOOK_SECRET not set")

    if not incoming_sig:
        if mode == "enforce":
            incr_metric("webhook.signature.rejected", provider_slug="postgrid", reason="missing_signature")
            incr_metric("webhook.events.rejected", provider_slug="postgrid", reason="missing_signature")
            raise _invalid_signature(
                reason="missing_signature",
                message=f"Missing {POSTGRID_SIGNATURE_HEADER} header",
            )
        return _audit("missing_signature", f"Missing {POSTGRID_SIGNATURE_HEADER} header")

    expected = compute_postgrid_signature(raw_body, secret)
    if not hmac.compare_digest(expected, incoming_sig):
        if mode == "enforce":
            incr_metric("webhook.signature.rejected", provider_slug="postgrid", reason="invalid_signature")
            incr_metric("webhook.events.rejected", provider_slug="postgrid", reason="invalid_signature")
            raise _invalid_signature(
                reason="invalid_signature",
                message="PostGrid webhook signature verification failed",
            )
        return _audit("invalid_signature", "PostGrid webhook signature verification failed")

    incr_metric("webhook.signature.verified", provider_slug="postgrid", mode=mode)
    result["signature_verified"] = True
    result["signature_reason"] = "verified"
    return result


def validate_postgrid_payload_schema(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Validate that the payload has the required fields for projection.

    PostGrid webhook shape: {id, type, data: {object: {...}}, created_at}.
    We require: id (event id), type (event type string), data.object.id
    (resource id), and created_at.
    """
    event_id = payload.get("id")
    event_type = payload.get("type")
    data = payload.get("data") or {}
    resource_obj = data.get("object") if isinstance(data, dict) else None
    resource_id = resource_obj.get("id") if isinstance(resource_obj, dict) else None
    created_at = payload.get("created_at")

    missing = []
    if not event_id:
        missing.append("id")
    if not event_type:
        missing.append("type")
    if not resource_id:
        missing.append("data.object.id")
    if not created_at:
        missing.append("created_at")

    if missing:
        raise ValueError(f"schema_invalid:{','.join(missing)}")

    return "v1", {
        "event_id": str(event_id),
        "event_type": str(event_type),
        "resource_id": str(resource_id),
    }
