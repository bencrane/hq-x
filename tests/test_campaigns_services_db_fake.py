"""Service-layer tests for campaigns + channel_campaigns against an
in-memory DB fake.

Validates that:
  * create_campaign / get_campaign / list_campaigns / update_campaign /
    archive_campaign enforce org scoping and brand/org consistency.
  * create_channel_campaign rejects unknown channel/provider combos and
    bad designs.
  * activate_channel_campaign computes scheduled_send_at from
    campaign.start_date.

The fake intercepts ``get_db_connection`` in both service modules and
dispatches each query against a Python dict. It is intentionally minimal —
just enough to cover the SQL the services actually emit.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.models.campaigns import (
    CampaignCreate,
    CampaignUpdate,
    ChannelCampaignCreate,
    ChannelCampaignUpdate,
)
from app.services import campaigns as campaigns_service
from app.services import channel_campaigns as channel_campaigns_service
from app.services.campaigns import (
    CampaignBrandMismatch,
    CampaignNotFound,
    archive_campaign,
    create_campaign,
    get_campaign,
    list_campaigns,
    update_campaign,
)
from app.services.channel_campaigns import (
    ChannelCampaignChannelProviderInvalid,
    ChannelCampaignDesignBrandMismatch,
    ChannelCampaignDesignRequired,
    ChannelCampaignInvalidStatusTransition,
    ChannelCampaignNotFound,
    activate_channel_campaign,
    archive_channel_campaign,
    create_channel_campaign,
    get_channel_campaign,
    list_channel_campaigns,
    pause_channel_campaign,
    resume_channel_campaign,
    update_channel_campaign,
)

# ── In-memory store ──────────────────────────────────────────────────────


@dataclass
class _Store:
    brands: dict[UUID, UUID] = field(default_factory=dict)  # brand_id → org_id
    designs: dict[UUID, UUID] = field(default_factory=dict)  # design_id → brand_id
    campaigns: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    channel_campaigns: dict[UUID, dict[str, Any]] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


# ── Fake cursor ───────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, store: _Store):
        self._s = store
        self._row: tuple | None = None
        self._rows: list[tuple] = []
        self.rowcount = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    # Helpers --------------------------------------------------------------

    def _campaign_row(self, m: dict[str, Any]) -> tuple:
        return (
            m["id"], m["organization_id"], m["brand_id"], m["name"],
            m["description"], m["status"], m["start_date"], m["metadata"],
            m["created_by_user_id"], m["created_at"], m["updated_at"],
            m["archived_at"],
        )

    def _channel_campaign_row(self, c: dict[str, Any]) -> tuple:
        return (
            c["id"], c["campaign_id"], c["organization_id"], c["brand_id"],
            c["name"], c["channel"], c["provider"],
            c["audience_spec_id"], c["audience_snapshot_count"],
            c["status"], c["start_offset_days"], c["scheduled_send_at"],
            c["schedule_config"], c["provider_config"],
            c["design_id"], c["metadata"],
            c["created_by_user_id"], c["created_at"], c["updated_at"],
            c["archived_at"],
        )

    # Routing --------------------------------------------------------------

    async def execute(self, sql: str, params) -> None:  # noqa: PLR0912, PLR0915
        s = _norm(sql)

        # ── brands.organization_id check
        if s.startswith("SELECT 1 FROM business.brands"):
            brand_id, org_id = UUID(params[0]), UUID(params[1])
            self._row = (1,) if self._s.brands.get(brand_id) == org_id else None
            return

        # ── dmaas_designs lookup
        if s.startswith("SELECT brand_id FROM dmaas_designs"):
            design_id = UUID(params[0])
            brand_id = self._s.designs.get(design_id)
            self._row = (brand_id,) if brand_id else None
            return

        # ── campaigns (umbrella)
        if s.startswith("INSERT INTO business.campaigns"):
            (org, brand, name, desc, start, meta, owner) = params
            m = {
                "id": uuid4(),
                "organization_id": UUID(org),
                "brand_id": UUID(brand),
                "name": name,
                "description": desc,
                "status": "draft",
                "start_date": start,
                "metadata": getattr(meta, "obj", {}) or {},
                "created_by_user_id": UUID(owner) if owner else None,
                "created_at": _now(),
                "updated_at": _now(),
                "archived_at": None,
            }
            self._s.campaigns[m["id"]] = m
            self._row = self._campaign_row(m)
            return

        camp_select_prefix = (
            "SELECT id, organization_id, brand_id, name, description, status,"
            " start_date,"
        )
        if (
            s.startswith(camp_select_prefix)
            and "FROM business.campaigns" in s
            and "WHERE id = %s AND organization_id = %s" in s
        ):
            mid, org = UUID(params[0]), UUID(params[1])
            m = self._s.campaigns.get(mid)
            self._row = (
                self._campaign_row(m) if m and m["organization_id"] == org else None
            )
            return

        if (
            s.startswith(camp_select_prefix)
            and "FROM business.campaigns" in s
            and "ORDER BY created_at DESC" in s
        ):
            org = UUID(params[0])
            rows = [m for m in self._s.campaigns.values() if m["organization_id"] == org]
            idx = 1
            if "brand_id = %s" in s:
                rows = [m for m in rows if m["brand_id"] == UUID(params[idx])]
                idx += 1
            if "status = %s" in s:
                rows = [m for m in rows if m["status"] == params[idx]]
                idx += 1
            limit, offset = params[idx], params[idx + 1]
            rows = sorted(rows, key=lambda m: m["created_at"], reverse=True)[
                offset : offset + limit
            ]
            self._rows = [self._campaign_row(m) for m in rows]
            return

        if s.startswith("UPDATE business.campaigns") and "SET status = 'archived'" in s:
            mid, org = UUID(params[0]), UUID(params[1])
            m = self._s.campaigns.get(mid)
            if m and m["organization_id"] == org:
                m["status"] = "archived"
                m["archived_at"] = m["archived_at"] or _now()
                m["updated_at"] = _now()
                self._row = self._campaign_row(m)
            else:
                self._row = None
            return

        if s.startswith("UPDATE business.campaigns") and "RETURNING" in s:
            # generic update path (PATCH).
            mid, org = UUID(params[-2]), UUID(params[-1])
            m = self._s.campaigns.get(mid)
            if m and m["organization_id"] == org:
                set_clause = s.split(" SET ", 1)[1].split(" WHERE ")[0]
                parts = [p.strip() for p in set_clause.split(", ")]
                values = list(params[:-2])
                for part, val in zip(parts, values, strict=False):
                    col = part.split(" = ")[0]
                    if col == "updated_at":
                        continue
                    if col == "metadata" and hasattr(val, "obj"):
                        m[col] = val.obj or {}
                    else:
                        m[col] = val
                m["updated_at"] = _now()
                self._row = self._campaign_row(m)
            else:
                self._row = None
            return

        # cascade-archive child channel_campaigns from archive_campaign
        if (
            s.startswith("UPDATE business.channel_campaigns SET status = 'archived'")
            and "WHERE campaign_id = %s" in s
        ):
            (cid,) = params
            cid = UUID(cid)
            for c in self._s.channel_campaigns.values():
                if c["campaign_id"] == cid and c["status"] != "archived":
                    c["status"] = "archived"
                    c["archived_at"] = c["archived_at"] or _now()
                    c["updated_at"] = _now()
            return

        # ── channel_campaigns
        if s.startswith("INSERT INTO business.channel_campaigns"):
            (
                campaign_id, org, brand, name, channel, provider,
                aud_spec, aud_count, offset_days, sched_cfg, prov_cfg,
                design, meta, owner,
            ) = params
            c = {
                "id": uuid4(),
                "campaign_id": UUID(campaign_id),
                "organization_id": UUID(org),
                "brand_id": UUID(brand),
                "name": name,
                "channel": channel,
                "provider": provider,
                "audience_spec_id": UUID(aud_spec) if aud_spec else None,
                "audience_snapshot_count": aud_count,
                "status": "draft",
                "start_offset_days": offset_days,
                "scheduled_send_at": None,
                "schedule_config": getattr(sched_cfg, "obj", {}) or {},
                "provider_config": getattr(prov_cfg, "obj", {}) or {},
                "design_id": UUID(design) if design else None,
                "metadata": getattr(meta, "obj", {}) or {},
                "created_by_user_id": UUID(owner) if owner else None,
                "created_at": _now(),
                "updated_at": _now(),
                "archived_at": None,
            }
            self._s.channel_campaigns[c["id"]] = c
            self._row = self._channel_campaign_row(c)
            return

        cc_select_prefix = "SELECT id, campaign_id, organization_id"
        if (
            s.startswith(cc_select_prefix)
            and "FROM business.channel_campaigns" in s
            and "WHERE id = %s AND organization_id = %s" in s
        ):
            cid, org = UUID(params[0]), UUID(params[1])
            c = self._s.channel_campaigns.get(cid)
            self._row = (
                self._channel_campaign_row(c)
                if c and c["organization_id"] == org
                else None
            )
            return

        if (
            s.startswith(cc_select_prefix)
            and "ORDER BY created_at DESC" in s
        ):
            org = UUID(params[0])
            rows = [
                c
                for c in self._s.channel_campaigns.values()
                if c["organization_id"] == org
            ]
            idx = 1
            if "campaign_id = %s" in s:
                rows = [c for c in rows if c["campaign_id"] == UUID(params[idx])]
                idx += 1
            if "channel = %s" in s:
                rows = [c for c in rows if c["channel"] == params[idx]]
                idx += 1
            if "status = %s" in s:
                rows = [c for c in rows if c["status"] == params[idx]]
                idx += 1
            limit, offset = params[idx], params[idx + 1]
            rows = sorted(rows, key=lambda c: c["created_at"], reverse=True)[
                offset : offset + limit
            ]
            self._rows = [self._channel_campaign_row(c) for c in rows]
            return

        if (
            s.startswith("UPDATE business.channel_campaigns")
            and "SET status = 'scheduled'" in s
        ):
            (sched, cid, org) = params
            c = self._s.channel_campaigns.get(UUID(cid))
            if c and c["organization_id"] == UUID(org):
                c["status"] = "scheduled"
                c["scheduled_send_at"] = sched
                c["updated_at"] = _now()
                self._row = self._channel_campaign_row(c)
            else:
                self._row = None
            return

        if (
            s.startswith("UPDATE business.channel_campaigns")
            and "SET status = %s" in s
        ):
            new_status = params[0]
            cid, org = UUID(params[1]), UUID(params[2])
            c = self._s.channel_campaigns.get(cid)
            if c and c["organization_id"] == org:
                c["status"] = new_status
                if "archived_at" in s:
                    c["archived_at"] = c["archived_at"] or _now()
                c["updated_at"] = _now()
                self._row = self._channel_campaign_row(c)
            else:
                self._row = None
            return

        if s.startswith("UPDATE business.channel_campaigns") and "RETURNING" in s:
            cid, org = UUID(params[-2]), UUID(params[-1])
            c = self._s.channel_campaigns.get(cid)
            if c and c["organization_id"] == org:
                set_clause = s.split(" SET ", 1)[1].split(" WHERE ")[0]
                parts = [p.strip() for p in set_clause.split(", ")]
                values = list(params[:-2])
                for part, val in zip(parts, values, strict=False):
                    col = part.split(" = ")[0]
                    if col == "updated_at":
                        continue
                    json_cols = ("schedule_config", "provider_config", "metadata")
                    if col in json_cols and hasattr(val, "obj"):
                        c[col] = val.obj or {}
                    elif col in ("audience_spec_id", "design_id"):
                        c[col] = UUID(val) if val else None
                    else:
                        c[col] = val
                c["updated_at"] = _now()
                self._row = self._channel_campaign_row(c)
            else:
                self._row = None
            return

        if s.startswith("SELECT organization_id, brand_id, campaign_id, channel, provider"):
            cid = UUID(params[0])
            c = self._s.channel_campaigns.get(cid)
            if c is None:
                self._row = None
            else:
                self._row = (
                    c["organization_id"], c["brand_id"], c["campaign_id"],
                    c["channel"], c["provider"],
                )
            return

        raise AssertionError(f"unhandled SQL: {s}")

    async def fetchone(self):
        return self._row

    async def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, store: _Store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    async def commit(self):
        return None


@pytest.fixture
def store(monkeypatch):
    s = _Store()

    @asynccontextmanager
    async def fake_get_db():
        yield _FakeConn(s)

    monkeypatch.setattr(campaigns_service, "get_db_connection", fake_get_db)
    monkeypatch.setattr(channel_campaigns_service, "get_db_connection", fake_get_db)
    return s


# ── Tests: campaigns (umbrella) ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_campaign_happy_path(store: _Store) -> None:
    org = uuid4()
    brand = uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(
            brand_id=brand, name="Q2 push", start_date=date(2026, 5, 1)
        ),
        created_by_user_id=uuid4(),
    )
    assert campaign.organization_id == org
    assert campaign.brand_id == brand
    assert campaign.status == "draft"
    assert campaign.start_date == date(2026, 5, 1)


@pytest.mark.asyncio
async def test_create_campaign_brand_outside_org_rejected(store: _Store) -> None:
    org_a, org_b, brand = uuid4(), uuid4(), uuid4()
    store.brands[brand] = org_a  # brand belongs to A
    with pytest.raises(CampaignBrandMismatch):
        await create_campaign(
            organization_id=org_b,
            payload=CampaignCreate(brand_id=brand, name="x"),
            created_by_user_id=None,
        )


@pytest.mark.asyncio
async def test_get_campaign_other_org_returns_404(store: _Store) -> None:
    org_a, org_b, brand = uuid4(), uuid4(), uuid4()
    store.brands[brand] = org_a
    campaign = await create_campaign(
        organization_id=org_a,
        payload=CampaignCreate(brand_id=brand, name="x"),
        created_by_user_id=None,
    )
    with pytest.raises(CampaignNotFound):
        await get_campaign(campaign_id=campaign.id, organization_id=org_b)


@pytest.mark.asyncio
async def test_archive_campaign_cascades_to_channel_campaigns(
    store: _Store,
) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="voice_outbound",
            provider="vapi",
        ),
        created_by_user_id=None,
    )
    archived = await archive_campaign(campaign_id=campaign.id, organization_id=org)
    assert archived.status == "archived"
    refetched = await get_channel_campaign(
        channel_campaign_id=cc.id, organization_id=org
    )
    assert refetched.status == "archived"


@pytest.mark.asyncio
async def test_update_campaign_changes_name(store: _Store) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="orig"),
        created_by_user_id=None,
    )
    updated = await update_campaign(
        campaign_id=campaign.id,
        organization_id=org,
        payload=CampaignUpdate(name="new", status="active"),
    )
    assert updated.name == "new"
    assert updated.status == "active"


@pytest.mark.asyncio
async def test_list_campaigns_only_returns_org_rows(store: _Store) -> None:
    org_a, org_b, brand_a, brand_b = uuid4(), uuid4(), uuid4(), uuid4()
    store.brands[brand_a] = org_a
    store.brands[brand_b] = org_b
    await create_campaign(
        organization_id=org_a,
        payload=CampaignCreate(brand_id=brand_a, name="A"),
        created_by_user_id=None,
    )
    await create_campaign(
        organization_id=org_b,
        payload=CampaignCreate(brand_id=brand_b, name="B"),
        created_by_user_id=None,
    )
    rows = await list_campaigns(organization_id=org_a)
    assert len(rows) == 1
    assert rows[0].name == "A"


# ── Tests: channel_campaigns ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_channel_campaign_inherits_org_and_brand(
    store: _Store,
) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="email",
            provider="emailbison",
        ),
        created_by_user_id=None,
    )
    assert cc.organization_id == org
    assert cc.brand_id == brand
    assert cc.campaign_id == campaign.id
    assert cc.status == "draft"


@pytest.mark.asyncio
async def test_create_channel_campaign_rejects_unknown_channel_provider(
    store: _Store,
) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    with pytest.raises(ChannelCampaignChannelProviderInvalid):
        await create_channel_campaign(
            organization_id=org,
            payload=ChannelCampaignCreate(
                campaign_id=campaign.id,
                name="bad",
                channel="voice_outbound",
                provider="lob",  # invalid combo
            ),
            created_by_user_id=None,
        )


@pytest.mark.asyncio
async def test_direct_mail_channel_campaign_requires_design_id(
    store: _Store,
) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    with pytest.raises(ChannelCampaignDesignRequired):
        await create_channel_campaign(
            organization_id=org,
            payload=ChannelCampaignCreate(
                campaign_id=campaign.id,
                name="dm",
                channel="direct_mail",
                provider="lob",
            ),
            created_by_user_id=None,
        )


@pytest.mark.asyncio
async def test_direct_mail_design_brand_must_match(store: _Store) -> None:
    org, brand_a, brand_b = uuid4(), uuid4(), uuid4()
    store.brands[brand_a] = org
    store.brands[brand_b] = org
    design = uuid4()
    store.designs[design] = brand_b  # design belongs to brand B
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand_a, name="m"),
        created_by_user_id=None,
    )
    with pytest.raises(ChannelCampaignDesignBrandMismatch):
        await create_channel_campaign(
            organization_id=org,
            payload=ChannelCampaignCreate(
                campaign_id=campaign.id,
                name="dm",
                channel="direct_mail",
                provider="lob",
                design_id=design,
            ),
            created_by_user_id=None,
        )


@pytest.mark.asyncio
async def test_activate_channel_campaign_computes_scheduled_send_at(
    store: _Store,
) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(
            brand_id=brand, name="m", start_date=date(2026, 5, 1)
        ),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="email",
            provider="emailbison",
            start_offset_days=7,
        ),
        created_by_user_id=None,
    )
    activated = await activate_channel_campaign(
        channel_campaign_id=cc.id, organization_id=org
    )
    assert activated.status == "scheduled"
    assert activated.scheduled_send_at == datetime(2026, 5, 8, 0, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_pause_then_resume_channel_campaign(store: _Store) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="sms",
            provider="twilio",
        ),
        created_by_user_id=None,
    )
    activated = await activate_channel_campaign(
        channel_campaign_id=cc.id, organization_id=org
    )
    assert activated.status == "scheduled"
    paused = await pause_channel_campaign(
        channel_campaign_id=cc.id, organization_id=org
    )
    assert paused.status == "paused"
    resumed = await resume_channel_campaign(
        channel_campaign_id=cc.id, organization_id=org
    )
    assert resumed.status == "scheduled"


@pytest.mark.asyncio
async def test_archive_channel_campaign_blocks_status_change(store: _Store) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="sms",
            provider="twilio",
        ),
        created_by_user_id=None,
    )
    await archive_channel_campaign(channel_campaign_id=cc.id, organization_id=org)
    with pytest.raises(ChannelCampaignInvalidStatusTransition):
        await activate_channel_campaign(
            channel_campaign_id=cc.id, organization_id=org
        )


@pytest.mark.asyncio
async def test_list_channel_campaigns_filters(store: _Store) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    a = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="A"),
        created_by_user_id=None,
    )
    b = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="B"),
        created_by_user_id=None,
    )
    await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=a.id, name="a-email",
            channel="email", provider="emailbison",
        ),
        created_by_user_id=None,
    )
    await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=a.id, name="a-sms",
            channel="sms", provider="twilio",
        ),
        created_by_user_id=None,
    )
    await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=b.id, name="b-sms",
            channel="sms", provider="twilio",
        ),
        created_by_user_id=None,
    )
    by_campaign = await list_channel_campaigns(
        organization_id=org, campaign_id=a.id
    )
    assert {c.name for c in by_campaign} == {"a-email", "a-sms"}
    by_channel = await list_channel_campaigns(organization_id=org, channel="sms")
    assert {c.name for c in by_channel} == {"a-sms", "b-sms"}


@pytest.mark.asyncio
async def test_get_channel_campaign_other_org_404(store: _Store) -> None:
    org_a, org_b, brand = uuid4(), uuid4(), uuid4()
    store.brands[brand] = org_a
    campaign = await create_campaign(
        organization_id=org_a,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org_a,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id,
            name="c",
            channel="sms",
            provider="twilio",
        ),
        created_by_user_id=None,
    )
    with pytest.raises(ChannelCampaignNotFound):
        await get_channel_campaign(channel_campaign_id=cc.id, organization_id=org_b)


@pytest.mark.asyncio
async def test_update_channel_campaign_persists_metadata(store: _Store) -> None:
    org, brand = uuid4(), uuid4()
    store.brands[brand] = org
    campaign = await create_campaign(
        organization_id=org,
        payload=CampaignCreate(brand_id=brand, name="m"),
        created_by_user_id=None,
    )
    cc = await create_channel_campaign(
        organization_id=org,
        payload=ChannelCampaignCreate(
            campaign_id=campaign.id, name="c",
            channel="email", provider="emailbison",
        ),
        created_by_user_id=None,
    )
    updated = await update_channel_campaign(
        channel_campaign_id=cc.id,
        organization_id=org,
        payload=ChannelCampaignUpdate(metadata={"audience_label": "lapsed_insurance"}),
    )
    assert updated.metadata == {"audience_label": "lapsed_insurance"}
