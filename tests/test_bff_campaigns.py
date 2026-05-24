"""Tests for the hq-zone BFF campaign-enrollment surface.

Two layers:
  * Pure-logic: model validation + dedupe + channel/provider whitelist.
  * Service: in-memory DB fake exercising the atomic enroll-list path.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.models.bff_campaigns import (
    BffEnrollListRecipient,
    BffEnrollListRequest,
)
from app.services import bff_campaigns as bff_campaigns_service
from app.services.bff_campaigns import (
    BffEnrollBrandMismatch,
    BffEnrollInvalidChannelProvider,
    _dedupe_recipients,
    enroll_list_into_new_campaign,
)


# ── Pure model validation ─────────────────────────────────────────────────


def test_request_requires_at_least_one_recipient() -> None:
    with pytest.raises(ValidationError):
        BffEnrollListRequest(
            organization_id=uuid4(),
            brand_id=uuid4(),
            campaign_name="x",
            recipients=[],
        )


def test_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        BffEnrollListRequest(
            organization_id=uuid4(),
            brand_id=uuid4(),
            campaign_name="x",
            recipients=[
                BffEnrollListRecipient(external_source="hq_zone", external_id="1")
            ],
            bogus="oops",  # type: ignore[call-arg]
        )


def test_recipient_rejects_blank_natural_key() -> None:
    with pytest.raises(ValidationError):
        BffEnrollListRecipient(external_source="", external_id="x")
    with pytest.raises(ValidationError):
        BffEnrollListRecipient(external_source="hq_zone", external_id="")


def test_dedupe_last_write_wins() -> None:
    a = BffEnrollListRecipient(
        external_source="hq_zone", external_id="1", display_name="First"
    )
    b = BffEnrollListRecipient(
        external_source="hq_zone", external_id="1", display_name="Second"
    )
    c = BffEnrollListRecipient(external_source="hq_zone", external_id="2")
    out = _dedupe_recipients([a, b, c])
    assert len(out) == 2
    # Second wins because dict[key]=value overwrites
    by_id = {r.external_id: r.display_name for r in out}
    assert by_id == {"1": "Second", "2": None}


# ── Service integration via in-memory fake ────────────────────────────────


@dataclass
class _Store:
    """Just enough state to round-trip the orchestrator."""

    brands: dict[UUID, UUID] = field(default_factory=dict)  # brand_id → org_id
    campaigns: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    channel_campaigns: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    steps: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    recipients: dict[tuple[UUID, str, str], dict[str, Any]] = field(
        default_factory=dict
    )
    memberships: list[dict[str, Any]] = field(default_factory=list)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class _FakeCursor:
    def __init__(self, store: _Store) -> None:
        self._s = store
        self._row: tuple | None = None

    async def __aenter__(self) -> "_FakeCursor":
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def execute(self, sql: str, params: tuple | list) -> None:
        s = _norm(sql)

        if s.startswith("SELECT 1 FROM business.brands"):
            brand_id, org_id = UUID(params[0]), UUID(params[1])
            self._row = (
                (1,) if self._s.brands.get(brand_id) == org_id else None
            )
            return

        if s.startswith("INSERT INTO business.campaigns"):
            org, brand, name, meta = params
            cid = uuid4()
            self._s.campaigns[cid] = {
                "id": cid,
                "organization_id": UUID(org),
                "brand_id": UUID(brand),
                "name": name,
                "metadata": getattr(meta, "obj", {}) or {},
                "created_at": _now(),
            }
            self._row = (cid,)
            return

        if s.startswith("INSERT INTO business.channel_campaigns"):
            (
                campaign_id, org, brand, name, channel, provider,
                aud_count, meta,
            ) = params
            ccid = uuid4()
            self._s.channel_campaigns[ccid] = {
                "id": ccid,
                "campaign_id": UUID(campaign_id),
                "organization_id": UUID(org),
                "brand_id": UUID(brand),
                "name": name,
                "channel": channel,
                "provider": provider,
                "audience_snapshot_count": aud_count,
                "metadata": getattr(meta, "obj", {}) or {},
            }
            self._row = (ccid,)
            return

        if s.startswith("INSERT INTO business.channel_campaign_steps"):
            (
                cc_id, campaign_id, org, brand, step_order, name,
                delay, content_mode, cfg, meta,
            ) = params
            sid = uuid4()
            self._s.steps[sid] = {
                "id": sid,
                "channel_campaign_id": UUID(cc_id),
                "campaign_id": UUID(campaign_id),
                "organization_id": UUID(org),
                "brand_id": UUID(brand),
                "step_order": step_order,
                "name": name,
                "delay_days_from_previous": delay,
                "content_mode": content_mode,
                "channel_specific_config": getattr(cfg, "obj", {}) or {},
                "metadata": getattr(meta, "obj", {}) or {},
            }
            self._row = (sid,)
            return

        if s.startswith("INSERT INTO business.recipients"):
            (
                org, rtype, src, ext_id, display, addr,
                phone, email, meta,
            ) = params
            key = (UUID(org), src, ext_id)
            existing = self._s.recipients.get(key)
            was_insert = existing is None
            if was_insert:
                rid = uuid4()
                self._s.recipients[key] = {
                    "id": rid,
                    "organization_id": UUID(org),
                    "recipient_type": rtype,
                    "external_source": src,
                    "external_id": ext_id,
                    "display_name": display,
                    "mailing_address": getattr(addr, "obj", {}) or {},
                    "phone": phone,
                    "email": email,
                    "metadata": getattr(meta, "obj", {}) or {},
                }
            else:
                rid = existing["id"]
                # Mirror COALESCE-preferring-EXCLUDED-when-non-null
                if display is not None:
                    existing["display_name"] = display
                if phone is not None:
                    existing["phone"] = phone
                if email is not None:
                    existing["email"] = email
                addr_obj = getattr(addr, "obj", {}) or {}
                if addr_obj:
                    existing["mailing_address"] = addr_obj
                existing["metadata"] = {
                    **existing["metadata"],
                    **(getattr(meta, "obj", {}) or {}),
                }
            self._row = (rid, was_insert)
            return

        raise AssertionError(f"unexpected SQL: {s[:120]}")

    async def executemany(self, sql: str, rows: list[tuple]) -> None:
        s = _norm(sql)
        if s.startswith(
            "INSERT INTO business.channel_campaign_step_recipients"
        ):
            for r in rows:
                step_id, recipient_id, org, status, meta = r
                self._s.memberships.append({
                    "channel_campaign_step_id": UUID(step_id),
                    "recipient_id": UUID(recipient_id),
                    "organization_id": UUID(org),
                    "status": status,
                    "metadata": getattr(meta, "obj", {}) or {},
                })
            return
        raise AssertionError(f"unexpected executemany SQL: {s[:120]}")

    async def fetchone(self) -> tuple | None:
        row, self._row = self._row, None
        return row


class _FakeConn:
    def __init__(self, store: _Store) -> None:
        self._s = store

    def cursor(self) -> "_FakeCursor":
        return _FakeCursor(self._s)


@asynccontextmanager
async def _fake_get_db_connection(store: _Store):
    yield _FakeConn(store)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    s = _Store()

    def patched():
        return _fake_get_db_connection(s)

    monkeypatch.setattr(bff_campaigns_service, "get_db_connection", patched)
    return s


def _request(
    org_id: UUID, brand_id: UUID, recipients: list[BffEnrollListRecipient]
) -> BffEnrollListRequest:
    return BffEnrollListRequest(
        organization_id=org_id,
        brand_id=brand_id,
        campaign_name="Test enrollment",
        channel="email",
        provider="emailbison",
        recipients=recipients,
    )


@pytest.mark.asyncio
async def test_happy_path_creates_full_hierarchy(store: _Store) -> None:
    org_id, brand_id = uuid4(), uuid4()
    store.brands[brand_id] = org_id

    recipients = [
        BffEnrollListRecipient(
            external_source="hq_zone",
            external_id="lead-1",
            display_name="Acme Co",
            email="acme@example.com",
        ),
        BffEnrollListRecipient(
            external_source="hq_zone",
            external_id="lead-2",
            display_name="Beta Co",
        ),
    ]

    result = await enroll_list_into_new_campaign(
        _request(org_id, brand_id, recipients)
    )

    assert result.recipient_count == 2
    assert result.recipients_new == 2
    assert result.recipients_existing == 0
    assert result.memberships_created == 2
    assert len(store.campaigns) == 1
    assert len(store.channel_campaigns) == 1
    assert len(store.steps) == 1
    assert len(store.recipients) == 2
    assert len(store.memberships) == 2

    step = next(iter(store.steps.values()))
    assert step["step_order"] == 1
    assert step["content_mode"] == "manual"

    cc = next(iter(store.channel_campaigns.values()))
    assert cc["channel"] == "email"
    assert cc["provider"] == "emailbison"
    assert cc["audience_snapshot_count"] == 2

    for m in store.memberships:
        assert m["status"] == "pending"
        assert m["organization_id"] == org_id


@pytest.mark.asyncio
async def test_brand_not_in_org_raises(store: _Store) -> None:
    org_id, brand_id, other_org = uuid4(), uuid4(), uuid4()
    store.brands[brand_id] = other_org  # brand is under a DIFFERENT org

    with pytest.raises(BffEnrollBrandMismatch):
        await enroll_list_into_new_campaign(
            _request(
                org_id,
                brand_id,
                [BffEnrollListRecipient(external_source="hq_zone", external_id="1")],
            )
        )

    # Nothing was written
    assert store.campaigns == {}
    assert store.channel_campaigns == {}
    assert store.steps == {}
    assert store.recipients == {}
    assert store.memberships == []


@pytest.mark.asyncio
async def test_invalid_channel_provider_raises(store: _Store) -> None:
    org_id, brand_id = uuid4(), uuid4()
    store.brands[brand_id] = org_id

    payload = BffEnrollListRequest(
        organization_id=org_id,
        brand_id=brand_id,
        campaign_name="x",
        channel="sms",
        provider="emailbison",  # mismatched pair
        recipients=[
            BffEnrollListRecipient(external_source="hq_zone", external_id="1")
        ],
    )

    with pytest.raises(BffEnrollInvalidChannelProvider):
        await enroll_list_into_new_campaign(payload)


@pytest.mark.asyncio
async def test_duplicate_recipients_are_collapsed(store: _Store) -> None:
    org_id, brand_id = uuid4(), uuid4()
    store.brands[brand_id] = org_id

    recipients = [
        BffEnrollListRecipient(
            external_source="hq_zone", external_id="lead-1", display_name="First"
        ),
        BffEnrollListRecipient(
            external_source="hq_zone", external_id="lead-1", display_name="Second"
        ),
        BffEnrollListRecipient(external_source="hq_zone", external_id="lead-2"),
    ]

    result = await enroll_list_into_new_campaign(
        _request(org_id, brand_id, recipients)
    )

    assert result.recipient_count == 2
    assert result.memberships_created == 2
    assert len(store.memberships) == 2

    by_ext_id = {
        spec["external_id"]: spec for spec in store.recipients.values()
    }
    assert by_ext_id["lead-1"]["display_name"] == "Second"


@pytest.mark.asyncio
async def test_repeat_enroll_marks_existing(store: _Store) -> None:
    """Second enroll of the same lead list reports it as existing, not new."""
    org_id, brand_id = uuid4(), uuid4()
    store.brands[brand_id] = org_id

    leads = [
        BffEnrollListRecipient(external_source="hq_zone", external_id="lead-1"),
        BffEnrollListRecipient(external_source="hq_zone", external_id="lead-2"),
    ]
    first = await enroll_list_into_new_campaign(_request(org_id, brand_id, leads))
    assert first.recipients_new == 2
    assert first.recipients_existing == 0

    second = await enroll_list_into_new_campaign(_request(org_id, brand_id, leads))
    assert second.recipients_new == 0
    assert second.recipients_existing == 2
    # Two separate campaigns, separate steps, separate memberships
    assert len(store.campaigns) == 2
    assert len(store.steps) == 2
    assert len(store.memberships) == 4
    # But still only two recipient rows (org-natural-key dedupe)
    assert len(store.recipients) == 2


@pytest.mark.asyncio
async def test_default_step_name(store: _Store) -> None:
    org_id, brand_id = uuid4(), uuid4()
    store.brands[brand_id] = org_id

    payload = BffEnrollListRequest(
        organization_id=org_id,
        brand_id=brand_id,
        campaign_name="x",
        channel="email",
        provider="emailbison",
        recipients=[
            BffEnrollListRecipient(external_source="hq_zone", external_id="1")
        ],
    )
    await enroll_list_into_new_campaign(payload)
    step = next(iter(store.steps.values()))
    assert step["name"] == "Step 1 — email"
