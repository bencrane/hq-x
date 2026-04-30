"""REST endpoint tests for /api/v1/entri/*.

Auth is bypassed via dependency_overrides; the Entri HTTP client and
entri_domains repository are monkeypatched so the suite has no DB or
network dependency.

The most important guarantee while we're unsigned-up: with
ENTRI_APPLICATION_ID unset, every endpoint returns 503
`entri_not_configured`. The same surface flips on transparently once
credentials land in Doppler.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.config import settings
from app.dmaas import entri_domains
from app.dmaas.entri_domains import EntriDomainConnection
from app.main import app
from app.providers.entri import client as entri_client
from app.routers import entri as entri_router

_ORG_ID = UUID("66666666-6666-6666-6666-666666666666")
_USER = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    email="user@example.com",
    platform_role=None,
    active_organization_id=_ORG_ID,
    org_role="admin",
    role="client",
    client_id=None,
)


@pytest.fixture
def auth_user():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _USER
    app.dependency_overrides[require_org_context] = lambda: _USER
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def configured(monkeypatch):
    """Set Entri credentials so endpoints don't 503."""
    monkeypatch.setattr(settings, "ENTRI_APPLICATION_ID", "app_test")
    monkeypatch.setattr(settings, "ENTRI_SECRET", SecretStr("secret_test"))
    monkeypatch.setattr(
        settings, "ENTRI_CNAME_TARGET", "domains.dmaas.example.com"
    )
    monkeypatch.setattr(
        settings, "ENTRI_APPLICATION_URL_BASE", "https://app.example.com"
    )


@pytest.fixture
def unconfigured(monkeypatch):
    """Clear Entri credentials — every endpoint must 503."""
    monkeypatch.setattr(settings, "ENTRI_APPLICATION_ID", None)
    monkeypatch.setattr(settings, "ENTRI_SECRET", None)


@pytest.fixture
def stub_repo(monkeypatch):
    state: dict[str, Any] = {"rows": {}}

    def _store(rec: EntriDomainConnection) -> EntriDomainConnection:
        state["rows"][rec.id] = rec
        return rec

    async def fake_insert(**kwargs):
        rec = EntriDomainConnection(
            id=uuid4(),
            organization_id=kwargs["organization_id"],
            channel_campaign_step_id=kwargs.get("channel_campaign_step_id"),
            domain=kwargs["domain"],
            is_root_domain=kwargs["is_root_domain"],
            application_url=kwargs["application_url"],
            state="pending_modal",
            entri_user_id=kwargs["entri_user_id"],
            entri_token=kwargs.get("entri_token"),
            entri_token_expires_at=kwargs.get("entri_token_expires_at"),
            provider=None,
            setup_type=None,
            propagation_status=None,
            power_status=None,
            secure_status=None,
            last_webhook_id=None,
            last_error=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        return _store(rec)

    async def fake_get(connection_id: UUID):
        return state["rows"].get(connection_id)

    async def fake_list(org_id: UUID):
        return [r for r in state["rows"].values() if r.organization_id == org_id]

    async def fake_update(connection_id: UUID, **fields):
        rec = state["rows"].get(connection_id)
        if rec is None:
            return None
        updates = {k: v for k, v in fields.items() if v is not None}
        rec = replace(rec, **updates)
        state["rows"][rec.id] = rec
        return rec

    async def fake_disconnect(connection_id: UUID):
        return await fake_update(connection_id, state="disconnected")

    monkeypatch.setattr(entri_domains, "insert_pending_connection", fake_insert)
    monkeypatch.setattr(entri_domains, "get_by_id", fake_get)
    monkeypatch.setattr(entri_domains, "list_for_organization", fake_list)
    monkeypatch.setattr(entri_domains, "update_state", fake_update)
    monkeypatch.setattr(entri_domains, "mark_disconnected", fake_disconnect)
    # entri_router imports the module reference, patch there too
    for fn in (
        "insert_pending_connection",
        "get_by_id",
        "list_for_organization",
        "update_state",
        "mark_disconnected",
    ):
        monkeypatch.setattr(entri_router.entri_domains, fn, locals()[f"fake_{fn.split('_')[0] if fn != 'insert_pending_connection' else 'insert'}"]) if False else None
    # Simpler: re-export directly
    monkeypatch.setattr(
        entri_router.entri_domains, "insert_pending_connection", fake_insert
    )
    monkeypatch.setattr(entri_router.entri_domains, "get_by_id", fake_get)
    monkeypatch.setattr(
        entri_router.entri_domains, "list_for_organization", fake_list
    )
    monkeypatch.setattr(entri_router.entri_domains, "update_state", fake_update)
    monkeypatch.setattr(
        entri_router.entri_domains, "mark_disconnected", fake_disconnect
    )
    return state


@pytest.fixture
def stub_entri_client(monkeypatch):
    state: dict[str, Any] = {"calls": []}

    def fake_mint_token(**kwargs):
        state["calls"].append(("mint_token", kwargs))
        return {"auth_token": "jwt_fixture"}

    def fake_check(**kwargs):
        state["calls"].append(("check", kwargs))
        return {"eligible": True}

    def fake_update(**kwargs):
        state["calls"].append(("update", kwargs))
        return {"ok": True}

    def fake_delete(**kwargs):
        state["calls"].append(("delete", kwargs))
        return {"ok": True}

    monkeypatch.setattr(entri_client, "mint_token", fake_mint_token)
    monkeypatch.setattr(entri_client, "check_power_eligibility", fake_check)
    monkeypatch.setattr(entri_client, "update_power_domain", fake_update)
    monkeypatch.setattr(entri_client, "delete_power_domain", fake_delete)
    monkeypatch.setattr(entri_router.entri_client, "mint_token", fake_mint_token)
    monkeypatch.setattr(
        entri_router.entri_client, "check_power_eligibility", fake_check
    )
    monkeypatch.setattr(
        entri_router.entri_client, "update_power_domain", fake_update
    )
    monkeypatch.setattr(
        entri_router.entri_client, "delete_power_domain", fake_delete
    )
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# 503 gating — the most important guarantee while we're unsigned-up.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_503_when_unconfigured(auth_user, unconfigured):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/entri/session", json={"domain": "acme.com", "subdomain": "qr"}
        )
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "entri_not_configured"


