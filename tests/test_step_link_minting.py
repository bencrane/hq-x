"""Tests for app.dmaas.step_link_minting.

Pure-logic tests: monkeypatch the dub HTTP client, the dub_links repo, and
list_step_memberships so nothing hits Dub or the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.config import settings
from app.dmaas import dub_links as dub_links_repo
from app.dmaas import step_link_minting
from app.dmaas.dub_links import DubLinkRecord
from app.dmaas.step_link_minting import (
    DubNotConfiguredError,
    StepLinkMintingError,
    mint_links_for_step,
)
from app.models.recipients import StepRecipientResponse
from app.providers.dub import client as dub_client
from app.providers.dub.client import DubProviderError


_STEP_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ORG_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
_BRAND_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
_CAMPAIGN_ID = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
_CHANNEL_CAMPAIGN_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")


def _membership(recipient_id: UUID) -> StepRecipientResponse:
    return StepRecipientResponse(
        id=uuid4(),
        channel_campaign_step_id=_STEP_ID,
        recipient_id=recipient_id,
        organization_id=_ORG_ID,
        status="pending",
        scheduled_for=None,
        processed_at=None,
        error_reason=None,
        metadata={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def _dub_payload(external_id: str) -> dict[str, Any]:
    return {
        "id": f"link_{external_id}",
        "domain": "dub.sh",
        "key": "abc",
        "url": "https://example.com",
        "shortLink": f"https://dub.sh/{external_id[-6:]}",
        "externalId": external_id,
    }


@pytest.fixture
def configured_dub(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "DUB_DEFAULT_DOMAIN", "dub.sh")
    monkeypatch.setattr(settings, "DUB_DEFAULT_TENANT_ID", "hq-x")
    yield


@pytest.fixture
def stub_repo(monkeypatch):
    state: dict[str, Any] = {"inserts": [], "existing": {}}

    async def fake_find(*, channel_campaign_step_id, recipient_id):
        return state["existing"].get((channel_campaign_step_id, recipient_id))

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
            attribution_context=kwargs.get("attribution_context") or {},
            created_by_user_id=kwargs.get("created_by_user_id"),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        state["inserts"].append((kwargs, rec))
        return rec

    monkeypatch.setattr(
        dub_links_repo, "find_dub_link_for_step_recipient", fake_find
    )
    monkeypatch.setattr(dub_links_repo, "insert_dub_link", fake_insert)
    monkeypatch.setattr(
        step_link_minting.dub_links_repo,
        "find_dub_link_for_step_recipient",
        fake_find,
    )
    monkeypatch.setattr(
        step_link_minting.dub_links_repo, "insert_dub_link", fake_insert
    )
    return state


@pytest.fixture
def stub_memberships(monkeypatch):
    state: dict[str, list[StepRecipientResponse]] = {"rows": []}

    async def fake_list(*, channel_campaign_step_id, status=None):
        rows = list(state["rows"])
        if status is not None:
            rows = [r for r in rows if r.status == status]
        state["last_call"] = {  # type: ignore[assignment]
            "channel_campaign_step_id": channel_campaign_step_id,
            "status": status,
        }
        return rows

    monkeypatch.setattr(step_link_minting, "list_step_memberships", fake_list)
    return state


@pytest.mark.asyncio
async def test_mints_one_link_per_pending_recipient(
    configured_dub, stub_repo, stub_memberships, monkeypatch
):
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    stub_memberships["rows"] = [_membership(r1), _membership(r2), _membership(r3)]

    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _dub_payload(kwargs["external_id"])

    monkeypatch.setattr(dub_client, "create_link", fake_create)
    monkeypatch.setattr(step_link_minting.dub_client, "create_link", fake_create)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )

    assert len(out) == 3
    assert len(calls) == 3
    assert len(stub_repo["inserts"]) == 3

    expected_external_ids = {
        f"step:{_STEP_ID}:rcpt:{r}" for r in (r1, r2, r3)
    }
    assert {c["external_id"] for c in calls} == expected_external_ids

    first_kwargs, _ = stub_repo["inserts"][0]
    attribution = first_kwargs["attribution_context"]
    assert attribution["campaign_id"] == str(_CAMPAIGN_ID)
    assert attribution["channel_campaign_id"] == str(_CHANNEL_CAMPAIGN_ID)
    assert attribution["channel_campaign_step_id"] == str(_STEP_ID)
    assert attribution["organization_id"] == str(_ORG_ID)
    assert "recipient_id" in attribution


@pytest.mark.asyncio
async def test_idempotent_skip_existing(
    configured_dub, stub_repo, stub_memberships, monkeypatch
):
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    stub_memberships["rows"] = [_membership(r1), _membership(r2), _membership(r3)]

    existing = DubLinkRecord(
        id=uuid4(),
        dub_link_id="link_existing",
        dub_external_id=f"step:{_STEP_ID}:rcpt:{r2}",
        dub_short_url="https://dub.sh/exist",
        dub_domain="dub.sh",
        dub_key="exist",
        destination_url="https://landing.example.com",
        dmaas_design_id=None,
        direct_mail_piece_id=None,
        brand_id=_BRAND_ID,
        channel_campaign_step_id=_STEP_ID,
        recipient_id=r2,
        attribution_context={},
        created_by_user_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    stub_repo["existing"][(_STEP_ID, r2)] = existing

    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        return _dub_payload(kwargs["external_id"])

    monkeypatch.setattr(dub_client, "create_link", fake_create)
    monkeypatch.setattr(step_link_minting.dub_client, "create_link", fake_create)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )

    assert len(out) == 3
    assert len(calls) == 2
    assert existing in out


@pytest.mark.asyncio
async def test_dub_failure_raises_step_link_minting_error(
    configured_dub, stub_repo, stub_memberships, monkeypatch
):
    r1, r2 = uuid4(), uuid4()
    stub_memberships["rows"] = [_membership(r1), _membership(r2)]

    calls: list[dict[str, Any]] = []

    def fake_create(**kwargs):
        calls.append(kwargs)
        raise DubProviderError("boom", status=500)

    monkeypatch.setattr(dub_client, "create_link", fake_create)
    monkeypatch.setattr(step_link_minting.dub_client, "create_link", fake_create)

    with pytest.raises(StepLinkMintingError) as exc_info:
        await mint_links_for_step(
            channel_campaign_step_id=_STEP_ID,
            organization_id=_ORG_ID,
            brand_id=_BRAND_ID,
            campaign_id=_CAMPAIGN_ID,
            channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
            destination_url="https://landing.example.com",
        )

    assert exc_info.value.recipient_id == r1
    assert len(calls) == 1  # stopped at first failure
    assert len(stub_repo["inserts"]) == 0


@pytest.mark.asyncio
async def test_persistence_failure_after_mint_raises(
    configured_dub, stub_memberships, monkeypatch
):
    r1 = uuid4()
    stub_memberships["rows"] = [_membership(r1)]

    async def fake_find(**kwargs):
        return None

    async def fake_insert(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        dub_links_repo, "find_dub_link_for_step_recipient", fake_find
    )
    monkeypatch.setattr(dub_links_repo, "insert_dub_link", fake_insert)
    monkeypatch.setattr(
        step_link_minting.dub_links_repo,
        "find_dub_link_for_step_recipient",
        fake_find,
    )
    monkeypatch.setattr(
        step_link_minting.dub_links_repo, "insert_dub_link", fake_insert
    )

    def fake_create(**kwargs):
        return _dub_payload(kwargs["external_id"])

    monkeypatch.setattr(dub_client, "create_link", fake_create)
    monkeypatch.setattr(step_link_minting.dub_client, "create_link", fake_create)

    with pytest.raises(StepLinkMintingError) as exc_info:
        await mint_links_for_step(
            channel_campaign_step_id=_STEP_ID,
            organization_id=_ORG_ID,
            brand_id=_BRAND_ID,
            campaign_id=_CAMPAIGN_ID,
            channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
            destination_url="https://landing.example.com",
        )
    assert exc_info.value.recipient_id == r1
    assert isinstance(exc_info.value.cause, RuntimeError)


@pytest.mark.asyncio
async def test_dub_not_configured_when_no_api_key(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", None)

    called = {"hit": False}

    async def fake_list(*, channel_campaign_step_id, status=None):
        called["hit"] = True
        return []

    monkeypatch.setattr(step_link_minting, "list_step_memberships", fake_list)

    with pytest.raises(DubNotConfiguredError):
        await mint_links_for_step(
            channel_campaign_step_id=_STEP_ID,
            organization_id=_ORG_ID,
            brand_id=_BRAND_ID,
            campaign_id=_CAMPAIGN_ID,
            channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
            destination_url="https://landing.example.com",
        )
    assert called["hit"] is False


@pytest.mark.asyncio
async def test_skips_non_pending_memberships(
    configured_dub, stub_repo, stub_memberships, monkeypatch
):
    stub_memberships["rows"] = []

    def fake_create(**kwargs):
        raise AssertionError("should not be called")

    monkeypatch.setattr(dub_client, "create_link", fake_create)
    monkeypatch.setattr(step_link_minting.dub_client, "create_link", fake_create)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    assert out == []
    assert stub_memberships["last_call"]["status"] == "pending"  # type: ignore[index]
    assert (
        stub_memberships["last_call"]["channel_campaign_step_id"]  # type: ignore[index]
        == _STEP_ID
    )
