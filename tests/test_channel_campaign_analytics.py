"""Tests for the per-channel_campaign drilldown endpoint and
``summarize_channel_campaign``.

Covers all three real channels (direct_mail with real steps,
voice_outbound with synthetic step + voice extension, sms with
synthetic step + sms extension) and the email channel that returns
zeros plus an empty ``channel_specific.email`` block. Cross-org
leakage is guarded by combining ``id = %s AND organization_id = %s``
in the channel_campaign SELECT.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.services import channel_campaign_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
CAMP_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
BRAND_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CC_DM = UUID("e1111111-1111-1111-1111-111111111111")
CC_VOICE = UUID("e2222222-2222-2222-2222-222222222222")
CC_SMS = UUID("e3333333-3333-3333-3333-333333333333")
CC_EMAIL = UUID("e4444444-4444-4444-4444-444444444444")
STEP_DM_1 = UUID("f1111111-1111-1111-1111-111111111111")
STEP_DM_2 = UUID("f2222222-2222-2222-2222-222222222222")


def _user(org_id: UUID | None) -> UserContext:
    return UserContext(
        auth_user_id=USER_A,
        business_user_id=USER_A,
        email="op@example.com",
        platform_role="platform_operator",
        active_organization_id=org_id,
        org_role=None,
        role="operator",
        client_id=None,
    )


@pytest.fixture
def auth_org_a():
    user = _user(ORG_A)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_org_b():
    user = _user(ORG_B)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user
    app.dependency_overrides[require_org_context] = lambda: user
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def auth_no_org():
    from fastapi import HTTPException, status

    user = _user(None)
    app.dependency_overrides[verify_supabase_jwt] = lambda: user

    def _deny() -> UserContext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "organization_context_required"},
        )

    app.dependency_overrides[require_org_context] = _deny
    yield
    app.dependency_overrides.clear()


# ── Postgres fake ───────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, queue: list[Any], capture: list[dict[str, Any]]) -> None:
        self._queue = queue
        self._capture = capture
        self._current: list[tuple] | tuple | None = None

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._capture.append({"sql": sql, "params": params})
        if not self._queue:
            self._current = []
            return
        self._current = self._queue.pop(0)

    async def fetchone(self) -> tuple | None:
        if isinstance(self._current, list):
            return self._current[0] if self._current else None
        return self._current

    async def fetchall(self) -> list[tuple]:
        if isinstance(self._current, list):
            return self._current
        return [self._current] if self._current else []


class _FakeConn:
    def __init__(self, queue: list[Any], capture: list[dict[str, Any]]) -> None:
        self._queue = queue
        self._capture = capture

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._queue, self._capture)


def _patch_pg(
    monkeypatch: pytest.MonkeyPatch, queue: list[Any]
) -> list[dict[str, Any]]:
    capture: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(queue, capture)

    monkeypatch.setattr(channel_campaign_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── Canned data ─────────────────────────────────────────────────────────


def _cc_row(channel: str, provider: str, cc_id: UUID = CC_DM) -> tuple:
    return (
        cc_id,
        CAMP_A,
        f"{channel} cc",
        channel,
        provider,
        "scheduled",
        datetime(2026, 4, 5, tzinfo=UTC),
        BRAND_A,
        ORG_A,
    )


def _dm_queue() -> list[Any]:
    """Direct-mail cc with two real steps and aggregates."""
    return [
        _cc_row("direct_mail", "lob", CC_DM),
        # steps
        [
            (STEP_DM_1, 1, "Postcard day 0", "cmp_aaa"),
            (STEP_DM_2, 2, "Letter day 14", "cmp_bbb"),
        ],
        # dm step aggregates (LEFT JOIN may emit zero-row groups)
        [
            (STEP_DM_1, 8, 2400, "succeeded"),
            (STEP_DM_2, 4, 1200, "succeeded"),
            (STEP_DM_1, 1, 0, "failed"),
        ],
        # piece funnel status rows
        [
            ("delivered", 7),
            ("in_transit", 3),
            ("returned", 1),
            ("failed", 0),
        ],
        # memberships
        [
            (STEP_DM_1, "pending", 5),
            (STEP_DM_1, "sent", 8),
            (STEP_DM_2, "sent", 4),
        ],
        # unique recipients
        (12,),
    ]


def _voice_queue() -> list[Any]:
    return [
        _cc_row("voice_outbound", "vapi", CC_VOICE),
        # outcomes
        [
            ("succeeded", 5, 25.50, 800),
            ("failed", 2, 1.20, 90),
            ("skipped", 3, 0.30, 30),
        ],
        # cost breakdown row
        (10.0, 5.0, 8.0, 3.0, 0.5),
    ]


def _sms_queue() -> list[Any]:
    return [
        _cc_row("sms", "twilio", CC_SMS),
        # outcomes (outcome, msgs)
        [
            ("succeeded", 18),  # status='delivered'
            ("failed", 2),
        ],
        # opt out count
        (1,),
    ]


def _email_queue() -> list[Any]:
    return [
        _cc_row("email", "emailbison", CC_EMAIL),
        # steps for the email cc (back-fill row exists)
        [(UUID("aaaa1111-0000-0000-0000-000000000001"), 1, "default", None)],
        # memberships
        [],
    ]


# ── Service tests ───────────────────────────────────────────────────────


async def test_unknown_cc_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(channel_campaign_analytics.ChannelCampaignNotFound):
        await channel_campaign_analytics.summarize_channel_campaign(
            organization_id=ORG_A,
            channel_campaign_id=CC_DM,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )


async def test_cc_lookup_uses_single_where_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-org guard: ``id = %s AND organization_id = %s`` in one WHERE."""
    capture = _patch_pg(monkeypatch, [None])
    with pytest.raises(channel_campaign_analytics.ChannelCampaignNotFound):
        await channel_campaign_analytics.summarize_channel_campaign(
            organization_id=ORG_A,
            channel_campaign_id=CC_DM,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )
    sql = capture[0]["sql"]
    assert "WHERE id = %s AND organization_id = %s" in sql
    assert capture[0]["params"] == (str(CC_DM), str(ORG_A))


