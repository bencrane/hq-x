"""Dub webhook signature verification — HMAC-SHA256 over the raw body.

Dub sends `Dub-Signature: <hex>` where `hex = HMAC_SHA256(secret, raw_body)`.
There is no signed timestamp in the protocol (unlike Lob), so there is no
replay window — the only protection against replay is `webhook_events`'
`(provider_slug, event_key)` unique index, where `event_key = event id`.

Modes (set via DUB_WEBHOOK_SIGNATURE_MODE):
  - enforce            : reject anything that doesn't verify
  - permissive_audit   : audit + log failures, but accept the event
  - disabled           : do not verify at all (insecure; refused at boot in prd)
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


def _invalid_signature(*, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "type": "webhook_signature_invalid",
            "provider": "dub",
            "reason": reason,
            "message": message,
        },
    )


def _normalized_mode() -> str:
    raw = (settings.DUB_WEBHOOK_SIGNATURE_MODE or "permissive_audit").strip().lower()
    return raw if raw in _VALID_MODES else "permissive_audit"


def _candidate_secret() -> str | None:
    if settings.DUB_WEBHOOK_SECRET is None:
        return None
    return settings.DUB_WEBHOOK_SECRET.get_secret_value()


def verify_dub_signature(
    *,
    raw_body: bytes,
    request: Request,
    request_id: str | None,
) -> dict[str, Any]:
    mode = _normalized_mode()
    secret = _candidate_secret()
    signature = request.headers.get("Dub-Signature")

    result: dict[str, Any] = {
        "signature_mode": mode,
        "signature_verified": False,
        "signature_reason": "not_verified",
    }

    def _audit(reason: str, message: str, level: int = logging.WARNING) -> dict[str, Any]:
        incr_metric("webhook.signature.audit_failed", provider_slug="dub", reason=reason, mode=mode)
        log_event(
            "webhook_signature_audit_failed",
            level=level,
            request_id=request_id,
            provider_slug="dub",
            reason=reason,
            mode=mode,
            message=message,
        )
        result["signature_reason"] = reason
        return result

    if mode == "disabled":
        return _audit("disabled", "Signature verification disabled", level=logging.INFO)

    if mode == "enforce" and not secret:
        incr_metric("webhook.signature.enforce_config_error", provider_slug="dub")
        incr_metric(
            "webhook.events.rejected", provider_slug="dub", reason="signature_configuration_error"
        )
        log_event(
            "webhook_signature_enforce_config_error",
            level=logging.ERROR,
            request_id=request_id,
            provider_slug="dub",
            mode=mode,
            message="DUB_WEBHOOK_SECRET required when mode=enforce",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "webhook_signature_configuration_error",
                "provider": "dub",
                "message": (
                    "Webhook signature enforcement is enabled but no secret is "
                    "configured (set DUB_WEBHOOK_SECRET)"
                ),
            },
        )

    if not secret:
        return _audit("secret_not_configured", "DUB_WEBHOOK_SECRET not configured")

    if not signature:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="dub", reason="missing_signature"
            )
            incr_metric("webhook.events.rejected", provider_slug="dub", reason="missing_signature")
            raise _invalid_signature(
                reason="missing_signature", message="Missing Dub-Signature header"
            )
        return _audit("missing_signature", "Missing Dub-Signature header")

    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="dub", reason="invalid_signature"
            )
            incr_metric("webhook.events.rejected", provider_slug="dub", reason="invalid_signature")
            raise _invalid_signature(
                reason="invalid_signature", message="Dub webhook signature verification failed"
            )
        return _audit("invalid_signature", "Dub webhook signature verification failed")

    incr_metric("webhook.signature.verified", provider_slug="dub", mode=mode)
    result["signature_verified"] = True
    result["signature_reason"] = "verified"
    return result


_KNOWN_EVENT_TYPES = {"link.clicked", "lead.created", "sale.created"}


def validate_dub_payload_schema(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Confirm the webhook envelope has the fields the projector needs.

    Dub's documented webhook shape: {id, event, createdAt, data: {...}}.
    `event` is one of {link.clicked, lead.created, sale.created}.
    """
    event_id = payload.get("id")
    event_type = payload.get("event")
    created_at = payload.get("createdAt") or payload.get("created_at")
    data = payload.get("data")

    missing: list[str] = []
    if not event_id:
        missing.append("id")
    if not event_type:
        missing.append("event")
    if not created_at:
        missing.append("createdAt")
    if not isinstance(data, dict):
        missing.append("data")
    if missing:
        raise ValueError(f"schema_invalid:{','.join(missing)}")

    if event_type not in _KNOWN_EVENT_TYPES:
        raise ValueError(f"event_type_unknown:{event_type}")

    return "v1", {
        "event_id": str(event_id),
        "event_type": str(event_type),
        "created_at": str(created_at),
    }
