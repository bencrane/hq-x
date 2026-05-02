"""Project an Entri webhook event onto entri_domain_connections.

Webhook payloads land in `webhook_events` (provider_slug='entri'); this
module is the state-machine that flips connection rows in response.

State transitions:

  pending_modal       -- domain.flow.completed -->   dns_records_submitted
  *                   -- domain.added (+power_status=success
                                       +secure_status=success) -->  live
  *                   -- domain.propagation.timeout -->            failed
  live                -- domain.record_missing -->                 failed
  failed              -- domain.record_restored -->                live

Other event types (purchase.*, transfer.*) are recorded but don't drive
state — DMaaS doesn't use Entri's domain-purchase or transfer flows yet.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.dmaas import entri_domains
from app.observability import incr_metric, log_event

_TERMINAL_STATES = {"live", "failed", "disconnected"}


def _is_success(payload: dict[str, Any]) -> bool:
    """`domain.added` is fully successful only when power+secure both succeeded."""
    power = payload.get("power_status")
    secure = payload.get("secure_status")
    return power in (None, "success", "exempt") and secure in (None, "success", "exempt")


async def project_entri_event(
    *,
    payload: dict[str, Any],
    event_id: str,
    event_type: str,
    webhook_event_id: UUID,
) -> dict[str, Any]:
    """Apply the event to its corresponding domain connection.

    Correlates by `user_id` (which we set to "<org_id>:<step_id>"). If we
    can't resolve the row we still return success — Entri sometimes fires
    purchase events for flows we didn't initiate (e.g. dashboard-driven).
    """
    user_id = payload.get("user_id")
    if not user_id:
        log_event(
            "entri_webhook_no_user_id",
            event_key=event_id,
            event_type=event_type,
        )
        return {"status": "no_user_id", "event_type": event_type}

    connection = await entri_domains.get_by_entri_user_id(str(user_id))
    if connection is None:
        log_event(
            "entri_webhook_connection_not_found",
            event_key=event_id,
            event_type=event_type,
            entri_user_id=user_id,
        )
        return {"status": "connection_not_found", "event_type": event_type}

    # Pull whatever fields Entri included into the projection columns.
    common_fields: dict[str, Any] = {
        "provider": payload.get("provider"),
        "setup_type": payload.get("setup_type"),
        "propagation_status": payload.get("propagation_status"),
        "power_status": payload.get("power_status"),
        "secure_status": payload.get("secure_status"),
        "last_webhook_id": event_id,
    }

    next_state: str | None = None
    last_error: str | None = None

    if event_type == "domain.flow.completed":
        if connection.state == "pending_modal":
            next_state = "dns_records_submitted"

    elif event_type == "domain.added":
        if _is_success(payload):
            next_state = "live"

    elif event_type == "domain.propagation.timeout":
        next_state = "failed"
        last_error = "propagation_timeout"

    elif event_type == "domain.record_missing":
        if connection.state == "live":
            next_state = "failed"
            last_error = "record_missing"

    elif event_type == "domain.record_restored":
        if connection.state == "failed":
            next_state = "live"
            last_error = ""  # explicit clear

    # Don't move terminal-into-different-terminal except via the explicit
    # transitions above.
    if next_state and connection.state in _TERMINAL_STATES and next_state == connection.state:
        next_state = None

    updated = await entri_domains.update_state(
        connection.id,
        state=next_state,
        last_error=last_error if last_error is not None else None,
        **common_fields,
    )

    incr_metric(
        "entri.event.projected",
        event_type=event_type,
        new_state=(updated.state if updated else connection.state),
    )

    return {
        "status": "projected",
        "event_type": event_type,
        "connection_id": str(connection.id),
        "previous_state": connection.state,
        "new_state": updated.state if updated else connection.state,
        "webhook_event_id": str(webhook_event_id),
    }