async def test_dm_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, _dm_queue())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_DM,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["source"] == "postgres"
    assert result["channel_campaign"]["channel"] == "direct_mail"
    assert result["totals"]["events_total"] == 13  # 8 + 4 + 1
    assert result["totals"]["unique_recipients"] == 12
    assert result["totals"]["cost_total_cents"] == 3600

    funnel = result["channel_specific"]["direct_mail"]["piece_funnel"]
    assert funnel["delivered"] == 7
    assert funnel["in_transit"] == 3
    assert funnel["returned"] == 1

    # Two real steps, no synthetic.
    assert len(result["steps"]) == 2
    step1 = next(s for s in result["steps"] if s["step_order"] == 1)
    assert step1["events_total"] == 9  # 8 succeeded + 1 failed
    assert step1["memberships"]["pending"] == 5


def _dm_queue_with_conversions(
    *,
    click_row: tuple = (24, 7),
    denom_row: tuple = (10,),
) -> list[Any]:
    return _dm_queue() + [click_row, denom_row]


async def test_dm_summary_includes_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _dm_queue_with_conversions())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_DM,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    conv = result["conversions"]
    assert conv["clicks_total"] == 24
    assert conv["unique_clickers"] == 7
    assert conv["click_rate"] == 0.7  # 7 / 10
    assert conv["unique_clickers"] <= conv["clicks_total"]


async def test_dm_conversions_divide_by_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(
        monkeypatch,
        _dm_queue_with_conversions(click_row=(5, 3), denom_row=(0,)),
    )
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_DM,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["conversions"]["click_rate"] == 0.0
    assert result["conversions"]["clicks_total"] == 5


async def test_dm_conversions_query_org_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _patch_pg(monkeypatch, _dm_queue_with_conversions())
    await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_DM,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    # Click query is the 7th SQL call (idx 6): cc, steps, dm-aggs,
    # piece-funnel, memberships, unique_recipients, click, denom.
    click_sql = capture[6]["sql"]
    assert "de.event_type = 'link.clicked'" in click_sql
    assert "s.channel_campaign_id = %s" in click_sql
    assert "s.organization_id = %s" in click_sql


