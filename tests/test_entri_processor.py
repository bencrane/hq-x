"""Entri webhook projector tests — state-machine on entri_domain_connections.

Stubs the entri_domains repository so the suite has no DB dependency.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.dmaas import entri_domains
from app.dmaas.entri_domains import EntriDomainConnection
from app.webhooks import entri_processor


def _conn(state: str = "pending_modal", **overrides: Any) -> EntriDomainConnection:
    base = EntriDomainConnection(
        id=uuid4(),
        organization_id=uuid4(),
        channel_campaign_step_id=None,
        domain="qr.acme.com",
        is_root_domain=False,
        application_url="https://app.example.com/lp/1",
        state=state,
        entri_user_id="org_1:step_1",
        entri_token=None,
        entri_token_expires_at=None,
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
    return replace(base, **overrides)


@pytest.fixture
def stub_repo(monkeypatch):
    state: dict[str, Any] = {"conn": None, "updates": []}

    async def fake_get(user_id: str):
        return state["conn"]

    async def fake_update(connection_id: UUID, **fields):
        state["updates"].append({"id": connection_id, **fields})
        if state["conn"] is None:
            return None
        new_state = fields.get("state") or state["conn"].state
        state["conn"] = replace(state["conn"], state=new_state)
        return state["conn"]

    monkeypatch.setattr(entri_domains, "get_by_entri_user_id", fake_get)
    monkeypatch.setattr(entri_domains, "update_state", fake_update)
    # entri_processor imports the names directly, so patch there too.
    monkeypatch.setattr(entri_processor.entri_domains, "get_by_entri_user_id", fake_get)
    monkeypatch.setattr(entri_processor.entri_domains, "update_state", fake_update)
    return state


@pytest.mark.asyncio
async def test_no_user_id_skips(stub_repo):
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.added"},
        event_id="e1",
        event_type="domain.added",
        webhook_event_id=uuid4(),
    )
    assert result["status"] == "no_user_id"


@pytest.mark.asyncio
async def test_unknown_user_id_skips(stub_repo):
    stub_repo["conn"] = None
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.added", "user_id": "unknown:step"},
        event_id="e1",
        event_type="domain.added",
        webhook_event_id=uuid4(),
    )
    assert result["status"] == "connection_not_found"


@pytest.mark.asyncio
async def test_flow_completed_moves_to_dns_records_submitted(stub_repo):
    stub_repo["conn"] = _conn(state="pending_modal")
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.flow.completed", "user_id": "x:y"},
        event_id="e1",
        event_type="domain.flow.completed",
        webhook_event_id=uuid4(),
    )
    assert result["new_state"] == "dns_records_submitted"


@pytest.mark.asyncio
async def test_domain_added_to_live_when_power_and_secure_success(stub_repo):
    stub_repo["conn"] = _conn(state="dns_records_submitted")
    result = await entri_processor.project_entri_event(
        payload={
            "id": "e1",
            "type": "domain.added",
            "user_id": "x:y",
            "power_status": "success",
            "secure_status": "success",
        },
        event_id="e1",
        event_type="domain.added",
        webhook_event_id=uuid4(),
    )
    assert result["new_state"] == "live"


@pytest.mark.asyncio
async def test_propagation_timeout_to_failed(stub_repo):
    stub_repo["conn"] = _conn(state="dns_records_submitted")
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.propagation.timeout", "user_id": "x:y"},
        event_id="e1",
        event_type="domain.propagation.timeout",
        webhook_event_id=uuid4(),
    )
    assert result["new_state"] == "failed"


@pytest.mark.asyncio
async def test_record_missing_only_demotes_live(stub_repo):
    # Already failed → record_missing is a no-op state-wise.
    stub_repo["conn"] = _conn(state="failed")
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.record_missing", "user_id": "x:y"},
        event_id="e1",
        event_type="domain.record_missing",
        webhook_event_id=uuid4(),
    )
    assert result["new_state"] == "failed"  # unchanged


@pytest.mark.asyncio
async def test_record_restored_brings_live_back(stub_repo):
    stub_repo["conn"] = _conn(state="failed")
    result = await entri_processor.project_entri_event(
        payload={"id": "e1", "type": "domain.record_restored", "user_id": "x:y"},
        event_id="e1",
        event_type="domain.record_restored",
        webhook_event_id=uuid4(),
    )
    assert result["new_state"] == "live"
