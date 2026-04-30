"""Tests for /internal/customer-webhooks/deliver."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.main import app
from app.models.customer_webhooks import CustomerWebhookDeliveryResponse
from app.routers.internal import customer_webhooks as router_mod
from app.services import customer_webhooks as cw_svc

SUB = UUID("99999999-9999-9999-9999-999999999999")
DELIVERY = UUID("88888888-8888-8888-8888-888888888888")


def _delivery(**kw: Any) -> CustomerWebhookDeliveryResponse:
    now = datetime.now(UTC)
    base = dict(
        id=DELIVERY,
        subscription_id=SUB,
        event_name="page.submitted",
        event_payload={
            "organization_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "form_data": {"name": "Joe"},
        },
        attempt=1,
        status="pending",
        response_status=None,
        response_body=None,
        attempted_at=now,
        next_retry_at=None,
    )
    base.update(kw)
    return CustomerWebhookDeliveryResponse(**base)


@pytest.fixture
def stub_delivery_state(monkeypatch):
    state: dict[str, Any] = {
        "delivery": _delivery(),
        "dispatch": ("https://customer.example.com/hook", "the-secret", "active"),
        "succeeded_calls": [],
        "failed_calls": [],
        "http_status": 200,
        "http_body": "ok",
        "http_should_timeout": False,
    }

    async def fake_get_delivery(*, delivery_id, organization_id=None):
        if delivery_id != state["delivery"].id:
            raise cw_svc.DeliveryNotFound(f"delivery {delivery_id}")
        return state["delivery"]

    async def fake_get_dispatch(*, subscription_id):
        return state["dispatch"]

    async def fake_succeeded(**kwargs):
        state["succeeded_calls"].append(kwargs)
        state["delivery"] = _delivery(status="succeeded", **{
            k: v for k, v in kwargs.items() if k in ("response_status", "response_body")
        })

    async def fake_failed(**kwargs):
        state["failed_calls"].append(kwargs)
        state["delivery"] = _delivery(status="failed", attempt=state["delivery"].attempt + 1)
        return state["delivery"]

    monkeypatch.setattr(cw_svc, "get_delivery", fake_get_delivery)
    monkeypatch.setattr(cw_svc, "get_subscription_dispatch_view", fake_get_dispatch)
    monkeypatch.setattr(cw_svc, "mark_delivery_succeeded", fake_succeeded)
    monkeypatch.setattr(cw_svc, "mark_delivery_failed", fake_failed)
    monkeypatch.setattr(router_mod.cw_svc, "get_delivery", fake_get_delivery)
    monkeypatch.setattr(
        router_mod.cw_svc, "get_subscription_dispatch_view", fake_get_dispatch
    )
    monkeypatch.setattr(router_mod.cw_svc, "mark_delivery_succeeded", fake_succeeded)
    monkeypatch.setattr(router_mod.cw_svc, "mark_delivery_failed", fake_failed)

    async def fake_http_post(*, url, body, headers):
        if state["http_should_timeout"]:
            raise httpx.TimeoutException("timeout")
        return state["http_status"], state["http_body"]

    monkeypatch.setattr(router_mod, "_http_post", fake_http_post)
    return state


async def _post(body: dict[str, Any], *, secret: str = "test-trigger-secret") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.post(
            "/internal/customer-webhooks/deliver",
            json=body,
            headers={"Authorization": f"Bearer {secret}"},
        )


# ── Auth ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_requires_secret(stub_delivery_state):
    resp = await _post({"delivery_id": str(DELIVERY)}, secret="wrong")
    assert resp.status_code == 401


# ── Happy path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_signs_and_succeeds(stub_delivery_state):
    resp = await _post({"delivery_id": str(DELIVERY)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["response_status"] == 200
    assert len(stub_delivery_state["succeeded_calls"]) == 1
    assert stub_delivery_state["succeeded_calls"][0]["response_status"] == 200


# ── Failure modes ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_5xx_marks_failed(stub_delivery_state):
    stub_delivery_state["http_status"] = 500
    stub_delivery_state["http_body"] = "internal error"
    resp = await _post({"delivery_id": str(DELIVERY)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["reason"] == "http_500"
    assert len(stub_delivery_state["failed_calls"]) == 1


@pytest.mark.asyncio
async def test_deliver_timeout_marks_failed(stub_delivery_state):
    stub_delivery_state["http_should_timeout"] = True
    resp = await _post({"delivery_id": str(DELIVERY)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["reason"] == "timeout"


@pytest.mark.asyncio
async def test_deliver_paused_subscription_marks_failed(stub_delivery_state):
    stub_delivery_state["dispatch"] = (
        "https://example.com/hook",
        "the-secret",
        "paused",
    )
    resp = await _post({"delivery_id": str(DELIVERY)})
    body = resp.json()
    assert body["status"] == "failed"
    assert body["reason"] == "subscription_paused"


@pytest.mark.asyncio
async def test_deliver_terminal_status_skipped(stub_delivery_state):
    stub_delivery_state["delivery"] = _delivery(status="succeeded")
    resp = await _post({"delivery_id": str(DELIVERY)})
    body = resp.json()
    assert body["skipped"] is True
    # No re-fire.
    assert stub_delivery_state["succeeded_calls"] == []


@pytest.mark.asyncio
async def test_deliver_dead_lettered_skipped(stub_delivery_state):
    stub_delivery_state["delivery"] = _delivery(status="dead_lettered")
    resp = await _post({"delivery_id": str(DELIVERY)})
    body = resp.json()
    assert body["skipped"] is True


@pytest.mark.asyncio
async def test_deliver_missing_delivery_id_400(stub_delivery_state):
    resp = await _post({})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_deliver_unknown_delivery_404(stub_delivery_state, monkeypatch):
    async def boom(*, delivery_id, organization_id=None):
        raise cw_svc.DeliveryNotFound(f"delivery {delivery_id}")

    monkeypatch.setattr(cw_svc, "get_delivery", boom)
    monkeypatch.setattr(router_mod.cw_svc, "get_delivery", boom)
    resp = await _post({"delivery_id": str(DELIVERY)})
    assert resp.status_code == 404
