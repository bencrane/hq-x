"""Entri webhook signature verification — V2 HMAC-SHA256.

Per https://developers.entri.com/webhooks.md the V2 scheme is:

    sig = hex(SHA256(webhook_id + timestamp + client_secret))

where:
  * webhook_id     = payload["id"]
  * timestamp      = the `Entri-Timestamp` header (epoch seconds)
  * client_secret  = ENTRI_WEBHOOK_SECRET (from the Entri dashboard)
  * sig            = compared against the `Entri-Signature-V2` header

A 5-minute timestamp tolerance window guards replays. We deliberately do NOT
implement the V1 (`Entri-Signature`) legacy scheme — V2 is recommended.

Modes mirror dub_signature.py:
  - enforce            : reject anything that doesn't verify
  - permissive_audit   : audit + log failures, but accept the event
  - disabled           : do not verify at all (refused at boot in prd)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
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
            "provider": "entri",
            "reason": reason,
            "message": message,
        },
    )


def _normalized_mode() -> str:
    raw = (settings.ENTRI_WEBHOOK_SIGNATURE_MODE or "permissive_audit").strip().lower()
    return raw if raw in _VALID_MODES else "permissive_audit"


def _candidate_secret() -> str | None:
    if settings.ENTRI_WEBHOOK_SECRET is None:
        return None
    return settings.ENTRI_WEBHOOK_SECRET.get_secret_value()


def _timestamp_within_tolerance(timestamp: str, *, tolerance_seconds: int) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    now = int(time.time())
    return abs(now - ts) <= tolerance_seconds


def verify_entri_signature(
    *,
    payload: dict[str, Any],
    request: Request,
    request_id: str | None,
) -> dict[str, Any]:
    """Verify Entri-Signature-V2 against payload + headers.

    Unlike Dub (which signs the raw body), Entri signs (id || timestamp ||
    secret), so we need the parsed payload — the caller passes it in.
    """
    mode = _normalized_mode()
    secret = _candidate_secret()
    signature = request.headers.get("Entri-Signature-V2")
    timestamp = request.headers.get("Entri-Timestamp")
    webhook_id = payload.get("id")

    result: dict[str, Any] = {
        "signature_mode": mode,
        "signature_verified": False,
        "signature_reason": "not_verified",
    }

    def _audit(reason: str, message: str, level: int = logging.WARNING) -> dict[str, Any]:
        incr_metric(
            "webhook.signature.audit_failed",
            provider_slug="entri",
            reason=reason,
            mode=mode,
        )
        log_event(
            "webhook_signature_audit_failed",
            level=level,
            request_id=request_id,
            provider_slug="entri",
            reason=reason,
            mode=mode,
            message=message,
        )
        result["signature_reason"] = reason
        return result

    if mode == "disabled":
        return _audit("disabled", "Signature verification disabled", level=logging.INFO)

    if mode == "enforce" and not secret:
        incr_metric("webhook.signature.enforce_config_error", provider_slug="entri")
        incr_metric(
            "webhook.events.rejected",
            provider_slug="entri",
            reason="signature_configuration_error",
        )
        log_event(
            "webhook_signature_enforce_config_error",
            level=logging.ERROR,
            request_id=request_id,
            provider_slug="entri",
            mode=mode,
            message="ENTRI_WEBHOOK_SECRET required when mode=enforce",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "webhook_signature_configuration_error",
                "provider": "entri",
                "message": (
                    "Webhook signature enforcement is enabled but no secret is "
                    "configured (set ENTRI_WEBHOOK_SECRET)"
                ),
            },
        )

    if not secret:
        return _audit("secret_not_configured", "ENTRI_WEBHOOK_SECRET not configured")

    if not signature:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected",
                provider_slug="entri",
                reason="missing_signature",
            )
            raise _invalid_signature(
                reason="missing_signature",
                message="Missing Entri-Signature-V2 header",
            )
        return _audit("missing_signature", "Missing Entri-Signature-V2 header")

    if not timestamp:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected",
                provider_slug="entri",
                reason="missing_timestamp",
            )
            raise _invalid_signature(
                reason="missing_timestamp",
                message="Missing Entri-Timestamp header",
            )
        return _audit("missing_timestamp", "Missing Entri-Timestamp header")

    if not webhook_id:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected",
                provider_slug="entri",
                reason="missing_webhook_id",
            )
            raise _invalid_signature(
                reason="missing_webhook_id",
                message="Payload missing `id` field",
            )
        return _audit("missing_webhook_id", "Payload missing `id` field")

    tolerance = settings.ENTRI_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS
    if not _timestamp_within_tolerance(timestamp, tolerance_seconds=tolerance):
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected",
                provider_slug="entri",
                reason="timestamp_outside_tolerance",
            )
            raise _invalid_signature(
                reason="timestamp_outside_tolerance",
                message=f"Entri-Timestamp outside ±{tolerance}s window",
            )
        return _audit(
            "timestamp_outside_tolerance",
            f"Entri-Timestamp outside ±{tolerance}s window",
        )

    expected = hashlib.sha256(
        str(webhook_id).encode("utf-8")
        + timestamp.encode("utf-8")
        + secret.encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected",
                provider_slug="entri",
                reason="invalid_signature",
            )
            raise _invalid_signature(
                reason="invalid_signature",
                message="Entri webhook signature verification failed",
            )
        return _audit("invalid_signature", "Entri webhook signature verification failed")

    incr_metric("webhook.signature.verified", provider_slug="entri", mode=mode)
    result["signature_verified"] = True
    result["signature_reason"] = "verified"
    return result


_KNOWN_EVENT_TYPES = {
    "domain.added",
    "domain.purchased",
    "domain.propagation.timeout",
    "domain.flow.completed",
    "domain.record_missing",
    "domain.record_restored",
    "domain.transfer.in.initiated",
    "domain.transfer.in.started",
    "domain.transfer.in.ack",
    "domain.transfer.in.failed",
    "domain.transfer.out.initiated",
    "purchase.error",
    "purchase.confirmation.expired",
}


def validate_entri_payload_schema(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Confirm the webhook envelope has the fields the projector needs.

    Per docs the canonical fields are: id, type, user_id, domain. `type` is
    one of `_KNOWN_EVENT_TYPES` (we accept unknown types as v1 too — they
    land in webhook_events as inert rows; only the known set drives
    state changes).
    """
    event_id = payload.get("id")
    event_type = payload.get("type")
    if not event_id:
        raise ValueError("schema_invalid:id")
    if not event_type:
        raise ValueError("schema_invalid:type")
    return "v1", {
        "event_id": str(event_id),
        "event_type": str(event_type),
        "event_known": "true" if event_type in _KNOWN_EVENT_TYPES else "false",
    }
