"""Verify step minting picks up the brand's custom Dub domain when set.

Resolution order in `mint_links_for_step`:
  1. explicit `domain=` argument (caller override)
  2. brand's `dub_domain_config.domain` (from brand_domains_svc)
  3. settings.DUB_DEFAULT_DOMAIN (workspace default)
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
from app.dmaas.step_link_minting import mint_links_for_step
from app.models.recipients import StepRecipientResponse
from app.providers.dub import client as dub_client
from app.services import brand_domains as brand_domains_svc
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


def _link_for(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "link_x",
        "domain": spec.get("domain", "dub.sh"),
        "key": "abc",
        "url": spec["url"],
        "shortLink": f"https://{spec.get('domain', 'dub.sh')}/abc",
        "externalId": spec["external_id"],
    }


@pytest.fixture
def base_setup(monkeypatch):
    monkeypatch.setattr(settings, "DUB_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(settings, "DUB_DEFAULT_DOMAIN", "dub.sh")
    monkeypatch.setattr(settings, "DUB_DEFAULT_TENANT_ID", "hq-x")
    monkeypatch.setattr(settings, "DUB_API_BASE_URL", None)

    state: dict[str, Any] = {"rows": [], "calls": []}

    async def fake_list_for_step(step_id):
        return [r for r in state["rows"] if r.channel_campaign_step_id == step_id]

    async def fake_bulk_insert(rows):
        for r in rows:
            state["rows"].append(
                DubLinkRecord(
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
                    recipient_id=r.get("recipient_id"),
                    dub_folder_id=r.get("dub_folder_id"),
                    dub_tag_ids=list(r.get("dub_tag_ids") or []),
                    attribution_context=r.get("attribution_context") or {},
                    created_by_user_id=r.get("created_by_user_id"),
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
        return state["rows"][-len(rows):]

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
    monkeypatch.setattr(dub_links_repo, "list_dub_links_for_step", fake_list_for_step)
    monkeypatch.setattr(dub_links_repo, "bulk_insert_dub_links", fake_bulk_insert)

    async def fake_list_memberships(*, channel_campaign_step_id, status=None):
        return [_membership(uuid4()), _membership(uuid4())]

    monkeypatch.setattr(step_link_minting, "list_step_memberships", fake_list_memberships)

    async def fake_acquire(*, channel_campaign_id, create_folder):
        return "fold_existing"

    monkeypatch.setattr(channel_campaigns_dub, "acquire_or_set_dub_folder_id", fake_acquire)
    monkeypatch.setattr(
        step_link_minting.channel_campaigns_dub,
        "acquire_or_set_dub_folder_id",
        fake_acquire,
    )

    def fake_bulk_create(*, api_key, links, base_url=None, timeout_seconds=30.0):
        state["calls"].append([dict(spec) for spec in links])
        return [_link_for(spec) for spec in links]

    monkeypatch.setattr(dub_client, "bulk_create_links", fake_bulk_create)
    monkeypatch.setattr(step_link_minting.dub_client, "bulk_create_links", fake_bulk_create)

    return state


@pytest.mark.asyncio
async def test_uses_brand_dub_domain_when_configured(base_setup, monkeypatch):
    """When brand has a dub_domain_config, mint specs use that domain."""

    async def fake_get(*, brand_id):
        assert brand_id == _BRAND_ID
        return "track.acme.com"

    monkeypatch.setattr(brand_domains_svc, "get_brand_dub_domain", fake_get)
    monkeypatch.setattr(
        step_link_minting.brand_domains_svc, "get_brand_dub_domain", fake_get
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    spec = base_setup["calls"][0][0]
    assert spec["domain"] == "track.acme.com"


@pytest.mark.asyncio
async def test_falls_back_to_default_when_no_brand_domain(base_setup, monkeypatch):
    """No brand-level config → settings.DUB_DEFAULT_DOMAIN wins."""

    async def fake_get(*, brand_id):
        return None

    monkeypatch.setattr(brand_domains_svc, "get_brand_dub_domain", fake_get)
    monkeypatch.setattr(
        step_link_minting.brand_domains_svc, "get_brand_dub_domain", fake_get
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    spec = base_setup["calls"][0][0]
    assert spec["domain"] == "dub.sh"


@pytest.mark.asyncio
async def test_explicit_domain_overrides_brand_config(base_setup, monkeypatch):
    """Caller-provided `domain=` wins over the brand's config."""

    called = {"hit": False}

    async def fake_get(*, brand_id):
        called["hit"] = True
        return "should-not-be-used.com"

    monkeypatch.setattr(brand_domains_svc, "get_brand_dub_domain", fake_get)
    monkeypatch.setattr(
        step_link_minting.brand_domains_svc, "get_brand_dub_domain", fake_get
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=_BRAND_ID,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
        domain="explicit.example.com",
    )
    spec = base_setup["calls"][0][0]
    assert spec["domain"] == "explicit.example.com"
    # Lookup short-circuited because explicit domain was supplied.
    assert called["hit"] is False


@pytest.mark.asyncio
async def test_no_brand_id_skips_brand_lookup(base_setup, monkeypatch):
    """Without brand_id, the brand-domain lookup is never attempted."""

    called = {"hit": False}

    async def fake_get(*, brand_id):
        called["hit"] = True
        return "should-not-be-used.com"

    monkeypatch.setattr(brand_domains_svc, "get_brand_dub_domain", fake_get)
    monkeypatch.setattr(
        step_link_minting.brand_domains_svc, "get_brand_dub_domain", fake_get
    )

    await mint_links_for_step(
        channel_campaign_step_id=_STEP_ID,
        organization_id=_ORG_ID,
        brand_id=None,
        campaign_id=_CAMPAIGN_ID,
        channel_campaign_id=_CHANNEL_CAMPAIGN_ID,
        destination_url="https://landing.example.com",
    )
    spec = base_setup["calls"][0][0]
    assert spec["domain"] == "dub.sh"
    assert called["hit"] is False
