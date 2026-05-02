"""Tests for the /internal/dmaas/reconcile/* endpoints."""

from __future__ import annotations

import httpx
import pytest

from app.main import app
from app.routers.internal import dmaas_reconcile as router_mod
from app.services.reconciliation import ReconciliationResult


async def _post(path: str, *, secret: str = "test-trigger-secret") -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", timeout=30.0
    ) as c:
        return await c.post(path, headers={"Authorization": f"Bearer {secret}"})


@pytest.fixture
def stub_reconcilers(monkeypatch):
    state = {"calls": []}

    def make_fake(name: str):
        async def fake_reconcile(**kwargs):
            state["calls"].append(name)
            return ReconciliationResult(rows_scanned=1, rows_touched=1, drift_found=0)
        return fake_reconcile

    monkeypatch.setattr(router_mod.r_stale, "reconcile", make_fake("stale"))
    monkeypatch.setattr(router_mod.r_lob, "reconcile", make_fake("lob"))
    monkeypatch.setattr(router_mod.r_dub, "reconcile", make_fake("dub"))
    monkeypatch.setattr(router_mod.r_wh, "reconcile", make_fake("webhook_replays"))
    monkeypatch.setattr(router_mod.r_cw, "reconcile", make_fake("customer_webhooks"))
    return state


@pytest.mark.asyncio
async def test_each_endpoint_dispatches_to_correct_reconciler(stub_reconcilers):
    paths = [
        ("/internal/dmaas/reconcile/stale-jobs", "stale"),
        ("/internal/dmaas/reconcile/lob", "lob"),
        ("/internal/dmaas/reconcile/dub", "dub"),
        ("/internal/dmaas/reconcile/webhook-replays", "webhook_replays"),
        ("/internal/dmaas/reconcile/customer-webhook-deliveries", "customer_webhooks"),
    ]
    for path, expected in paths:
        resp = await _post(path)
        assert resp.status_code == 200, f"{path}: {resp.text}"
        body = resp.json()
        assert body["rows_scanned"] == 1
        assert stub_reconcilers["calls"][-1] == expected


@pytest.mark.asyncio
async def test_endpoints_reject_wrong_secret(stub_reconcilers):
    resp = await _post(
        "/internal/dmaas/reconcile/stale-jobs", secret="wrong-secret"
    )
    assert resp.status_code == 401
