"""Tests for app.dmaas.step_link_minting (bulk-minting path).

Pure-logic tests: monkeypatch the dub HTTP client, the dub_links repo,
list_step_memberships, and the channel_campaigns_dub helper so nothing
hits Dub or the DB.
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
from app.services import channel_campaigns_dub

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


def _link_for(external_id: str, *, folder_id: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": f"link_{external_id[-8:]}",
        "domain": "dub.sh",
        "key": external_id[-6:],
        "url": "https://landing.example.com",
        "shortLink": f"https://dub.sh/{external_id[-6:]}",
        "externalId": external_id,
    }
    if folder_id:
        out["folderId"] = folder_id
    return out


def _record(recipient_id: UUID, *, dub_link_id: str | None = None) -> DubLinkRecord:
    return DubLinkRecord(
        id=uuid4(),
        dub_link_id=dub_link_id or f"link_{recipient_id.hex[:8]}",
        dub_external_id=f"step:{_STEP_ID}:rcpt:{recipient_id}",
        dub_short_url="https://dub.sh/exist",
        dub_domain="dub.sh",
        dub_key="abc",
        destination_url="https://landing.example.com",
        dmaas_design_id=None,
        direct_mail_piece_id=None,
        brand_id=_BRAND_ID,
        channel_campaign_step_id=_STEP_ID,
        recipient_id=recipient_id,
        dub_folder_id="fold_xyz",
        dub_tag_ids=[],
        attribution_context={},
        created_by_user_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


@pytest.fixture
def configured_dub(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "DUB_DEFAULT_DOMAIN", "dub.sh")
    monkeypatch.setattr(settings, "DUB_DEFAULT_TENANT_ID", "hq-x")
    monkeypatch.setattr(settings, "DUB_API_BASE_URL", None)
    yield


@pytest.fixture
def stub_repo(monkeypatch):
    """In-memory replacement for the dmaas_dub_links repo."""
    state: dict[str, Any] = {
        "rows": [],  # list[DubLinkRecord]
        "bulk_calls": [],
    }

    async def fake_list_for_step(step_id):
        return [r for r in state["rows"] if r.channel_campaign_step_id == step_id]

    async def fake_bulk_insert(rows):
        state["bulk_calls"].append(list(rows))
        inserted: list[DubLinkRecord] = []
        for r in rows:
            recipient_id = r.get("recipient_id")
            existing = any(
                row
                for row in state["rows"]
                if row.channel_campaign_step_id
                == r.get("channel_campaign_step_id")
                and row.recipient_id == recipient_id
            )
            if existing:
                continue
            rec = DubLinkRecord(
                id=uuid4(),
                dub_link_id=r["dub_link_id"],
                dub_external_id=r.get("dub_external_id"),
                dub_short_url=r["dub_short_url"],
                dub_domain=r["dub_domain"],
                dub_key=r["dub_key"],
                destination_url=r["destination_url"],
                dmaas_design_id=r.get("dmaas_design_id"),
                direct_mail_piece_id=r.get("direct_mail_piece_id"),
                brand_id=r.get("brand_id"),
                channel_campaign_step_id=r.get("channel_campaign_step_id"),
                recipient_id=recipient_id,
                dub_folder_id=r.get("dub_folder_id"),
                dub_tag_ids=list(r.get("dub_tag_ids") or []),
                attribution_context=r.get("attribution_context") or {},
                created_by_user_id=r.get("created_by_user_id"),
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
            state["rows"].append(rec)
            inserted.append(rec)
        return inserted

    monkeypatch.setattr(dub_links_repo, "list_dub_links_for_step", fake_list_for_step)
    monkeypatch.setattr(dub_links_repo, "bulk_insert_dub_links", fake_bulk_insert)
    monkeypatch.setattr(
        step_link_minting.dub_links_repo,
        "list_dub_links_for_step",
        fake_list_for_step,
    )
    monkeypatch.setattr(
        step_link_minting.dub_links_repo,
        "bulk_insert_dub_links",
        fake_bulk_insert,
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


@pytest.fixture
def stub_folder(monkeypatch):
    """Stub the channel_campaigns_dub helper so we don't touch the DB.

    State holds `existing_folder_id` (returned without calling create_folder)
    or None (in which case create_folder is invoked once and the result
    stored back).
    """
    state: dict[str, Any] = {
        "existing_folder_id": None,
        "create_folder_calls": 0,
        "set_folder_id": None,
    }

    async def fake_acquire(*, channel_campaign_id, create_folder):
        if state["existing_folder_id"] is not None:
            return state["existing_folder_id"]
        folder_id = await create_folder()
        state["set_folder_id"] = folder_id
        state["existing_folder_id"] = folder_id
        return folder_id

    monkeypatch.setattr(
        channel_campaigns_dub, "acquire_or_set_dub_folder_id", fake_acquire
    )
    monkeypatch.setattr(
        step_link_minting.channel_campaigns_dub,
        "acquire_or_set_dub_folder_id",
        fake_acquire,
    )
    return state


def _patch_bulk(monkeypatch, fn):
    monkeypatch.setattr(dub_client, "bulk_create_links", fn)
    monkeypatch.setattr(step_link_minting.dub_client, "bulk_create_links", fn)


def _patch_create_folder(monkeypatch, fn):
    monkeypatch.setattr(dub_client, "create_folder", fn)
    monkeypatch.setattr(step_link_minting.dub_client, "create_folder", fn)


@pytest.mark.asyncio
async def test_bulk_mints_attribution_and_tag_names(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    r1, r2, r3 = uuid4(), uuid4(), uuid4()
    stub_memberships["rows"] = [_membership(r1), _membership(r2), _membership(r3)]
    stub_folder["existing_folder_id"] = "fold_existing"

    bulk_calls: list[list[dict[str, Any]]] = []

    def fake_bulk(*, api_key, links, base_url=None, timeout_seconds=30.0):
        bulk_calls.append(list(links))
        return [_link_for(spec["external_id"], folder_id="fold_existing") for spec in links]

    _patch_bulk(monkeypatch, fake_bulk)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    assert len(out) == 3
    assert len(bulk_calls) == 1
    spec = bulk_calls[0][0]
    assert spec["url"] == "https://landing.example.com"
    assert spec["external_id"].startswith(f"step:{_STEP_ID}:rcpt:")
    assert spec["folder_id"] == "fold_existing"
    assert spec["domain"] == "dub.sh"
    assert spec["tenant_id"] == "hq-x"
    assert f"step:{_STEP_ID}" in spec["tag_names"]
    assert f"campaign:{_CHANNEL_CAMPAIGN_ID}" in spec["tag_names"]
    assert f"brand:{_BRAND_ID}" in spec["tag_names"]
    assert spec["utm_source"] == "dub"
    assert spec["utm_medium"] == "direct_mail"
    assert spec["utm_campaign"] == str(_CHANNEL_CAMPAIGN_ID)

    inserted = stub_repo["rows"]
    assert len(inserted) == 3
    rec0 = inserted[0]
    assert rec0.attribution_context["channel_campaign_step_id"] == str(_STEP_ID)
    assert rec0.attribution_context["organization_id"] == str(_ORG_ID)


@pytest.mark.asyncio
async def test_chunks_at_100(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    recipients = [uuid4() for _ in range(250)]
    stub_memberships["rows"] = [_membership(r) for r in recipients]
    stub_folder["existing_folder_id"] = "fold_x"

    chunk_sizes: list[int] = []

    def fake_bulk(*, api_key, links, base_url=None, timeout_seconds=30.0):
        chunk_sizes.append(len(links))
        return [_link_for(spec["external_id"]) for spec in links]

    _patch_bulk(monkeypatch, fake_bulk)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    assert chunk_sizes == [100, 100, 50]
    assert len(out) == 250


@pytest.mark.asyncio
async def test_resolves_existing_folder(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    r1 = uuid4()
    stub_memberships["rows"] = [_membership(r1)]
    stub_folder["existing_folder_id"] = "fold_already_set"

    create_calls: list[Any] = []

    def fake_create_folder(**kwargs):
        create_calls.append(kwargs)
        return {"id": "fold_new"}

    _patch_create_folder(monkeypatch, fake_create_folder)
    _patch_bulk(
        monkeypatch,
        lambda **kw: [_link_for(spec["external_id"]) for spec in kw["links"]],
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    assert create_calls == []  # no folder creation when already set


@pytest.mark.asyncio
async def test_creates_folder_when_missing(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    r1 = uuid4()
    stub_memberships["rows"] = [_membership(r1)]
    stub_folder["existing_folder_id"] = None

    create_calls: list[dict[str, Any]] = []

    def fake_create_folder(**kwargs):
        create_calls.append(kwargs)
        return {"id": "fold_brand_new"}

    _patch_create_folder(monkeypatch, fake_create_folder)
    _patch_bulk(
        monkeypatch,
        lambda **kw: [_link_for(spec["external_id"]) for spec in kw["links"]],
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    assert len(create_calls) == 1
    assert create_calls[0]["name"] == f"campaign:{_CHANNEL_CAMPAIGN_ID}"
    assert stub_folder["set_folder_id"] == "fold_brand_new"


@pytest.mark.asyncio
async def test_partial_failure_in_batch_raises(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    recipients = [uuid4() for _ in range(8)]
    stub_memberships["rows"] = [_membership(r) for r in recipients]
    stub_folder["existing_folder_id"] = "fold_x"

    def fake_bulk(*, api_key, links, base_url=None, timeout_seconds=30.0):
        # Position 5 fails.
        out: list[dict[str, Any]] = []
        for idx, spec in enumerate(links):
            if idx == 5:
                out.append({"error": {"code": "conflict", "message": "key in use"}})
            else:
                out.append(_link_for(spec["external_id"]))
        return out

    _patch_bulk(monkeypatch, fake_bulk)

    with pytest.raises(StepLinkMintingError) as exc_info:
        await mint_links_for_step(
            channel_campaign_step_id=_STEP_ID,
            organization_id=_ORG_ID,
            brand_id=_BRAND_ID,
            campaign_id=_CAMPAIGN_ID,
            channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
            destination_url="https://landing.example.com",
        )
    # Recipient at position 5 was the one that failed.
    assert exc_info.value.recipient_id == recipients[5]


@pytest.mark.asyncio
async def test_idempotent_retry_after_partial(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    recipients = [uuid4() for _ in range(4)]
    stub_memberships["rows"] = [_membership(r) for r in recipients]
    stub_folder["existing_folder_id"] = "fold_x"

    # Pre-seed half the recipients as already minted.
    stub_repo["rows"].extend([_record(recipients[0]), _record(recipients[1])])

    bulk_calls: list[list[dict[str, Any]]] = []

    def fake_bulk(*, api_key, links, base_url=None, timeout_seconds=30.0):
        bulk_calls.append(list(links))
        return [_link_for(spec["external_id"]) for spec in links]

    _patch_bulk(monkeypatch, fake_bulk)

    out = await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    # Only the missing 2 went through bulk_create.
    assert len(bulk_calls) == 1
    assert len(bulk_calls[0]) == 2
    # Final list covers all 4 recipients.
    assert len(out) == 4
    assert {r.recipient_id for r in out} == set(recipients)


@pytest.mark.asyncio
async def test_bulk_failure_raises(
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    r1, r2 = uuid4(), uuid4()
    stub_memberships["rows"] = [_membership(r1), _membership(r2)]
    stub_folder["existing_folder_id"] = "fold_x"

    def fake_bulk(**_):
        raise DubProviderError("boom", status=500)

    _patch_bulk(monkeypatch, fake_bulk)

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
    configured_dub, stub_repo, stub_memberships, stub_folder, monkeypatch
):
    stub_memberships["rows"] = []
    stub_folder["existing_folder_id"] = "fold_x"

    def fake_bulk(**_):
        raise AssertionError("should not be called")

    _patch_bulk(monkeypatch, fake_bulk)

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
