"""Lob webhook signature verification — HMAC-SHA256 over `{ts}.{body}`.

Modes (set via LOB_WEBHOOK_SIGNATURE_MODE):
  - enforce            : reject anything that doesn't verify
  - permissive_audit   : audit + log failures, but accept the event
  - disabled           : do not verify at all (insecure; refused at boot in prd)

Schema validation also lives here because callers want one place that says
"is this Lob webhook payload safe to process?"
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, Request, status

from app.config import settings
from app.observability import incr_metric, log_event
from app.webhooks.lob_normalization import extract_lob_event_name, extract_lob_piece_id

_VALID_MODES = {"enforce", "permissive_audit", "disabled"}


def _invalid_signature(*, reason: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={
            "type": "webhook_signature_invalid",
            "provider": "lob",
            "reason": reason,
            "message": message,
        },
    )


def parse_lob_timestamp(raw_timestamp: str) -> datetime | None:
    text = str(raw_timestamp).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            return datetime.fromtimestamp(int(text), tz=UTC)
        except (ValueError, OSError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def supported_lob_versions() -> set[str]:
    raw = settings.LOB_WEBHOOK_SCHEMA_VERSIONS or "v1"
    versions = {item.strip() for item in raw.split(",") if item.strip()}
    return versions or {"v1"}


def extract_lob_payload_version(payload: dict[str, Any]) -> str:
    for key in ("version", "webhook_version", "schema_version"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "v1"


def validate_lob_payload_schema(payload: dict[str, Any]) -> tuple[str, dict[str, str]]:
    """Confirm the payload has the fields the projector needs.

    Lob's documented webhook shape: {id, event_type:{id,resource,...},
    reference_id, date_created, body, object}. We require id (evt_xxx),
    the event name (event_type.id), date_created, and a resolvable piece
    id (reference_id or body.id).
    """
    version = extract_lob_payload_version(payload)
    if version not in supported_lob_versions():
        raise ValueError(f"version_unsupported:{version}")
    event_id = payload.get("id") or payload.get("event_id")
    event_name = extract_lob_event_name(payload)
    event_ts = payload.get("date_created") or payload.get("created_at") or payload.get("time")
    resource_id = extract_lob_piece_id(payload)
    missing = []
    if not event_id:
        missing.append("id")
    if not event_name:
        missing.append("event_type.id")
    if not event_ts:
        missing.append("date_created")
    if not resource_id:
        missing.append("reference_id")
    if missing:
        raise ValueError(f"schema_invalid:{','.join(missing)}")
    return version, {
        "event_id": str(event_id),
        "event_type": event_name,
        "resource_id": resource_id,
    }


def _normalized_mode() -> str:
    raw = (settings.LOB_WEBHOOK_SIGNATURE_MODE or "permissive_audit").strip().lower()
    return raw if raw in _VALID_MODES else "permissive_audit"


def _candidate_secrets() -> list[tuple[str, str]]:
    """Return [(environment_label, secret), ...] for whichever secrets are set.

    Lob runs separate live and test webhook subscriptions, each with its
    own signing secret. We try LIVE first (matches the prd-traffic case),
    then TEST as fallback for test-mode pieces.
    """
    out: list[tuple[str, str]] = []
    if settings.LOB_WEBHOOKS_SECRET_LIVE:
        out.append(("live", settings.LOB_WEBHOOKS_SECRET_LIVE))
    if settings.LOB_WEBHOOKS_SECRET_TEST:
        out.append(("test", settings.LOB_WEBHOOKS_SECRET_TEST))
    return out


def verify_lob_signature(
    *,
    raw_body: bytes,
    request: Request,
    request_id: str | None,
) -> dict[str, Any]:
    mode = _normalized_mode()
    tolerance = max(0, int(settings.LOB_WEBHOOK_SIGNATURE_TOLERANCE_SECONDS or 0))
    candidates = _candidate_secrets()
    signature = request.headers.get("Lob-Signature")
    timestamp_header = request.headers.get("Lob-Signature-Timestamp")

    result: dict[str, Any] = {
        "signature_mode": mode,
        "signature_verified": False,
        "signature_environment": None,
        "signature_reason": "not_verified",
        "signature_timestamp": timestamp_header,
    }

    def _audit(reason: str, message: str, level: int = logging.WARNING) -> dict[str, Any]:
        incr_metric("webhook.signature.audit_failed", provider_slug="lob", reason=reason, mode=mode)
        log_event(
            "webhook_signature_audit_failed",
            level=level,
            request_id=request_id,
            provider_slug="lob",
            reason=reason,
            mode=mode,
            message=message,
        )
        result["signature_reason"] = reason
        return result

    if mode == "disabled":
        return _audit("disabled", "Signature verification disabled", level=logging.INFO)

    if mode == "enforce" and not candidates:
        incr_metric("webhook.signature.enforce_config_error", provider_slug="lob")
        incr_metric(
            "webhook.events.rejected", provider_slug="lob", reason="signature_configuration_error"
        )
        log_event(
            "webhook_signature_enforce_config_error",
            level=logging.ERROR,
            request_id=request_id,
            provider_slug="lob",
            mode=mode,
            message="LOB_WEBHOOKS_SECRET_LIVE/_TEST required when mode=enforce",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "webhook_signature_configuration_error",
                "provider": "lob",
                "message": (
                    "Webhook signature enforcement is enabled but no secret is configured "
                    "(set LOB_WEBHOOKS_SECRET_LIVE and/or LOB_WEBHOOKS_SECRET_TEST)"
                ),
            },
        )

    if not candidates:
        return _audit("secret_not_configured", "No Lob webhook secrets configured")

    if not signature:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="lob", reason="missing_signature"
            )
            incr_metric("webhook.events.rejected", provider_slug="lob", reason="missing_signature")
            raise _invalid_signature(
                reason="missing_signature", message="Missing Lob-Signature header"
            )
        return _audit("missing_signature", "Missing Lob-Signature header")

    if not timestamp_header:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="lob", reason="missing_timestamp"
            )
            incr_metric("webhook.events.rejected", provider_slug="lob", reason="missing_timestamp")
            raise _invalid_signature(
                reason="missing_timestamp", message="Missing Lob-Signature-Timestamp header"
            )
        return _audit("missing_timestamp", "Missing Lob-Signature-Timestamp header")

    parsed_ts = parse_lob_timestamp(timestamp_header)
    if parsed_ts is None:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="lob", reason="invalid_timestamp"
            )
            incr_metric("webhook.events.rejected", provider_slug="lob", reason="invalid_timestamp")
            raise _invalid_signature(
                reason="invalid_timestamp", message="Invalid Lob-Signature-Timestamp format"
            )
        return _audit("invalid_timestamp", "Invalid Lob-Signature-Timestamp format")

    age = abs((datetime.now(UTC) - parsed_ts).total_seconds())
    if tolerance > 0 and age > tolerance:
        if mode == "enforce":
            incr_metric("webhook.signature.rejected", provider_slug="lob", reason="stale_timestamp")
            incr_metric("webhook.events.rejected", provider_slug="lob", reason="stale_timestamp")
            raise _invalid_signature(
                reason="stale_timestamp",
                message="Lob-Signature-Timestamp is outside accepted tolerance window",
            )
        return _audit("stale_timestamp", "Lob-Signature-Timestamp outside tolerance window")

    # Try each candidate secret. First match wins.
    signature_input = f"{timestamp_header}.{raw_body.decode('utf-8', errors='strict')}"
    matched_environment: str | None = None
    for env_label, secret in candidates:
        expected = hmac.new(
            secret.encode("utf-8"), signature_input.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, signature):
            matched_environment = env_label
            break

    if matched_environment is None:
        if mode == "enforce":
            incr_metric(
                "webhook.signature.rejected", provider_slug="lob", reason="invalid_signature"
            )
            incr_metric("webhook.events.rejected", provider_slug="lob", reason="invalid_signature")
            raise _invalid_signature(
                reason="invalid_signature", message="Lob webhook signature verification failed"
            )
        return _audit("invalid_signature", "Lob webhook signature verification failed")

    incr_metric(
        "webhook.signature.verified",
        provider_slug="lob",
        mode=mode,
        environment=matched_environment,
    )
    result["signature_environment"] = matched_environment
    result["signature_verified"] = True
    result["signature_reason"] = "verified"
    return result
