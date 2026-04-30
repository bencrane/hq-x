"""Customer-facing webhook subscription router tests.

Stubs the service layer so the router contract (validation, secret
exposure, cross-org guard, soft-delete semantics) is exercised without
DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.models.customer_webhooks import (
    CustomerWebhookDeliveryResponse,
    CustomerWebhookSubscriptionResponse,
    CustomerWebhookSubscriptionWithSecretResponse,
)
from app.routers import customer_webhooks as router_mod
from app.services import customer_webhooks as svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_OTHER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
USER = UUID("11111111-1111-1111-1111-111111111111")
SUB = UUID("99999999-9999-9999-9999-999999999999")
DELIVERY = UUID("88888888-8888-8888-8888-888888888888")


def _user(org_id: UUID | None = ORG) -> UserContext:
    return UserContext(
        auth_user_id=USER,
        business_user_id=USER,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


def _sub(*, organization_id: UUID = ORG, **kw: Any) -> CustomerWebhookSubscriptionResponse:
    now = datetime.now(UTC)
    return CustomerWebhookSubscriptionResponse(
        id=SUB,
        organization_id=organization_id,
        brand_id=kw.get("brand_id"),
        url=kw.get("url", "https://example.com/hook"),
        event_filter=kw.get("event_filter", ["*"]),
        state=kw.get("state", "active"),
        consecutive_failures=0,
        last_delivery_at=None,
        last_failure_at=None,
        last_failure_reason=None,
        created_at=now,
        updated_at=now,
    )


def _sub_with_secret(**kw: Any) -> CustomerWebhookSubscriptionWithSecretResponse:
    base = _sub(**kw)
    return CustomerWebhookSubscriptionWithSecretResponse(
        **base.model_dump(),
        secret="the-plaintext-secret-only-shown-once",
    )


def _delivery(*, status: str = "succeeded") -> CustomerWebhookDeliveryResponse:
    now = datetime.now(UTC)
    return CustomerWebhookDeliveryResponse(
        id=DELIVERY,
        subscription_id=SUB,
        event_name="page.submitted",
        event_payload={"organization_id": str(ORG)},
        attempt=1,
        status=status,
        response_status=200,
        response_body=None,
        attempted_at=now,
        next_retry_at=None,
    )


@pytest.fixture
def stub_svc(monkeypatch):
    state: dict[str, Any] = {
        "subs": {SUB: _sub()},
        "deliveries": {DELIVERY: _delivery()},
        "create_calls": [],
        "update_calls": [],
        "rotate_calls": [],
        "pause_calls": [],
        "list_deliveries_calls": [],
    }

    async def fake_create(*, organization_id, payload):
        state["create_calls"].append((organization_id, payload))
        return _sub_with_secret()

    async def fake_get(*, subscription_id, organization_id):
        sub = state["subs"].get(subscription_id)
        if sub is None or sub.organization_id != organization_id:
            raise svc.SubscriptionNotFound(f"sub {subscription_id}")
        return sub

    async def fake_list(*, organization_id):
        return [s for s in state["subs"].values() if s.organization_id == organization_id]

    async def fake_update(*, subscription_id, organization_id, payload):
        state["update_calls"].append((subscription_id, payload))
        sub = state["subs"].get(subscription_id)
        if sub is None or sub.organization_id != organization_id:
            raise svc.SubscriptionNotFound(f"sub {subscription_id}")
        # Apply updates in-memory.
        fields = payload.model_dump(exclude_unset=True)
        new = sub.model_copy(update=fields)
        state["subs"][subscription_id] = new
        return new

    async def fake_pause(*, subscription_id, organization_id):
        state["pause_calls"].append(subscription_id)
        return await fake_update(
            subscription_id=subscription_id,
            organization_id=organization_id,
            payload=router_mod.CustomerWebhookSubscriptionUpdate(state="paused"),
        )

    async def fake_rotate(*, subscription_id, organization_id):
        state["rotate_calls"].append(subscription_id)
        sub = state["subs"].get(subscription_id)
        if sub is None or sub.organization_id != organization_id:
            raise svc.SubscriptionNotFound(f"sub {subscription_id}")
        return CustomerWebhookSubscriptionWithSecretResponse(
            **sub.model_dump(), secret="rotated-secret"
        )

    async def fake_get_delivery(*, delivery_id, organization_id=None):
        d = state["deliveries"].get(delivery_id)
        if d is None:
            raise svc.DeliveryNotFound(f"delivery {delivery_id}")
        return d

    async def fake_list_deliveries(*, subscription_id, organization_id, limit):
        state["list_deliveries_calls"].append(subscription_id)
        return [d for d in state["deliveries"].values() if d.subscription_id == subscription_id]

    monkeypatch.setattr(svc, "create_subscription", fake_create)
    monkeypatch.setattr(svc, "get_subscription", fake_get)
    monkeypatch.setattr(svc, "list_subscriptions", fake_list)
    monkeypatch.setattr(svc, "update_subscription", fake_update)
    monkeypatch.setattr(svc, "pause_subscription", fake_pause)
    monkeypatch.setattr(svc, "rotate_secret", fake_rotate)
    monkeypatch.setattr(svc, "get_delivery", fake_get_delivery)
    monkeypatch.setattr(svc, "list_deliveries", fake_list_deliveries)
    monkeypatch.setattr(router_mod.svc, "create_subscription", fake_create)
    monkeypatch.setattr(router_mod.svc, "get_subscription", fake_get)
    monkeypatch.setattr(router_mod.svc, "list_subscriptions", fake_list)
    monkeypatch.setattr(router_mod.svc, "update_subscription", fake_update)
    monkeypatch.setattr(router_mod.svc, "pause_subscription", fake_pause)
    monkeypatch.setattr(router_mod.svc, "rotate_secret", fake_rotate)
    monkeypatch.setattr(router_mod.svc, "get_delivery", fake_get_delivery)
    monkeypatch.setattr(router_mod.svc, "list_deliveries", fake_list_deliveries)

    return state


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.request(method, path, **kwargs)


# ── Create ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_returns_secret_once(auth_org_a, stub_svc):
    resp = await _request(
        "POST",
        "/api/v1/dmaas/webhooks",
        json={
            "url": "https://example.com/hook",
            "event_filter": ["page.submitted", "step.completed"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"] == "the-plaintext-secret-only-shown-once"
    assert body["url"] == "https://example.com/hook"
    assert body["event_filter"] == ["*"]  # from _sub_with_secret default


@pytest.mark.asyncio
async def test_create_rejects_non_https_url(auth_org_a, stub_svc):
    resp = await _request(
        "POST",
        "/api/v1/dmaas/webhooks",
        json={"url": "ftp://example.com", "event_filter": ["*"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_empty_event_filter(auth_org_a, stub_svc):
    resp = await _request(
        "POST",
        "/api/v1/dmaas/webhooks",
        json={"url": "https://example.com", "event_filter": []},
    )
    assert resp.status_code == 422


# ── List + Get ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_org_subscriptions(auth_org_a, stub_svc):
    resp = await _request("GET", "/api/v1/dmaas/webhooks")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(SUB)


@pytest.mark.asyncio
async def test_list_excludes_other_orgs(auth_org_a, stub_svc):
    stub_svc["subs"][SUB] = _sub(organization_id=ORG_OTHER)
    resp = await _request("GET", "/api/v1/dmaas/webhooks")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_subscription_in_other_org_returns_404(auth_org_a, stub_svc):
    stub_svc["subs"][SUB] = _sub(organization_id=ORG_OTHER)
    resp = await _request("GET", f"/api/v1/dmaas/webhooks/{SUB}")
    assert resp.status_code == 404


# ── Update ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_updates_url(auth_org_a, stub_svc):
    resp = await _request(
        "PATCH",
        f"/api/v1/dmaas/webhooks/{SUB}",
        json={"url": "https://other.example.com/hook"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["url"] == "https://other.example.com/hook"


# ── Delete (soft) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_pauses_subscription(auth_org_a, stub_svc):
    resp = await _request("DELETE", f"/api/v1/dmaas/webhooks/{SUB}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "paused"
    assert SUB in stub_svc["pause_calls"]


# ── Rotate secret ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_returns_new_secret(auth_org_a, stub_svc):
    resp = await _request(
        "POST", f"/api/v1/dmaas/webhooks/{SUB}/rotate-secret"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["secret"] == "rotated-secret"


# ── Deliveries ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_deliveries(auth_org_a, stub_svc):
    resp = await _request("GET", f"/api/v1/dmaas/webhooks/{SUB}/deliveries")
    assert resp.status_code == 200
    assert len(resp.json()) == 1
