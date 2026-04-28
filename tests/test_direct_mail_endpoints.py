"""Direct-mail public router tests.

Bypasses Supabase JWT auth via FastAPI's `dependency_overrides`. Mocks
provider calls + DB-touching helpers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.auth.roles import require_operator
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.direct_mail import addresses, persistence
from app.direct_mail.persistence import UpsertedPiece
from app.main import app
from app.providers.lob import client as lob_client
from app.routers import direct_mail as direct_mail_router

_TEST_USER = UserContext(
    auth_user_id=UUID("11111111-1111-1111-1111-111111111111"),
    business_user_id=UUID("22222222-2222-2222-2222-222222222222"),
    email="op@example.com",
    role="operator",
    client_id=None,
)


@pytest.fixture(autouse=True)
def auth_override():
    app.dependency_overrides[verify_supabase_jwt] = lambda: _TEST_USER
    app.dependency_overrides[require_operator] = lambda: _TEST_USER
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def stub_persistence(monkeypatch):
    state = {"upserts": [], "lookup": {}}

    def _fake_upserted(piece_type, provider_piece, deliverability):
        return UpsertedPiece(
            id=uuid4(),
            external_piece_id=provider_piece["id"],
            piece_type=piece_type,
            status=provider_piece.get("status", "queued"),
            cost_cents=persistence.project_cost_cents(provider_piece),
            deliverability=deliverability,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            raw_payload=provider_piece,
            metadata=None,
        )

    async def fake_upsert(*, piece_type, provider_piece, deliverability, created_by_user_id, **_):
        piece = _fake_upserted(piece_type, provider_piece, deliverability)
        state["upserts"].append(piece)
        state["lookup"][(piece_type, piece.external_piece_id)] = piece
        return piece

    async def fake_get(*, external_piece_id, piece_type=None, provider_slug="lob"):
        if piece_type is None:
            for (_, ex_id), piece in state["lookup"].items():
                if ex_id == external_piece_id:
                    return piece
            return None
        return state["lookup"].get((piece_type, external_piece_id))

    monkeypatch.setattr(direct_mail_router, "upsert_piece", fake_upsert)
    monkeypatch.setattr(direct_mail_router, "get_piece_by_external_id", fake_get)
    return state


@pytest.fixture
def stub_address_gate(monkeypatch):
    state = {"calls": []}

    async def fake_verify_or_suppress(*, api_key, payload, skip):
        state["calls"].append({"payload": payload, "skip": skip})
        return addresses.AddressVerifyResult(deliverability="deliverable", raw=None)

    monkeypatch.setattr(direct_mail_router, "verify_or_suppress", fake_verify_or_suppress)
    return state


@pytest.fixture
def stub_lob(monkeypatch):
    state = {
        "create_postcard_calls": [],
        "create_postcard_response": {"id": "psc_1", "price": "0.84", "status": "queued"},
        "create_letter_calls": [],
        "create_letter_response": {"id": "ltr_1", "price": "1.40", "status": "queued"},
        "list_postcards_response": {"data": [{"id": "psc_1"}], "object": "list"},
        "cancel_postcard_response": {"id": "psc_1", "deleted": True},
        "verify_response": {"deliverability": "deliverable"},
        "verify_bulk_response": {"addresses": []},
    }

    def fake_create_postcard(api_key, payload, **kwargs):
        state["create_postcard_calls"].append({"api_key": api_key, "payload": payload, **kwargs})
        return state["create_postcard_response"]

    def fake_create_letter(api_key, payload, **kwargs):
        state["create_letter_calls"].append({"payload": payload, **kwargs})
        return state["create_letter_response"]

    monkeypatch.setattr(lob_client, "create_postcard", fake_create_postcard)
    monkeypatch.setattr(lob_client, "create_letter", fake_create_letter)
    monkeypatch.setattr(
        lob_client, "list_postcards", lambda k, **kw: state["list_postcards_response"]
    )
    monkeypatch.setattr(
        lob_client, "cancel_postcard", lambda k, pid, **kw: state["cancel_postcard_response"]
    )
    monkeypatch.setattr(
        lob_client, "verify_address_us_single", lambda k, p: state["verify_response"]
    )
    monkeypatch.setattr(
        lob_client, "verify_address_us_bulk", lambda k, p: state["verify_bulk_response"]
    )

    # Mirror for self-mailers / snap packs / booklets so the test set covers all 5.
    for fn_name, resp in (
        ("create_self_mailer", {"id": "sm_1", "price": "0.50", "status": "queued"}),
        ("create_snap_pack", {"id": "sp_1", "price": "0.60", "status": "queued"}),
        ("create_booklet", {"id": "bk_1", "price": "1.20", "status": "queued"}),
    ):
        monkeypatch.setattr(
            lob_client,
            fn_name,
            lambda k, p, _resp=resp, **kw: _resp,
        )
    return state


async def _client():
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_create_postcard_happy_path(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={
                "payload": {
                    "to": {
                        "address_line1": "1 Main",
                        "address_city": "SF",
                        "address_state": "CA",
                        "address_zip": "94101",
                    },
                    "front": "tmpl_a",
                }
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["external_piece_id"] == "psc_1"
    assert body["piece_type"] == "postcard"
    assert body["cost_cents"] == 84  # "0.84" → 84
    assert body["deliverability"] == "deliverable"
    # idempotency was auto-derived (caller didn't supply)
    assert stub_lob["create_postcard_calls"][0]["idempotency_key"] is not None
    # default location is header
    assert stub_lob["create_postcard_calls"][0]["idempotency_in_query"] is False


@pytest.mark.asyncio
async def test_create_postcard_caller_supplied_idempotency_passes_through(
    stub_persistence, stub_address_gate, stub_lob
):
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={
                "payload": {"to": "adr_x"},
                "idempotency_key": "K-FROM-CALLER",
                "idempotency_location": "query",
                "skip_address_verification": True,
            },
        )
    assert resp.status_code == 200
    assert stub_lob["create_postcard_calls"][0]["idempotency_key"] == "K-FROM-CALLER"
    assert stub_lob["create_postcard_calls"][0]["idempotency_in_query"] is True


@pytest.mark.asyncio
async def test_create_postcard_502_when_provider_omits_id(
    stub_persistence, stub_address_gate, stub_lob
):
    stub_lob["create_postcard_response"] = {"price": "0.84"}
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={"payload": {"to": "adr_x"}, "skip_address_verification": True},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"]["type"] == "provider_bad_response"


@pytest.mark.asyncio
async def test_create_postcard_address_undeliverable_returns_422(
    stub_persistence, stub_address_gate, stub_lob, monkeypatch
):
    async def reject(*, api_key, payload, skip):
        raise addresses.AddressUndeliverable(
            deliverability="undeliverable",
            address_hash="h",
            raw=None,
        )

    monkeypatch.setattr(direct_mail_router, "verify_or_suppress", reject)

    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={
                "payload": {
                    "to": {
                        "address_line1": "x",
                        "address_city": "y",
                        "address_state": "z",
                        "address_zip": "0",
                    }
                }
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "address_undeliverable"


@pytest.mark.asyncio
async def test_create_postcard_suppressed_returns_409(
    stub_persistence, stub_address_gate, stub_lob, monkeypatch
):
    async def reject(*, api_key, payload, skip):
        raise addresses.AddressSuppressed(reason="returned_to_sender", address_hash="h")

    monkeypatch.setattr(direct_mail_router, "verify_or_suppress", reject)

    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={
                "payload": {
                    "to": {
                        "address_line1": "x",
                        "address_city": "y",
                        "address_state": "z",
                        "address_zip": "0",
                    }
                }
            },
        )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "address_suppressed"
    assert resp.json()["detail"]["reason"] == "returned_to_sender"


@pytest.mark.asyncio
async def test_get_postcard_404_for_unknown(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.get("/direct-mail/postcards/psc_unknown")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_postcard_after_create(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        await c.post(
            "/direct-mail/postcards",
            json={"payload": {"to": "adr_x"}, "skip_address_verification": True},
        )
        resp = await c.get("/direct-mail/postcards/psc_1")
    assert resp.status_code == 200
    assert resp.json()["external_piece_id"] == "psc_1"


@pytest.mark.asyncio
async def test_list_postcards(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.get("/direct-mail/postcards")
    assert resp.status_code == 200
    assert resp.json()["data"][0]["id"] == "psc_1"


@pytest.mark.asyncio
async def test_cancel_postcard(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.post("/direct-mail/postcards/psc_1/cancel")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True


@pytest.mark.asyncio
async def test_create_letter_happy_path(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/letters",
            json={"payload": {"to": "adr_x", "file": "tmpl_a"}, "skip_address_verification": True},
        )
    assert resp.status_code == 200
    assert resp.json()["external_piece_id"] == "ltr_1"
    assert resp.json()["cost_cents"] == 140


@pytest.mark.asyncio
async def test_each_piece_type_routes_exist(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        for path in ("self-mailers", "snap-packs", "booklets"):
            resp = await c.post(
                f"/direct-mail/{path}",
                json={"payload": {"to": "adr_x"}, "skip_address_verification": True},
            )
            assert resp.status_code == 200, (path, resp.text)


@pytest.mark.asyncio
async def test_verify_address_us_route(stub_persistence, stub_address_gate, stub_lob):
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/verify-address/us", json={"payload": {"primary_line": "1 Main"}}
        )
    assert resp.status_code == 200
    assert resp.json()["result"]["deliverability"] == "deliverable"


@pytest.mark.asyncio
async def test_create_postcard_503_when_no_api_key(
    monkeypatch, stub_persistence, stub_address_gate, stub_lob
):
    from app.config import settings

    monkeypatch.setattr(settings, "LOB_API_KEY", None)
    async with await _client() as c:
        resp = await c.post(
            "/direct-mail/postcards",
            json={"payload": {"to": "adr_x"}, "skip_address_verification": True},
        )
    assert resp.status_code == 503
    assert resp.json()["detail"]["type"] == "provider_unconfigured"
