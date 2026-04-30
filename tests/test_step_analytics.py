"""Tests for the per-step funnel endpoint and ``summarize_step``.

Verifies the membership funnel, event-type breakdown, outcome mapping,
piece funnel, and the cross-org leakage guarantee — the step SELECT
must combine ``s.id = %s AND s.organization_id = %s`` so a caller
in org A cannot probe step ids in org B.
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
from app.services import step_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
STEP_A = UUID("f1111111-1111-1111-1111-111111111111")
CC_A = UUID("e1111111-1111-1111-1111-111111111111")
CAMP_A = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")


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
    capture: list[dict[str, Any]] = []

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(queue, capture)

    monkeypatch.setattr(step_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── Canned data ─────────────────────────────────────────────────────────


def _step_row_dm() -> tuple:
    return (
        STEP_A,
        CC_A,
        CAMP_A,
        1,
        "Postcard day 0",
        "direct_mail",
        "lob",
        "cmp_xyz",
        "scheduled",
        datetime(2026, 4, 5, tzinfo=UTC),
        datetime(2026, 4, 5, tzinfo=UTC),
    )


def _step_row_voice() -> tuple:
    return (
        STEP_A,
        CC_A,
        CAMP_A,
        1,
        "voice step",
        "voice_outbound",
        "vapi",
        None,
        "pending",
        None,
        None,
    )


def _dm_queue(
    *,
    membership_rows: list[tuple] | None = None,
    outcome_rows: list[tuple] | None = None,
    status_rows: list[tuple] | None = None,
    event_rows: list[tuple] | None = None,
) -> list[Any]:
    return [
        _step_row_dm(),
        membership_rows
        if membership_rows is not None
        else [
            ("pending", 5),
            ("scheduled", 3),
            ("sent", 12),
            ("failed", 1),
        ],
        outcome_rows
        if outcome_rows is not None
        else [
            ("succeeded", 12, 3600),
            ("failed", 1, 0),
        ],
        status_rows
        if status_rows is not None
        else [
            ("delivered", 8),
            ("in_transit", 2),
            ("processed_for_delivery", 2),
            ("returned", 1),
            ("queued", 0),
        ],
        event_rows
        if event_rows is not None
        else [
            ("piece.mailed", 13),
            ("piece.delivered", 8),
            ("piece.returned", 1),
        ],
    ]


# ── Service tests ───────────────────────────────────────────────────────


async def test_summarize_unknown_step_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(step_analytics.StepNotFound):
        await step_analytics.summarize_step(
            organization_id=ORG_A,
            step_id=STEP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )


async def test_summarize_step_filters_by_org_in_same_where(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Org isolation guard: the step SELECT must combine
    ``s.id = %s AND s.organization_id = %s`` in one WHERE clause.
    """
    capture = _patch_pg(monkeypatch, [None])
    with pytest.raises(step_analytics.StepNotFound):
        await step_analytics.summarize_step(
            organization_id=ORG_A,
            step_id=STEP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )
    sql = capture[0]["sql"]
    assert "WHERE s.id = %s AND s.organization_id = %s" in sql
    assert capture[0]["params"] == (str(STEP_A), str(ORG_A))


async def test_summarize_dm_step_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _dm_queue())
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["source"] == "postgres"
    assert result["step"]["channel"] == "direct_mail"
    assert result["memberships"] == {
        "pending": 5,
        "scheduled": 3,
        "sent": 12,
        "failed": 1,
        "suppressed": 0,
        "cancelled": 0,
    }
    assert result["events"]["total"] == 13  # 12 succeeded + 1 failed
    assert result["events"]["outcomes"] == {
        "succeeded": 12,
        "failed": 1,
        "skipped": 0,
    }
    assert result["events"]["cost_total_cents"] == 3600
    assert result["events"]["by_event_type"]["piece.delivered"] == 8

    funnel = result["channel_specific"]["direct_mail"]["piece_funnel"]
    assert funnel["delivered"] == 8
    assert funnel["in_transit"] == 4  # in_transit + processed_for_delivery
    assert funnel["returned"] == 1
    assert funnel["queued"] == 0  # zero count rows are dropped


async def test_summarize_voice_step_returns_zero_dm_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real voice steps (back-filled by 0023) have no per-step direct_mail
    aggregates; the endpoint returns zeros for events + an empty
    channel_specific block.
    """
    _patch_pg(
        monkeypatch,
        [
            _step_row_voice(),
            [("pending", 4)],  # memberships
        ],
    )
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["step"]["channel"] == "voice_outbound"
    assert result["events"]["total"] == 0
    assert result["events"]["by_event_type"] == {}
    assert result["channel_specific"] == {}
    assert result["memberships"]["pending"] == 4


async def test_summarize_membership_query_filters_by_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The membership SELECT must include ``organization_id = %s`` so a
    step lookup that somehow leaked across orgs (defence in depth)
    couldn't pull memberships from the wrong org.
    """
    capture = _patch_pg(monkeypatch, _dm_queue())
    await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    membership_sql = capture[1]["sql"]
    assert "channel_campaign_step_id = %s" in membership_sql
    assert "organization_id = %s" in membership_sql
    assert capture[1]["params"] == (str(STEP_A), str(ORG_A))


