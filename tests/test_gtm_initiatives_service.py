"""Tests for app.services.gtm_initiatives.

Uses an in-memory fake of get_db_connection so the create / get /
transition_status paths can be exercised without a real Postgres. The
state-machine table is the load-bearing part — we test allowed and
disallowed transitions explicitly.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import gtm_initiatives as gtm_svc

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PARTNER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTRACT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
AUDIENCE = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
ORG_OTHER = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")


# ─────────────────────── in-memory fake of get_db_connection ───────────────


def _row_tuple(row: dict[str, Any]) -> tuple:
    return (
        row["id"],
        row["organization_id"],
        row["brand_id"],
        row["partner_id"],
        row["partner_contract_id"],
        row["data_engine_audience_id"],
        row["partner_research_ref"],
        row["strategic_context_research_ref"],
        row["campaign_strategy_path"],
        row["status"],
        row["history"],
        row["metadata"],
        row["reservation_window_start"],
        row["reservation_window_end"],
        row["created_at"],
        row["updated_at"],
    )


class _FakeCursor:
    def __init__(self, store: dict[UUID, dict[str, Any]]):
        self._store = store
        self._row: tuple | None = None
        self.description = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def execute(self, sql: str, params: tuple | list | None = None):
        params = params or ()
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO business.gtm_initiatives"):
            now = datetime.now(UTC)
            new_id = uuid4()
            row = {
                "id": new_id,
                "organization_id": UUID(params[0]),
                "brand_id": UUID(params[1]),
                "partner_id": UUID(params[2]),
                "partner_contract_id": UUID(params[3]),
                "data_engine_audience_id": UUID(params[4]),
                "partner_research_ref": params[5],
                "strategic_context_research_ref": None,
                "campaign_strategy_path": None,
                "status": "draft",
                "history": [],
                "metadata": params[8].obj if hasattr(params[8], "obj") else params[8],
                "reservation_window_start": params[6],
                "reservation_window_end": params[7],
                "created_at": now,
                "updated_at": now,
            }
            self._store[new_id] = row
            self._row = _row_tuple(row)
            return
        if s.startswith("SELECT") and "FROM business.gtm_initiatives" in s and "WHERE id = %s" in s:
            target_id = UUID(params[0])
            row = self._store.get(target_id)
            if row is None:
                self._row = None
                return
            if "organization_id = %s" in s and len(params) >= 2:
                if str(row["organization_id"]) != params[1]:
                    self._row = None
                    return
            self._row = _row_tuple(row)
            return
        if s.startswith("UPDATE business.gtm_initiatives") and "SET status = %s" in s:
            new_status = params[0]
            target_id = UUID(params[2])
            row = self._store[target_id]
            row["status"] = new_status
            row["updated_at"] = datetime.now(UTC)
            row["history"] = (row["history"] or []) + [
                {"kind": "transition", "to_status": new_status}
            ]
            self._row = _row_tuple(row)
            return
        if (
            s.startswith("UPDATE business.gtm_initiatives")
            and "strategic_context_research_ref" in s
        ):
            ref = params[0]
            target_id = UUID(params[1])
            self._store[target_id]["strategic_context_research_ref"] = ref
            return
        if s.startswith("UPDATE business.gtm_initiatives") and "campaign_strategy_path" in s:
            path = params[0]
            target_id = UUID(params[1])
            self._store[target_id]["campaign_strategy_path"] = path
            return
        if s.startswith("UPDATE business.gtm_initiatives") and "history = history" in s:
            # history-only append used by append_history; no return value needed
            return
        raise AssertionError(f"unhandled SQL: {s!r}")

    async def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, store: dict[UUID, dict[str, Any]]):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    async def commit(self):
        pass

    async def rollback(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    store: dict[UUID, dict[str, Any]] = {}

    @asynccontextmanager
    async def fake_get_db_connection():
        yield _FakeConn(store)

    monkeypatch.setattr(gtm_svc, "get_db_connection", fake_get_db_connection)
    return store


# ── create + get ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_initiative_returns_draft_row(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
        partner_research_ref="hqx://exa.exa_calls/abc",
    )
    assert row["status"] == "draft"
    assert row["organization_id"] == ORG
    assert row["partner_research_ref"] == "hqx://exa.exa_calls/abc"
    assert row["history"] == []


@pytest.mark.asyncio
async def test_get_initiative_cross_org_returns_none(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    assert await gtm_svc.get_initiative(row["id"], organization_id=ORG) is not None
    assert await gtm_svc.get_initiative(row["id"], organization_id=ORG_OTHER) is None


# ── state machine ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transition_happy_path(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    iid = row["id"]
    after_research = await gtm_svc.transition_status(
        iid, new_status="awaiting_strategic_research"
    )
    assert after_research["status"] == "awaiting_strategic_research"

    after_ready = await gtm_svc.transition_status(
        iid, new_status="strategic_research_ready"
    )
    assert after_ready["status"] == "strategic_research_ready"

    after_synth = await gtm_svc.transition_status(
        iid, new_status="awaiting_strategy_synthesis"
    )
    assert after_synth["status"] == "awaiting_strategy_synthesis"

    after_final = await gtm_svc.transition_status(
        iid, new_status="strategy_ready"
    )
    assert after_final["status"] == "strategy_ready"


@pytest.mark.asyncio
async def test_transition_refuses_illegal(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    iid = row["id"]
    # draft → strategy_ready is not allowed
    with pytest.raises(gtm_svc.InvalidStatusTransition):
        await gtm_svc.transition_status(iid, new_status="strategy_ready")
    # draft → awaiting_strategy_synthesis is not allowed
    with pytest.raises(gtm_svc.InvalidStatusTransition):
        await gtm_svc.transition_status(
            iid, new_status="awaiting_strategy_synthesis"
        )


@pytest.mark.asyncio
async def test_transition_refuses_backward(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    iid = row["id"]
    await gtm_svc.transition_status(iid, new_status="awaiting_strategic_research")
    await gtm_svc.transition_status(iid, new_status="strategic_research_ready")
    # strategic_research_ready → awaiting_strategic_research is not allowed
    with pytest.raises(gtm_svc.InvalidStatusTransition):
        await gtm_svc.transition_status(
            iid, new_status="awaiting_strategic_research"
        )


@pytest.mark.asyncio
async def test_transition_not_found(fake_db):
    with pytest.raises(gtm_svc.GtmInitiativeNotFound):
        await gtm_svc.transition_status(
            uuid4(), new_status="awaiting_strategic_research"
        )


@pytest.mark.asyncio
async def test_set_strategic_context_research_ref(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    iid = row["id"]
    await gtm_svc.set_strategic_context_research_ref(
        iid, "hqx://exa.exa_calls/zzz"
    )
    after = await gtm_svc.get_initiative(iid)
    assert after["strategic_context_research_ref"] == "hqx://exa.exa_calls/zzz"


@pytest.mark.asyncio
async def test_set_campaign_strategy_path(fake_db):
    row = await gtm_svc.create_initiative(
        organization_id=ORG,
        brand_id=BRAND,
        partner_id=PARTNER,
        partner_contract_id=CONTRACT,
        data_engine_audience_id=AUDIENCE,
    )
    iid = row["id"]
    await gtm_svc.set_campaign_strategy_path(iid, "/tmp/strategy.md")
    after = await gtm_svc.get_initiative(iid)
    assert after["campaign_strategy_path"] == "/tmp/strategy.md"