@pytest.mark.asyncio
async def test_eligibility_503_when_unconfigured(auth_user, unconfigured):
    async with await _client() as c:
        r = await c.get("/api/v1/entri/eligibility?domain=qr.acme.com")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Configured path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_returns_full_show_entri_config(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/entri/session",
            json={"domain": "acme.com", "subdomain": "qr"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applicationId"] == "app_test"
    assert body["token"] == "jwt_fixture"
    assert body["prefilledDomain"] == "acme.com"
    assert body["defaultSubdomain"] == "qr"
    assert body["power"] is True
    # Subdomain flow → CNAME pointing at our cname_target.
    assert len(body["dnsRecords"]) == 1
    rec = body["dnsRecords"][0]
    assert rec["type"] == "CNAME"
    assert rec["host"] == "{SUBDOMAIN}"
    assert rec["value"] == "domains.dmaas.example.com"
    assert rec["applicationUrl"].startswith("https://app.example.com")
    # userId encodes org:step for webhook correlation.
    assert body["userId"].startswith(f"{_ORG_ID}:")


@pytest.mark.asyncio
async def test_session_root_domain_uses_a_record(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/entri/session",
            json={"domain": "acme.com", "use_root_domain": True},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["secureRootDomain"] is True
    rec = body["dnsRecords"][0]
    assert rec["type"] == "A"
    assert rec["value"] == "{ENTRI_SERVERS}"


@pytest.mark.asyncio
async def test_eligibility_proxies_through_client(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        r = await c.get("/api/v1/entri/eligibility?domain=qr.acme.com")
    assert r.status_code == 200
    body = r.json()
    assert body["domain"] == "qr.acme.com"
    assert body["eligible"] is True
    # Token must have been minted before the eligibility check.
    ops = [op for op, _ in stub_entri_client["calls"]]
    assert ops == ["mint_token", "check"]


@pytest.mark.asyncio
async def test_success_registers_power_and_advances_state(
    auth_user, configured, stub_repo, stub_entri_client
):
    # First create a session to get a session_id
    async with await _client() as c:
        sess = await c.post(
            "/api/v1/entri/session",
            json={"domain": "acme.com", "subdomain": "qr"},
        )
        session_id = sess.json()["session_id"]

        r = await c.post(
            "/api/v1/entri/success",
            json={
                "session_id": session_id,
                "domain": "qr.acme.com",
                "setup_type": "automatic",
                "provider": "godaddy",
            },
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "dns_records_submitted"
    assert body["provider"] == "godaddy"
    # update_power_domain (PUT /power) was called once.
    assert any(op == "update" for op, _ in stub_entri_client["calls"])


@pytest.mark.asyncio
async def test_success_404_unknown_session(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        r = await c.post(
            "/api/v1/entri/success",
            json={"session_id": str(uuid4()), "domain": "qr.acme.com"},
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_domains_only_returns_org_rows(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        # Seed
        await c.post(
            "/api/v1/entri/session",
            json={"domain": "acme.com", "subdomain": "qr"},
        )
        r = await c.get("/api/v1/entri/domains")
    assert r.status_code == 200
    domains = r.json()["domains"]
    assert len(domains) == 1
    assert domains[0]["domain"] == "qr.acme.com"


@pytest.mark.asyncio
async def test_delete_marks_disconnected(
    auth_user, configured, stub_repo, stub_entri_client
):
    async with await _client() as c:
        sess = await c.post(
            "/api/v1/entri/session",
            json={"domain": "acme.com", "subdomain": "qr"},
        )
        session_id = sess.json()["session_id"]
        r = await c.delete(f"/api/v1/entri/domains/{session_id}")
    assert r.status_code == 200
    assert r.json()["state"] == "disconnected"
    assert any(op == "delete" for op, _ in stub_entri_client["calls"])
