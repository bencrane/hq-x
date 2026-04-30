"""Tests for the campaign rollup endpoint and the underlying
``summarize_campaign`` service.

The service issues a sequence of SQL queries against Postgres; tests
patch ``get_db_connection`` with a fake that returns canned rows in
order. Auth is bypassed via ``app.dependency_overrides`` so we exercise
endpoint behavior, not the JWT.

Cross-org leakage is verified at two layers: the ``_load_campaign``
SELECT must include both ``id = %s`` and ``organization_id = %s`` in
the same WHERE clause; the endpoint test forces a 404 when the caller's
auth org differs from the campaign's org.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.auth.roles import require_org_context
from app.auth.supabase_jwt import UserContext, verify_supabase_jwt
from app.main import app
from app.services import campaign_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
CAMP_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
BRAND_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CC_DM = UUID("e1111111-1111-1111-1111-111111111111")
CC_VOICE = UUID("e2222222-2222-2222-2222-222222222222")
CC_SMS = UUID("e3333333-3333-3333-3333-333333333333")
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


# ── Postgres fake (queue-driven) ────────────────────────────────────────


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
    """Patch get_db_connection to feed the queue (one entry per execute call).

    Each entry is the rows list (or single tuple) the next ``execute`` call
    should return through fetchone / fetchall.
    """
    capture: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(queue, capture)

    monkeypatch.setattr(campaign_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── Canned data builders ────────────────────────────────────────────────


def _campaign_row() -> tuple:
    return (
        CAMP_A,
        ORG_A,
        BRAND_A,
        "Q2 outreach",
        "active",
        date(2026, 4, 1),
        datetime(2026, 4, 1, tzinfo=UTC),
    )


def _all_channel_campaigns_queue(
    *,
    dm_step_pieces: tuple[int, int] = (10, 5),
    dm_step_costs: tuple[int, int] = (1000, 500),
    voice_calls: int = 4,
    sms_msgs: int = 3,
    dm_unique_recipients: int = 12,
    pending: int = 7,
) -> list[Any]:
    """Build the full execute() queue for a campaign with one DM cc (two
    real steps), one voice cc, and one sms cc.

    Order matches summarize_campaign's call sequence:
      1. _load_campaign
      2. _load_channel_campaigns
      3. _load_steps
      4. _load_dm_step_aggregates
      5. _load_step_memberships
      6. _load_dm_unique_recipients_per_cc
      7. _load_voice_aggregates
      8. _load_sms_aggregates
    """
    return [
        _campaign_row(),  # 1
        [
            (
                CC_DM,
                "DM Lob",
                "direct_mail",
                "lob",
                "scheduled",
                datetime(2026, 4, 5, tzinfo=UTC),
            ),
            (
                CC_VOICE,
                "Voice Vapi",
                "voice_outbound",
                "vapi",
                "scheduled",
                None,
            ),
            (CC_SMS, "SMS Twilio", "sms", "twilio", "draft", None),
        ],  # 2
        [
            (STEP_DM_1, CC_DM, 1, "Postcard day 0", "cmp_aaa"),
            (STEP_DM_2, CC_DM, 2, "Letter day 14", "cmp_bbb"),
        ],  # 3
        [
            (STEP_DM_1, dm_step_pieces[0], dm_step_costs[0], "succeeded"),
            (STEP_DM_2, dm_step_pieces[1], dm_step_costs[1], "succeeded"),
        ],  # 4
        [
            (STEP_DM_1, "pending", pending),
            (STEP_DM_1, "sent", dm_step_pieces[0]),
            (STEP_DM_2, "sent", dm_step_pieces[1]),
        ],  # 5
        [(CC_DM, dm_unique_recipients)],  # 6
        [(CC_VOICE, voice_calls, 0, "succeeded")],  # 7
        [(CC_SMS, sms_msgs, "succeeded")],  # 8
    ]


# ─────────────────────────────────────────────────────────────────────────
# Service layer
# ─────────────────────────────────────────────────────────────────────────


async def test_summarize_unknown_campaign_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(campaign_analytics.CampaignNotFound):
        await campaign_analytics.summarize_campaign(
            organization_id=ORG_A,
            campaign_id=CAMP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )


async def test_summarize_filters_campaign_by_org(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first SELECT (``_load_campaign``) MUST combine id AND organization_id
    in the same WHERE clause — no two-step lookup that could leak via timing.
    """
    capture = _patch_pg(monkeypatch, [None])
    with pytest.raises(campaign_analytics.CampaignNotFound):
        await campaign_analytics.summarize_campaign(
            organization_id=ORG_A,
            campaign_id=CAMP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )
    sql = capture[0]["sql"]
    assert "WHERE id = %s AND organization_id = %s" in sql
    assert capture[0]["params"] == (str(CAMP_A), str(ORG_A))