async def test_summarize_window_passed_to_dm_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _patch_pg(monkeypatch, _dm_queue())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 30, tzinfo=UTC)
    await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=start,
        end=end,
    )
    # outcome aggregates are query #3 (idx 2).
    outcome_call = capture[2]
    assert start in outcome_call["params"]
    assert end in outcome_call["params"]


# ── Conversions (Slice 3) ───────────────────────────────────────────────


def _dm_queue_with_conversions(
    *,
    click_row: tuple = (12, 4),  # (clicks_total, unique_clickers)
    denom_row: tuple = (10,),    # (unique_recipients_in_funnel,)
) -> list[Any]:
    return _dm_queue() + [click_row, denom_row]


async def test_summarize_dm_step_includes_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _dm_queue_with_conversions())
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    conv = result["conversions"]
    assert conv["clicks_total"] == 12
    assert conv["unique_clickers"] == 4
    assert conv["click_rate"] == 0.4  # 4/10
    # Property: unique_clickers <= clicks_total.
    assert conv["unique_clickers"] <= conv["clicks_total"]


async def test_summarize_step_conversions_zero_when_no_clicks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(
        monkeypatch,
        _dm_queue_with_conversions(click_row=(0, 0), denom_row=(5,)),
    )
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    conv = result["conversions"]
    assert conv["clicks_total"] == 0
    assert conv["unique_clickers"] == 0
    assert conv["click_rate"] == 0.0


async def test_summarize_step_conversions_divide_by_zero_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No recipients in delivered/in-transit family → click_rate is 0.0,
    not a divide-by-zero error."""
    _patch_pg(
        monkeypatch,
        _dm_queue_with_conversions(click_row=(3, 2), denom_row=(0,)),
    )
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["conversions"]["click_rate"] == 0.0
    # Recorded clicks still surface (we don't suppress them).
    assert result["conversions"]["clicks_total"] == 3
    assert result["conversions"]["unique_clickers"] == 2


async def test_summarize_step_conversions_query_filters_by_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clicks SQL must filter on s.id + s.organization_id together."""
    capture = _patch_pg(monkeypatch, _dm_queue_with_conversions())
    await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    # Click query is the 5th call (idx 4): step, memberships, outcomes,
    # status, event-breakdown, click, denom.
    click_sql = capture[5]["sql"]
    assert "de.event_type = 'link.clicked'" in click_sql
    assert "s.id = %s" in click_sql
    assert "s.organization_id = %s" in click_sql
    # Org id must be in the click query params.
    assert str(ORG_A) in capture[5]["params"]


async def test_summarize_voice_step_zero_conversions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Voice steps don't run dub queries — conversions block is zeros."""
    _patch_pg(
        monkeypatch,
        [
            _step_row_voice(),
            [("pending", 4)],
        ],
    )
    result = await step_analytics.summarize_step(
        organization_id=ORG_A,
        step_id=STEP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["conversions"] == {
        "clicks_total": 0,
        "unique_clickers": 0,
        "click_rate": 0.0,
        "leads_total": 0,
        "unique_leads": 0,
        "lead_rate": 0.0,
    }


# ── HTTP tests ──────────────────────────────────────────────────────────


async def test_endpoint_returns_payload(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _dm_queue())
    resp = await _get(f"/api/v1/analytics/channel-campaign-steps/{STEP_A}/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["step"]["channel"] == "direct_mail"
    assert body["events"]["total"] == 13


async def test_endpoint_404_when_step_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/channel-campaign-steps/{STEP_A}/summary")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "step_not_found"


async def test_endpoint_uses_org_from_auth(
    monkeypatch: pytest.MonkeyPatch, auth_org_b: None
) -> None:
    capture = _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/channel-campaign-steps/{STEP_A}/summary")
    assert resp.status_code == 404
    assert capture[0]["params"] == (str(STEP_A), str(ORG_B))


async def test_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    monkeypatch.setattr(
        step_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get(f"/api/v1/analytics/channel-campaign-steps/{STEP_A}/summary")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_endpoint_rejects_invalid_step_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        step_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/channel-campaign-steps/not-a-uuid/summary")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_step_id"


async def test_endpoint_rejects_invalid_window(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        step_analytics,
        "get_db_connection",
        lambda: pytest.fail("invalid window should short-circuit"),
    )
    resp = await _get(
        f"/api/v1/analytics/channel-campaign-steps/{STEP_A}/summary"
        "?from=2026-04-30T00:00:00Z&to=2026-04-01T00:00:00Z"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_window"
