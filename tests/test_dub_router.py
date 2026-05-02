"""REST endpoint tests for /api/v1/dub/*.

Auth is bypassed via dependency_overrides; the dub HTTP client is
monkeypatched so we never hit Dub's real API. The dmaas_dub_links
repository is also monkeypatched to in-memory state — no DB required.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.config import settings
from app.dmaas import dub_links as dub_links_repo
from app.dmaas.dub_links import DubLinkRecord
from app.main import app
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError
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
_CLIENT = UserContext(
    auth_user_id=UUID("33333333-3333-3333-3333-333333333333"),
    business_user_id=UUID("44444444-4444-4444-4444-444444444444"),
    email="client@example.com",
    platform_role=None,
    active_organization_id=None,
    org_role="member",
    role="client",
    client_id=UUID("55555555-5555-5555-5555-555555555555"),
)


@pytest.fixture
def auth_operator():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _OPERATOR
    app.dependency_overrides[require_operator] = lambda: _OPERATOR
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_client():
    from fastapi import HTTPException

    def _deny():
        raise HTTPException(403, {"error": "operator_role_required"})

    app.dependency_overrides[verify_supabase_jwt] = lambda: _CLIENT
    app.dependency_overrides[require_operator] = _deny
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def stub_dub_links_repo(monkeypatch):
    """In-memory replacement for the dmaas_dub_links repo."""
    state: dict[str, Any] = {"rows": []}

    async def fake_insert(**kwargs):
        rec = DubLinkRecord(
            id=uuid4(),
            dub_link_id=kwargs["dub_link_id"],
            dub_external_id=kwargs.get("dub_external_id"),
            dub_short_url=kwargs["dub_short_url"],
            dub_domain=kwargs["dub_domain"],
            dub_key=kwargs["dub_key"],
            destination_url=kwargs["destination_url"],
            dmaas_design_id=kwargs.get("dmaas_design_id"),
            direct_mail_piece_id=kwargs.get("direct_mail_piece_id"),
            brand_id=kwargs.get("brand_id"),
            channel_campaign_step_id=kwargs.get("channel_campaign_step_id"),
            recipient_id=kwargs.get("recipient_id"),
            dub_folder_id=kwargs.get("dub_folder_id"),
            dub_tag_ids=list(kwargs.get("dub_tag_ids") or []),
            attribution_context=kwargs.get("attribution_context") or {},
            created_by_user_id=kwargs.get("created_by_user_id"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["rows"].append(rec)
        return rec

    async def fake_get_by_dub_id(dub_link_id):
        for r in state["rows"]:
            if r.dub_link_id == dub_link_id:
                return r
        return None

    async def fake_list_for_design(design_id):
        return [r for r in state["rows"] if r.dmaas_design_id == design_id]

    monkeypatch.setattr(dub_links_repo, "insert_dub_link", fake_insert)
    monkeypatch.setattr(dub_links_repo, "get_dub_link_by_dub_id", fake_get_by_dub_id)
    monkeypatch.setattr(dub_links_repo, "list_dub_links_for_design", fake_list_for_design)

    # The router imports the module; patch through there too.
    monkeypatch.setattr(dub_router.dub_links_repo, "insert_dub_link", fake_insert)
    monkeypatch.setattr(
        dub_router.dub_links_repo, "get_dub_link_by_dub_id", fake_get_by_dub_id
    )
    monkeypatch.setattr(
        dub_router.dub_links_repo, "list_dub_links_for_design", fake_list_for_design
    )
    return state


async def _http_client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _link_payload(**overrides) -> dict[str, Any]:
    base = {
        "id": "link_abc",
        "domain": "dub.sh",
        "key": "abc",
        "url": "https://example.com",
        "shortLink": "https://dub.sh/abc",
        "qrCode": "https://api.dub.co/qr?url=…",
        "externalId": None,
        "tenantId": "hq-x",
        "trackConversion": False,
        "clicks": 0,
        "leads": 0,
        "sales": 0,
        "createdAt": "2026-04-01T00:00:00Z",
        "updatedAt": "2026-04-01T00:00:00Z",
        "archived": False,
        "workspaceId": "ws_abc",  # extra field — should land in `extra` dict
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_link_happy_and_persists(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    captured: dict[str, Any] = {}

    def fake_create_link(**kwargs):
        captured.update(kwargs)
        return _link_payload()

    monkeypatch.setattr(dub_client, "create_link", fake_create_link)
    monkeypatch.setattr(dub_router.dub_client, "create_link", fake_create_link)

    design_id = uuid4()
    body = {
        "url": "https://example.com",
        "external_id": "ext_42",
        "dmaas_design_id": str(design_id),
        "attribution_context": {"campaign_id": "spring_2026"},
    }
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["id"] == "link_abc"
    assert out["short_link"] == "https://dub.sh/abc"
    # Unknown Dub field surfaces in `extra`.
    assert out["extra"]["workspaceId"] == "ws_abc"
    # Provider was called with snake-case kwargs. Pydantic's HttpUrl
    # normalizes trailing slash; just verify the host round-trips.
    assert captured["url"].startswith("https://example.com")
    assert captured["external_id"] == "ext_42"
    # Persisted row exists and joins the design.
    assert len(stub_dub_links_repo["rows"]) == 1
    rec = stub_dub_links_repo["rows"][0]
    assert rec.dmaas_design_id == design_id
    assert rec.attribution_context == {"campaign_id": "spring_2026"}


@pytest.mark.asyncio
async def test_create_link_auto_stamps_default_tenant_and_domain(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    captured: dict[str, Any] = {}

    def fake_create_link(**kwargs):
        captured.update(kwargs)
        return _link_payload()

    monkeypatch.setattr(dub_router.dub_client, "create_link", fake_create_link)
    monkeypatch.setattr(settings, "DUB_DEFAULT_TENANT_ID", "hq-x")
    monkeypatch.setattr(settings, "DUB_DEFAULT_DOMAIN", "go.hq-x.com")

    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 201, r.text
    assert captured["tenant_id"] == "hq-x"
    assert captured["domain"] == "go.hq-x.com"


@pytest.mark.asyncio
async def test_create_link_explicit_overrides_default(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    captured: dict[str, Any] = {}

    def fake_create_link(**kwargs):
        captured.update(kwargs)
        return _link_payload()

    monkeypatch.setattr(dub_router.dub_client, "create_link", fake_create_link)
    monkeypatch.setattr(settings, "DUB_DEFAULT_TENANT_ID", "default")

    async with await _http_client() as c:
        await c.post(
            "/api/v1/dub/links",
            json={"url": "https://example.com", "tenant_id": "explicit"},
        )
    assert captured["tenant_id"] == "explicit"


@pytest.mark.asyncio
async def test_create_link_requires_operator(
    auth_client, stub_dub_links_repo, monkeypatch
):
    monkeypatch.setattr(dub_router.dub_client, "create_link", lambda **_: _link_payload())
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_create_link_dub_409_conflict(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    def boom(**_):
        raise DubProviderError("key in use", status=409, code="conflict")

    monkeypatch.setattr(dub_router.dub_client, "create_link", boom)
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "dub_conflict"


@pytest.mark.asyncio
async def test_create_link_dub_401_becomes_502(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    def boom(**_):
        raise DubProviderError("bad key", status=401, code="unauthorized")

    monkeypatch.setattr(dub_router.dub_client, "create_link", boom)
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 502
    assert r.json()["detail"]["error"] == "dub_auth_failed"


@pytest.mark.asyncio
async def test_create_link_transient_becomes_503(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    def boom(**_):
        raise DubProviderError("upstream broken", status=503)

    monkeypatch.setattr(dub_router.dub_client, "create_link", boom)
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "dub_unavailable"


@pytest.mark.asyncio
async def test_create_link_dub_400(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    def boom(**_):
        raise DubProviderError(
            "invalid url",
            status=400,
            code="invalid_input",
            doc_url="https://dub.co/docs/errors/invalid_input",
        )

    monkeypatch.setattr(dub_router.dub_client, "create_link", boom)
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"] == "dub_bad_request"
    assert detail["code"] == "invalid_input"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_link_happy(auth_operator, stub_dub_links_repo, monkeypatch):
    monkeypatch.setattr(dub_router.dub_client, "get_link", lambda **_: _link_payload())
    async with await _http_client() as c:
        r = await c.get("/api/v1/dub/links/link_abc")
    assert r.status_code == 200
    assert r.json()["id"] == "link_abc"


@pytest.mark.asyncio
async def test_get_link_by_external_id(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    monkeypatch.setattr(
        dub_router.dub_client,
        "get_link_by_external_id",
        lambda **_: _link_payload(externalId="ext_42"),
    )
    async with await _http_client() as c:
        r = await c.get("/api/v1/dub/links/by-external-id/ext_42")
    assert r.status_code == 200
    assert r.json()["external_id"] == "ext_42"


# ---------------------------------------------------------------------------
# Delete (soft vs hard)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_link_defaults_to_archive(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    captured: dict[str, Any] = {}

    def fake_update(**kwargs):
        captured.update(kwargs)
        return _link_payload(archived=True)

    def hard_delete(**_):
        raise AssertionError("hard delete should not be called")

    monkeypatch.setattr(dub_router.dub_client, "update_link", fake_update)
    monkeypatch.setattr(dub_router.dub_client, "delete_link", hard_delete)

    async with await _http_client() as c:
        r = await c.delete("/api/v1/dub/links/link_abc")
    assert r.status_code == 200
    assert r.json() == {"id": "link_abc", "deleted": False, "archived": True}
    assert captured["fields"] == {"archived": True}


@pytest.mark.asyncio
async def test_delete_link_hard(auth_operator, stub_dub_links_repo, monkeypatch):
    called: dict[str, Any] = {}

    def fake_delete(**kwargs):
        called.update(kwargs)
        return None

    def soft(**_):
        raise AssertionError("soft delete should not be called")

    monkeypatch.setattr(dub_router.dub_client, "delete_link", fake_delete)
    monkeypatch.setattr(dub_router.dub_client, "update_link", soft)

    async with await _http_client() as c:
        r = await c.delete("/api/v1/dub/links/link_abc?hard=true")
    assert r.status_code == 200
    assert r.json() == {"id": "link_abc", "deleted": True, "archived": False}
    assert called["link_id"] == "link_abc"


# ---------------------------------------------------------------------------
# Analytics + events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_analytics_passthrough(auth_operator, stub_dub_links_repo, monkeypatch):
    captured: dict[str, Any] = {}
    sample_data = [{"start": "2026-04-01", "clicks": 7}]

    def fake(**kwargs):
        captured.update(kwargs)
        return sample_data

    monkeypatch.setattr(dub_router.dub_client, "retrieve_analytics", fake)
    async with await _http_client() as c:
        r = await c.get(
            "/api/v1/dub/analytics",
            params={
                "event": "clicks",
                "group_by": "timeseries",
                "interval": "7d",
                "link_id": "link_abc",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "clicks"
    assert body["group_by"] == "timeseries"
    assert body["data"] == sample_data
    assert captured["group_by"] == "timeseries"
    assert captured["link_id"] == "link_abc"


@pytest.mark.asyncio
async def test_events_passthrough(auth_operator, stub_dub_links_repo, monkeypatch):
    sample = [{"event": "click", "timestamp": "2026-04-01T00:00:00Z"}]
    monkeypatch.setattr(dub_router.dub_client, "list_events", lambda **_: sample)
    async with await _http_client() as c:
        r = await c.get("/api/v1/dub/events?event=clicks&link_id=link_abc")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["events"] == sample


# ---------------------------------------------------------------------------
# By-design lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_links_by_design_returns_persisted(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    captured: dict[str, Any] = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return _link_payload()

    def fake_get(**_):
        return _link_payload()

    monkeypatch.setattr(dub_router.dub_client, "create_link", fake_create)
    monkeypatch.setattr(dub_router.dub_client, "get_link", fake_get)

    design_id = uuid4()
    async with await _http_client() as c:
        cr = await c.post(
            "/api/v1/dub/links",
            json={
                "url": "https://example.com",
                "dmaas_design_id": str(design_id),
            },
        )
        assert cr.status_code == 201, cr.text
        r = await c.get(f"/api/v1/dub/links/by-design/{design_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["persisted"]["dmaas_design_id"] == str(design_id)
    assert body["items"][0]["link"]["id"] == "link_abc"


@pytest.mark.asyncio
async def test_links_by_design_degrades_on_dub_failure(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    monkeypatch.setattr(
        dub_router.dub_client, "create_link", lambda **_: _link_payload()
    )

    def boom(**_):
        raise DubProviderError("upstream down", status=503)

    monkeypatch.setattr(dub_router.dub_client, "get_link", boom)

    design_id = uuid4()
    async with await _http_client() as c:
        await c.post(
            "/api/v1/dub/links",
            json={"url": "https://example.com", "dmaas_design_id": str(design_id)},
        )
        r = await c.get(f"/api/v1/dub/links/by-design/{design_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["items"][0]["link"] is None
    assert body["items"][0]["persisted"]["dub_link_id"] == "link_abc"


# ---------------------------------------------------------------------------
# Not configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dub_not_configured_returns_503(
    auth_operator, stub_dub_links_repo, monkeypatch
):
    monkeypatch.setattr(settings, "DUB_API_KEY", None)
    async with await _http_client() as c:
        r = await c.post("/api/v1/dub/links", json={"url": "https://example.com"})
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "dub_not_configured"