async def test_summarize_full_rollup(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _patch_pg(monkeypatch, _all_channel_campaigns_queue())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 30, tzinfo=UTC)

    result = await campaign_analytics.summarize_campaign(
        organization_id=ORG_A,
        campaign_id=CAMP_A,
        start=start,
        end=end,
    )

    assert result["source"] == "postgres"
    assert result["window"] == {"from": start.isoformat(), "to": end.isoformat()}
    assert result["campaign"]["id"] == str(CAMP_A)
    assert result["campaign"]["organization_id"] == str(ORG_A)

    # 10 + 5 DM + 4 voice + 3 sms = 22 events_total
    assert result["totals"]["events_total"] == 22
    assert result["totals"]["unique_recipients_total"] == 12
    # 1000 + 500 cents
    assert result["totals"]["cost_total_cents"] == 1500

    by_id = {cc["channel_campaign_id"]: cc for cc in result["channel_campaigns"]}
    dm = by_id[str(CC_DM)]
    assert dm["channel"] == "direct_mail"
    assert dm["events_total"] == 15
    assert dm["unique_recipients"] == 12
    assert dm["cost_total_cents"] == 1500
    assert dm["outcomes"] == {"succeeded": 15, "failed": 0, "skipped": 0}
    assert len(dm["steps"]) == 2
    assert "voice_step_attribution" not in dm

    voice = by_id[str(CC_VOICE)]
    assert voice["channel"] == "voice_outbound"
    assert voice["events_total"] == 4
    assert voice["voice_step_attribution"] == "synthetic"
    assert len(voice["steps"]) == 1
    assert voice["steps"][0]["synthetic"] is True
    assert voice["steps"][0]["channel"] == "voice_outbound"
    assert voice["steps"][0]["channel_campaign_step_id"] is None

    sms = by_id[str(CC_SMS)]
    assert sms["channel"] == "sms"
    assert sms["events_total"] == 3
    assert sms["sms_step_attribution"] == "synthetic"
    assert sms["steps"][0]["synthetic"] is True

    # by_channel rollup
    by_channel = {row["channel"]: row for row in result["by_channel"]}
    assert by_channel["direct_mail"]["events_total"] == 15
    assert by_channel["voice_outbound"]["events_total"] == 4
    assert by_channel["sms"]["events_total"] == 3

    # by_provider rollup
    by_provider = {row["provider"]: row for row in result["by_provider"]}
    assert by_provider["lob"]["events_total"] == 15
    assert by_provider["vapi"]["events_total"] == 4
    assert by_provider["twilio"]["events_total"] == 3

    # All eight queries fired in order, every params tuple stamped with the
    # auth's org id. Spot-check a few.
    assert all(str(ORG_A) in (str(c["params"]) if c["params"] else "") for c in capture)


