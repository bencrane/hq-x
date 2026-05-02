"""Verify emit_event fans out to matching customer webhook subscriptions."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.models.customer_webhooks import (
    CustomerWebhookDeliveryResponse,
    CustomerWebhookSubscriptionResponse,
)
from app.services import analytics as analytics_svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SUB = UUID("99999999-9999-9999-9999-999999999999")
DELIVERY = UUID("88888888-8888-8888-8888-888888888888")


def _sub(
    *, event_filter: list[str], brand_id: UUID | None = None
) -> CustomerWebhookSubscriptionResponse:
    now = datetime.now(UTC)
    return CustomerWebhookSubscriptionResponse(
        id=SUB,
        organization_id=ORG,
        brand_id=brand_id,
        url="https://example.com/hook",
        event_filter=event_filter,
        state="active",
        consecutive_failures=0,
        last_delivery_at=None,
        last_failure_at=None,
        last_failure_reason=None,
        created_at=now,
        updated_at=now,
    )


def _delivery() -> CustomerWebhookDeliveryResponse:
    now = datetime.now(UTC)
    return CustomerWebhookDeliveryResponse(
        id=DELIVERY,
        subscription_id=SUB,
        event_name="page.submitted",
        event_payload={"organization_id": str(ORG)},
        attempt=1,
        status="pending",
        response_status=None,
        response_body=None,
        attempted_at=now,
        next_retry_at=None,
    )


@pytest.fixture
def stub_event_pipeline(monkeypatch):
    state: dict[str, Any] = {
        "matches": [],
        "enqueue_delivery_calls": [],
        "trigger_calls": [],
        "trigger_should_raise": False,
    }

    async def fake_resolve_step(step_id):
        return {
            "organization_id": str(ORG),
            "brand_id": str(BRAND),
            "campaign_id": "00000000-0000-0000-0000-000000000001",
            "channel_campaign_id": "00000000-0000-0000-0000-000000000002",
            "channel_campaign_step_id": str(step_id),
            "channel": "direct_mail",
            "provider": "lob",
        }

    monkeypatch.setattr(analytics_svc, "resolve_step_context", fake_resolve_step)

    from app.services import customer_webhooks as cw_svc

    async def fake_find_matching(*, organization_id, event_name, brand_id):
        return state["matches"]

    async def fake_enqueue_delivery(*, subscription_id, event_name, event_payload):
        state["enqueue_delivery_calls"].append(
            {
                "subscription_id": subscription_id,
                "event_name": event_name,
                "event_payload": event_payload,
            }
        )
        return _delivery()

    monkeypatch.setattr(cw_svc, "find_matching_subscriptions", fake_find_matching)
    monkeypatch.setattr(cw_svc, "enqueue_delivery", fake_enqueue_delivery)

    from app.services import activation_jobs as jobs_svc

    async def fake_enqueue_via_trigger(**kwargs):
        state["trigger_calls"].append(kwargs)
        if state["trigger_should_raise"]:
            raise jobs_svc.TriggerEnqueueError("trigger.dev down")
        return "run_xyz"

    monkeypatch.setattr(jobs_svc, "enqueue_via_trigger", fake_enqueue_via_trigger)

    return state


@pytest.mark.asyncio
async def test_emit_event_fanout_enqueues_delivery_for_matching_sub(stub_event_pipeline):
    stub_event_pipeline["matches"] = [_sub(event_filter=["page.submitted"])]
    await analytics_svc.emit_event(
        event_name="page.submitted",
        channel_campaign_step_id=UUID("ccccccc1-cccc-cccc-cccc-cccccccccccc"),
        properties={"form_data": {"name": "Joe"}},
    )
    assert len(stub_event_pipeline["enqueue_delivery_calls"]) == 1
    enqueued = stub_event_pipeline["enqueue_delivery_calls"][0]
    assert enqueued["event_name"] == "page.submitted"
    assert enqueued["event_payload"]["form_data"] == {"name": "Joe"}
    # Trigger.dev call also fired.
    assert len(stub_event_pipeline["trigger_calls"]) == 1
    assert stub_event_pipeline["trigger_calls"][0]["task_identifier"] == "customer_webhook.deliver"


@pytest.mark.asyncio
async def test_emit_event_skips_when_no_subscriptions(stub_event_pipeline):
    stub_event_pipeline["matches"] = []
    await analytics_svc.emit_event(
        event_name="page.submitted",
        channel_campaign_step_id=UUID("ccccccc1-cccc-cccc-cccc-cccccccccccc"),
    )
    assert stub_event_pipeline["enqueue_delivery_calls"] == []
    assert stub_event_pipeline["trigger_calls"] == []


@pytest.mark.asyncio
async def test_emit_event_swallows_trigger_errors(stub_event_pipeline):
    stub_event_pipeline["matches"] = [_sub(event_filter=["*"])]
    stub_event_pipeline["trigger_should_raise"] = True
    # Must not raise — webhook fanout is fire-and-forget.
    await analytics_svc.emit_event(
        event_name="page.submitted",
        channel_campaign_step_id=UUID("ccccccc1-cccc-cccc-cccc-cccccccccccc"),
    )
    # Delivery row still created so reconciliation can pick it up.
    assert len(stub_event_pipeline["enqueue_delivery_calls"]) == 1
