"""Activation-dispatch tests for the email/emailbison branch.

The DB layer is mocked at the service-function and connection-pool
boundary; we don't run a real DB. The test asserts that the steps
service dispatches to ``EmailBisonAdapter.activate_step`` for an
email/emailbison step and persists the returned external_provider_id.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.models.campaigns import (
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
)
from app.services import channel_campaign_steps as steps_service
from app.services.channel_campaign_steps import (
    StepActivationNotImplemented,
    activate_step,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _step(
    *,
    step_id: UUID | None = None,
    org_id: UUID | None = None,
    cc_id: UUID | None = None,
    status: str = "pending",
) -> ChannelCampaignStepResponse:
    return ChannelCampaignStepResponse(
        id=step_id or uuid4(),
        channel_campaign_id=cc_id or uuid4(),
        campaign_id=uuid4(),
        organization_id=org_id or uuid4(),
        brand_id=uuid4(),
        step_order=1,
        name="t1",
        delay_days_from_previous=0,
        scheduled_send_at=None,
        creative_ref=None,
        channel_specific_config={},
        external_provider_id=None,
        external_provider_metadata={},
        status=status,
        activated_at=None,
        metadata={},
        created_at=_now(),
        updated_at=_now(),
    )


def _cc(
    *,
    cc_id: UUID | None = None,
    org_id: UUID | None = None,
    channel: str = "email",
    provider: str = "emailbison",
) -> ChannelCampaignResponse:
    return ChannelCampaignResponse(
        id=cc_id or uuid4(),
        campaign_id=uuid4(),
        organization_id=org_id or uuid4(),
        brand_id=uuid4(),
        name="cc",
        channel=channel,
        provider=provider,
        audience_spec_id=None,
        audience_snapshot_count=None,
        status="draft",
        start_offset_days=0,
        scheduled_send_at=None,
        schedule_config={},
        provider_config={},
        design_id=None,
        metadata={},
        created_by_user_id=None,
        created_at=_now(),
        updated_at=_now(),
        archived_at=None,
    )


@dataclass
class _FakeCursor:
    update_calls: list[tuple[Any, ...]] = field(default_factory=list)
    return_row: tuple | None = None

    async def execute(self, sql, args=None):
        self.update_calls.append((sql, args))

    async def fetchone(self):
        return self.return_row

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def patch_db_layer(monkeypatch):
    """Wire a minimal fake DB into the steps service so the SQL UPDATE in
    activate_step succeeds without a real Postgres."""
    cursor = _FakeCursor()

    @asynccontextmanager
    async def fake_get_db_connection():
        yield _FakeConn(cursor)

    monkeypatch.setattr(
        steps_service, "get_db_connection", fake_get_db_connection
    )
    return cursor


@pytest.mark.asyncio
async def test_activate_step_dispatches_to_emailbison_adapter(
    monkeypatch, patch_db_layer
):
    org = uuid4()
    cc_id = uuid4()
    step_id = uuid4()
    step = _step(step_id=step_id, org_id=org, cc_id=cc_id)
    cc = _cc(cc_id=cc_id, org_id=org)

    async def fake_get_step(*, step_id, organization_id):
        return step

    async def fake_get_channel_campaign(*, channel_campaign_id, organization_id):
        return cc

    monkeypatch.setattr(steps_service, "get_step", fake_get_step)
    monkeypatch.setattr(
        steps_service, "get_channel_campaign", fake_get_channel_campaign
    )

    adapter_call = {"hit": False}

    from app.providers.emailbison import adapter as eb_adapter
    from app.providers.emailbison.adapter import EmailBisonActivationResult

    async def fake_activate(self, *, step, channel_campaign):
        adapter_call["hit"] = True
        adapter_call["step_id"] = step.id
        adapter_call["channel"] = channel_campaign.channel
        return EmailBisonActivationResult(
            status="scheduled",
            external_provider_id="42",
            metadata={"id": 42},
        )

    monkeypatch.setattr(
        eb_adapter.EmailBisonAdapter, "activate_step", fake_activate
    )

    membership_calls = {"count": 0}

    async def fake_bulk_update(*, channel_campaign_step_id):
        membership_calls["count"] += 1
        return 0

    import app.services.recipients as recipients_module

    monkeypatch.setattr(
        recipients_module,
        "bulk_update_pending_to_scheduled",
        fake_bulk_update,
    )

    # Make the final SELECT-after-UPDATE return a synthetic row mirroring
    # what we expect the service to read back.
    expected_row = (
        step.id,
        step.channel_campaign_id,
        step.campaign_id,
        step.organization_id,
        step.brand_id,
        step.step_order,
        step.name,
        step.delay_days_from_previous,
        step.scheduled_send_at,
        step.creative_ref,
        step.channel_specific_config,
        "42",
        {"id": 42},
        "scheduled",
        _now(),
        step.metadata,
        step.created_at,
        _now(),
    )
    patch_db_layer.return_row = expected_row

    result = await activate_step(step_id=step_id, organization_id=org)

    assert adapter_call["hit"] is True
    assert adapter_call["channel"] == "email"
    assert result.external_provider_id == "42"
    assert result.status == "scheduled"
    assert membership_calls["count"] == 1


@pytest.mark.asyncio
async def test_activate_step_unsupported_channel_provider_raises(
    monkeypatch, patch_db_layer
):
    org = uuid4()
    cc_id = uuid4()
    step_id = uuid4()
    step = _step(step_id=step_id, org_id=org, cc_id=cc_id)
    cc = _cc(cc_id=cc_id, org_id=org, channel="email", provider="manual")

    async def fake_get_step(*, step_id, organization_id):
        return step

    async def fake_get_channel_campaign(*, channel_campaign_id, organization_id):
        return cc

    monkeypatch.setattr(steps_service, "get_step", fake_get_step)
    monkeypatch.setattr(
        steps_service, "get_channel_campaign", fake_get_channel_campaign
    )

    with pytest.raises(StepActivationNotImplemented):
        await activate_step(step_id=step_id, organization_id=org)