async def test_summarize_property_step_events_match_channel_campaign_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property test: sum(step.events_total) == channel_campaign.events_total
    for direct_mail (the only channel today with real per-step data).
    """
    _patch_pg(monkeypatch, _all_channel_campaigns_queue(dm_step_pieces=(10, 5)))
    result = await campaign_analytics.summarize_campaign(
        organization_id=ORG_A,
        campaign_id=CAMP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    for cc in result["channel_campaigns"]:
        if cc["channel"] != "direct_mail":
            continue
        step_sum = sum(s["events_total"] for s in cc["steps"])
        assert step_sum == cc["events_total"], (
            f"channel_campaign {cc['channel_campaign_id']}: "
            f"step sum {step_sum} != cc total {cc['events_total']}"
        )


async def test_summarize_step_memberships_attached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _all_channel_campaigns_queue(pending=9))
    result = await campaign_analytics.summarize_campaign(
        organization_id=ORG_A,
        campaign_id=CAMP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    dm = next(cc for cc in result["channel_campaigns"] if cc["channel"] == "direct_mail")
    step1 = next(s for s in dm["steps"] if s["channel_campaign_step_id"] == str(STEP_DM_1))
    assert step1["memberships"]["pending"] == 9
    assert step1["memberships"]["sent"] == 10
    assert step1["memberships"]["cancelled"] == 0


async def test_summarize_empty_campaign_zeros_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Campaign exists but has no channel_campaigns yet."""
    _patch_pg(
        monkeypatch,
        [
            _campaign_row(),
            [],  # no channel_campaigns
            [],  # no steps
            [],  # dm aggs
            [],  # memberships
            [],  # dm unique
            [],  # voice
            [],  # sms
        ],
    )
    result = await campaign_analytics.summarize_campaign(
        organization_id=ORG_A,
        campaign_id=CAMP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["channel_campaigns"] == []
    assert result["totals"] == {
        "events_total": 0,
        "unique_recipients_total": 0,
        "cost_total_cents": 0,
    }
    assert result["by_channel"] == []
    assert result["by_provider"] == []


async def test_summarize_window_passed_to_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _patch_pg(monkeypatch, _all_channel_campaigns_queue())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 15, tzinfo=UTC)
    await campaign_analytics.summarize_campaign(
        organization_id=ORG_A,
        campaign_id=CAMP_A,
        start=start,
        end=end,
    )
    # The dm_step_aggregates query (4th) and voice/sms queries take the
    # window in their params.
    dm_call = capture[3]
    assert start in dm_call["params"]
    assert end in dm_call["params"]


# ─────────────────────────────────────────────────────────────────────────
# HTTP layer
# ─────────────────────────────────────────────────────────────────────────


async def test_endpoint_returns_payload(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _all_channel_campaigns_queue())
    resp = await _get(f"/api/v1/analytics/campaigns/{CAMP_A}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["campaign"]["id"] == str(CAMP_A)
    assert body["totals"]["events_total"] == 22


async def test_endpoint_404_when_campaign_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    """User in org A asking about a campaign that does not belong to org A
    must get 404 — the SELECT returns no row because the WHERE clause
    combines id AND organization_id."""
    _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/campaigns/{CAMP_A}/summary")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "campaign_not_found"


async def test_endpoint_uses_org_from_auth(
    monkeypatch: pytest.MonkeyPatch, auth_org_b: None
) -> None:
    """User signed into org B — even with a campaign id that exists in
    org A, the SQL filter binds to org B and yields no row → 404.
    """
    capture = _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/campaigns/{CAMP_A}/summary")
    assert resp.status_code == 404
    assert capture[0]["params"] == (str(CAMP_A), str(ORG_B))


async def test_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    monkeypatch.setattr(
        campaign_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get(f"/api/v1/analytics/campaigns/{CAMP_A}/summary")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_endpoint_rejects_invalid_campaign_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        campaign_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/campaigns/not-a-uuid/summary")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_campaign_id"


async def test_endpoint_rejects_invalid_window(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        campaign_analytics,
        "get_db_connection",
        lambda: pytest.fail("invalid window should short-circuit"),
    )
    resp = await _get(
        f"/api/v1/analytics/campaigns/{CAMP_A}/summary"
        "?from=2026-04-30T00:00:00Z&to=2026-04-01T00:00:00Z"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_window"
