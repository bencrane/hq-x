"""REST endpoint tests for the new /api/v1/dub/* admin routes:
links/bulk, folders, tags, webhooks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.dmaas import dub_webhooks_repo
from app.dmaas.dub_webhooks_repo import DubWebhookRecord
from app.main import app
from app.routers import dub as dub_router

_OPERATOR = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    email="op@example.com",
    platform_role="platform_operator",
    active_organization_id=None,
    org_role=None,
    role="operator",
    client_id=None,
)


@pytest.fixture
def auth_operator():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _OPERATOR
    app.dependency_overrides[require_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def stub_webhook_repo(monkeypatch):
    state: dict[str, Any] = {"rows": [], "deactivated": []}

    async def fake_insert(**kwargs):
        rec = DubWebhookRecord(
            id=uuid4(),
            dub_webhook_id=kwargs["dub_webhook_id"],
            name=kwargs["name"],
            receiver_url=kwargs["receiver_url"],
            secret_hash=kwargs.get("secret_hash"),
            triggers=list(kwargs.get("triggers") or []),
            environment=kwargs["environment"],
            is_active=kwargs.get("is_active", True),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["rows"].append(rec)
        return rec

    async def fake_deactivate(dub_webhook_id):
        state["deactivated"].append(dub_webhook_id)
        for r in state["rows"]:
            if r.dub_webhook_id == dub_webhook_id:
                r.is_active = False
                return r
        return None

    monkeypatch.setattr(dub_webhooks_repo, "insert_dub_webhook", fake_insert)
    monkeypatch.setattr(dub_webhooks_repo, "deactivate_dub_webhook", fake_deactivate)
    monkeypatch.setattr(dub_router.dub_webhooks_repo, "insert_dub_webhook", fake_insert)
    monkeypatch.setattr(
        dub_router.dub_webhooks_repo, "deactivate_dub_webhook", fake_deactivate
    )
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# Bulk links
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_create_links_endpoint(auth_operator, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_bulk(**kwargs):
        captured.update(kwargs)
        return [
            {"id": "link_a", "url": "https://a"},
            {"id": "link_b", "url": "https://b"},
        ]

    monkeypatch.setattr(dub_router.dub_client, "bulk_create_links", fake_bulk)

    body = {
        "links": [
            {"url": "https://a", "external_id": "ext_a"},
            {"url": "https://b", "external_id": "ext_b"},
        ]
    }
    async with await _client() as c:
        r = await c.post("/api/v1/dub/links/bulk", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["count"] == 2
    assert out["results"][0]["id"] == "link_a"
    assert captured["links"] == [
        {"url": "https://a", "external_id": "ext_a"},
        {"url": "https://b", "external_id": "ext_b"},
    ]


@pytest.mark.asyncio
async def test_bulk_update_links_endpoint(auth_operator, monkeypatch):
    monkeypatch.setattr(
        dub_router.dub_client,
        "bulk_update_links",
        lambda **_: [{"id": "link_a", "archived": True}],
    )
    async with await _client() as c:
        r = await c.patch(
            "/api/v1/dub/links/bulk",
            json={"link_ids": ["link_a"], "fields": {"archived": True}},
        )
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 1


@pytest.mark.asyncio
async def test_bulk_delete_links_endpoint(auth_operator, monkeypatch):
    captured: dict[str, Any] = {}

    def fake_delete(**kwargs):
        captured.update(kwargs)
        return {"deletedCount": 2}

    monkeypatch.setattr(dub_router.dub_client, "bulk_delete_links", fake_delete)

    async with await _client() as c:
        r = await c.delete("/api/v1/dub/links/bulk?link_ids=link_a,link_b")
    assert r.status_code == 200, r.text
    assert r.json()["deleted_count"] == 2
    assert captured["link_ids"] == ["link_a", "link_b"]


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_folders_crud(auth_operator, monkeypatch):
    monkeypatch.setattr(
        dub_router.dub_client, "list_folders", lambda **_: [{"id": "fold_a"}]
    )
    monkeypatch.setattr(
        dub_router.dub_client, "create_folder", lambda **kw: {"id": "fold_new", **kw}
    )
    monkeypatch.setattr(
        dub_router.dub_client, "get_folder", lambda **kw: {"id": kw["folder_id"]}
    )
    monkeypatch.setattr(
        dub_router.dub_client,
        "update_folder",
        lambda **kw: {"id": kw["folder_id"], "name": kw["name"]},
    )
    monkeypatch.setattr(dub_router.dub_client, "delete_folder", lambda **_: None)

    async with await _client() as c:
        r = await c.get("/api/v1/dub/folders")
        assert r.status_code == 200
        assert r.json()["count"] == 1

        r = await c.post(
            "/api/v1/dub/folders", json={"name": "campaign:abc", "access_level": "write"}
        )
        assert r.status_code == 201
        assert r.json()["id"] == "fold_new"

        r = await c.get("/api/v1/dub/folders/fold_a")
        assert r.status_code == 200

        r = await c.patch("/api/v1/dub/folders/fold_a", json={"name": "renamed"})
        assert r.status_code == 200
        assert r.json()["name"] == "renamed"

        r = await c.delete("/api/v1/dub/folders/fold_a")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tags_crud(auth_operator, monkeypatch):
    monkeypatch.setattr(
        dub_router.dub_client, "list_tags", lambda **_: [{"id": "tag_a"}]
    )
    monkeypatch.setattr(
        dub_router.dub_client, "create_tag", lambda **kw: {"id": "tag_new", **kw}
    )
    monkeypatch.setattr(
        dub_router.dub_client,
        "update_tag",
        lambda **kw: {"id": kw["tag_id"], "color": kw["color"]},
    )
    monkeypatch.setattr(dub_router.dub_client, "delete_tag", lambda **_: None)

    async with await _client() as c:
        r = await c.get("/api/v1/dub/tags")
        assert r.status_code == 200

        r = await c.post("/api/v1/dub/tags", json={"name": "brand:hq-x"})
        assert r.status_code == 201

        r = await c.patch("/api/v1/dub/tags/tag_a", json={"color": "blue"})
        assert r.status_code == 200
        assert r.json()["color"] == "blue"

        r = await c.delete("/api/v1/dub/tags/tag_a")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Webhooks (CRUD + local mirror)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_webhook_create_persists_local_mirror(
    auth_operator, stub_webhook_repo, monkeypatch
):
    monkeypatch.setattr(
        dub_router.dub_client,
        "create_webhook",
        lambda **kw: {"id": "wh_new", "name": kw["name"]},
    )
    body = {
        "name": "hq-x:dev",
        "url": "https://api.hq-x.com/webhooks/dub",
        "triggers": ["link.clicked", "lead.created"],
        "secret": "topsecret",
    }
    async with await _client() as c:
        r = await c.post("/api/v1/dub/webhooks", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "wh_new"
    assert len(stub_webhook_repo["rows"]) == 1
    rec = stub_webhook_repo["rows"][0]
    assert rec.dub_webhook_id == "wh_new"
    # Plain secret is NEVER stored — only the sha256 hash.
    assert rec.secret_hash and rec.secret_hash != "topsecret"
    assert rec.triggers == ["link.clicked", "lead.created"]


@pytest.mark.asyncio
async def test_webhook_delete_soft_deactivates_local(
    auth_operator, stub_webhook_repo, monkeypatch
):
    # Pre-seed a local mirror row.
    await dub_webhooks_repo.insert_dub_webhook(
        dub_webhook_id="wh_existing",
        name="x",
        receiver_url="https://example.com/wh",
        triggers=["link.clicked"],
        environment="dev",
    )
    monkeypatch.setattr(dub_router.dub_client, "delete_webhook", lambda **_: None)

    async with await _client() as c:
        r = await c.delete("/api/v1/dub/webhooks/wh_existing")
    assert r.status_code == 200
    assert r.json() == {"id": "wh_existing", "deleted": True}
    assert "wh_existing" in stub_webhook_repo["deactivated"]


@pytest.mark.asyncio
async def test_webhook_list_get_update(auth_operator, monkeypatch):
    monkeypatch.setattr(
        dub_router.dub_client,
        "list_webhooks",
        lambda **_: [{"id": "wh_a"}],
    )
    monkeypatch.setattr(
        dub_router.dub_client,
        "get_webhook",
        lambda **kw: {"id": kw["webhook_id"]},
    )
    monkeypatch.setattr(
        dub_router.dub_client,
        "update_webhook",
        lambda **kw: {"id": kw["webhook_id"], "disabled": kw["disabled"]},
    )
    async with await _client() as c:
        r = await c.get("/api/v1/dub/webhooks")
        assert r.status_code == 200
        assert r.json()["count"] == 1

        r = await c.get("/api/v1/dub/webhooks/wh_a")
        assert r.status_code == 200

        r = await c.patch(
            "/api/v1/dub/webhooks/wh_a", json={"disabled": True}
        )
        assert r.status_code == 200
        assert r.json()["disabled"] is True


# ---------------------------------------------------------------------------
# Analytics filter forwarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_forwards_new_filters(auth_operator, monkeypatch):
    captured: dict[str, Any] = {}

    def fake(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(dub_router.dub_client, "retrieve_analytics", fake)
    async with await _client() as c:
        r = await c.get(
            "/api/v1/dub/analytics",
            params={
                "country": "US",
                "device": "Mobile",
                "folder_id": "fold_abc",
                "customer_id": "rcpt_42",
            },
        )
    assert r.status_code == 200
    assert captured["country"] == "US"
    assert captured["device"] == "Mobile"
    assert captured["folder_id"] == "fold_abc"
    assert captured["customer_id"] == "rcpt_42"