async def test_voice_summary_zero_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voice cc doesn't run dub queries; conversions block is zero."""
    _patch_pg(monkeypatch, _voice_queue())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_VOICE,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["conversions"] == {
        "clicks_total": 0,
        "unique_clickers": 0,
        "click_rate": 0.0,
    }


async def test_voice_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, _voice_queue())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_VOICE,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["channel_campaign"]["channel"] == "voice_outbound"
    assert result["totals"]["events_total"] == 10  # 5 + 2 + 3
    # cost_total = (25.50 + 1.20 + 0.30) * 100 = 2700 cents
    assert result["totals"]["cost_total_cents"] == 2700
    voice = result["channel_specific"]["voice_outbound"]
    # 5 succeeded / 10 total = 0.5
    assert voice["transfer_rate"] == 0.5
    assert voice["avg_duration_seconds"] == 92  # (800+90+30)//10
    assert voice["voice_step_attribution"] == "synthetic"
    assert voice["cost_breakdown"]["transport"] == 10.0
    # Synthetic step.
    assert len(result["steps"]) == 1
    assert result["steps"][0]["synthetic"] is True


async def test_sms_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, _sms_queue())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_SMS,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["channel_campaign"]["channel"] == "sms"
    assert result["totals"]["events_total"] == 20
    sms = result["channel_specific"]["sms"]
    assert sms["delivery_rate"] == 0.9  # 18/20
    assert sms["opt_out_count"] == 1
    assert sms["sms_step_attribution"] == "synthetic"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["synthetic"] is True


async def test_email_summary_returns_zeros(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _email_queue())
    result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_EMAIL,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["channel_campaign"]["channel"] == "email"
    assert result["totals"] == {
        "events_total": 0,
        "unique_recipients": 0,
        "cost_total_cents": 0,
    }
    assert "email" in result["channel_specific"]
    # Email backfill step exists but has no aggregates.
    assert len(result["steps"]) == 1


async def test_voice_extension_uses_brand_org_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The voice rollup query must filter on ``cl.brand_id = %s``;
    the brand has been pre-verified (via the cc lookup) to be in the
    auth's org, so this is the org-isolation chain.
    """
    capture = _patch_pg(monkeypatch, _voice_queue())
    await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_VOICE,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    voice_sql = capture[1]["sql"]
    assert "cl.channel_campaign_id = %s" in voice_sql
    assert "cl.brand_id = %s" in voice_sql


async def test_channel_specific_has_only_one_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per directive: ``channel_specific`` only contains the section for
    the channel_campaign's channel — never all four."""
    _patch_pg(monkeypatch, _dm_queue())
    dm_result = await channel_campaign_analytics.summarize_channel_campaign(
        organization_id=ORG_A,
        channel_campaign_id=CC_DM,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert list(dm_result["channel_specific"].keys()) == ["direct_mail"]


# ── HTTP tests ──────────────────────────────────────────────────────────


async def test_endpoint_returns_payload(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _dm_queue())
    resp = await _get(f"/api/v1/analytics/channel-campaigns/{CC_DM}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel_campaign"]["channel"] == "direct_mail"
    assert body["totals"]["events_total"] == 13


async def test_endpoint_404_when_cc_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/channel-campaigns/{CC_DM}/summary")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "channel_campaign_not_found"


async def test_endpoint_uses_org_from_auth(
    monkeypatch: pytest.MonkeyPatch, auth_org_b: None
) -> None:
    capture = _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/channel-campaigns/{CC_DM}/summary")
    assert resp.status_code == 404
    assert capture[0]["params"] == (str(CC_DM), str(ORG_B))


async def test_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    monkeypatch.setattr(
        channel_campaign_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get(f"/api/v1/analytics/channel-campaigns/{CC_DM}/summary")
    assert resp.status_code == 400


async def test_endpoint_rejects_invalid_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        channel_campaign_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/channel-campaigns/not-a-uuid/summary")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_channel_campaign_id"
