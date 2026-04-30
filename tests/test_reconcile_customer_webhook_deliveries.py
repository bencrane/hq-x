"""Tests for the customer-webhook-deliveries reconciler."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.config import settings
from app.models.customer_webhooks import CustomerWebhookDeliveryResponse
from app.services import activation_jobs as jobs_svc
from app.services import customer_webhooks as cw_svc
from app.services.reconciliation import (
    customer_webhook_deliveries as r_cwd,
)

DELIVERY_A = UUID("aaaaaaa1-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DELIVERY_B = UUID("aaaaaaa2-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SUB = UUID("99999999-9999-9999-9999-999999999999")


def _delivery(*, delivery_id: UUID) -> CustomerWebhookDeliveryResponse:
    now = datetime.now(UTC)
    return CustomerWebhookDeliveryResponse(
        id=delivery_id,
        subscription_id=SUB,
        event_name="page.viewed",
        event_payload={"organization_id": "x"},
        attempt=2,
        status="pending",
        response_status=503,
        response_body=None,
        attempted_at=now,
        next_retry_at=now,
    )


@pytest.fixture
def stub_state(monkeypatch):
    state: dict[str, Any] = {
        "pending": [_delivery(delivery_id=DELIVERY_A), _delivery(delivery_id=DELIVERY_B)],
        "enqueue_calls": [],
        "trigger_should_raise": False,
    }

    async def fake_find(*, limit: int = 100):
        return state["pending"]

    async def fake_enqueue(**kwargs):
        state["enqueue_calls"].append(kwargs)
        if state["trigger_should_raise"]:
            raise jobs_svc.TriggerEnqueueError("trigger.dev down")
        return "run_x"

    monkeypatch.setattr(cw_svc, "find_pending_due_deliveries", fake_find)
    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue)
    monkeypatch.setattr(r_cwd.cw_svc, "find_pending_due_deliveries", fake_find)
    monkeypatch.setattr(r_cwd.jobs_svc, "enqueue_via_trigger", fake_enqueue)
    return state


@pytest.mark.asyncio
async def test_reconcile_re_enqueues_pending_deliveries(stub_state, monkeypatch):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED", True)
    result = await r_cwd.reconcile()
    assert result.rows_scanned == 2
    assert result.rows_touched == 2
    assert len(stub_state["enqueue_calls"]) == 2
    for call in stub_state["enqueue_calls"]:
        assert call["task_identifier"] == "customer_webhook.deliver"
        assert "delivery_id" in call["payload_override"]


@pytest.mark.asyncio
async def test_reconcile_records_drift_on_enqueue_failure(stub_state, monkeypatch):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED", True)
    stub_state["trigger_should_raise"] = True
    result = await r_cwd.reconcile()
    assert result.rows_scanned == 2
    assert result.rows_touched == 0
    assert result.drift_found == 2


@pytest.mark.asyncio
async def test_reconcile_disabled_short_circuits(stub_state, monkeypatch):
    monkeypatch.setattr(settings, "DMAAS_RECONCILE_CUSTOMER_WEBHOOKS_ENABLED", False)
    result = await r_cwd.reconcile()
    assert result.enabled is False
    assert result.rows_scanned == 0
    assert stub_state["enqueue_calls"] == []
