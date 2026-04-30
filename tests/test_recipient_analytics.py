"""Tests for the recipient timeline endpoint and ``recipient_timeline``.

Covers:

* Single-WHERE-clause org isolation (the lookup must combine
  ``id`` + ``organization_id`` so a caller in org A cannot probe
  recipient ids in org B via timing).
* Direct-mail event projection.
* Membership transition surfacing as synthetic ``membership.{status}``
  events with ``occurred_at`` from ``processed_at`` / ``created_at``.
* Time-ordering, pagination, summary rollup.
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
from app.services import recipient_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
RECIP_A = UUID("12121212-1212-1212-1212-121212121212")
PIECE_1 = UUID("0a0a0a0a-0000-0000-0000-000000000001")
PIECE_2 = UUID("0a0a0a0a-0000-0000-0000-000000000002")
CAMP_1 = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CC_1 = UUID("e1111111-1111-1111-1111-111111111111")
STEP_1 = UUID("f1111111-1111-1111-1111-111111111111")


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

    monkeypatch.setattr(recipient_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── Canned data ─────────────────────────────────────────────────────────


def _recipient_row() -> tuple:
    return (
        RECIP_A,
        ORG_A,
        "business",
        "fmcsa",
        "123456",
        "Test Carrier Inc.",
        datetime(2026, 3, 1, tzinfo=UTC),
    )


def _full_queue() -> list[Any]:
    """Recipient + 3 dm events + 2 membership events."""
    return [
        _recipient_row(),
        # dm events
        [
            (
                datetime(2026, 4, 12, tzinfo=UTC),
                "piece.delivered",
                PIECE_1,
                CAMP_1,
                CC_1,
                STEP_1,
            ),
            (
                datetime(2026, 4, 10, tzinfo=UTC),
                "piece.in_transit",
                PIECE_1,
                CAMP_1,
                CC_1,
                STEP_1,
            ),
            (
                datetime(2026, 4, 6, tzinfo=UTC),
                "piece.mailed",
                PIECE_1,
                CAMP_1,
                CC_1,
                STEP_1,
            ),
        ],
        # membership events
        [
            (
                datetime(2026, 4, 5, tzinfo=UTC),
                "scheduled",
                STEP_1,
                CC_1,
                CAMP_1,
                "direct_mail",
                "lob",
            ),
            (
                datetime(2026, 4, 12, 1, 0, tzinfo=UTC),
                "sent",
                STEP_1,
                CC_1,
                CAMP_1,
                "direct_mail",
                "lob",
            ),
        ],
    ]


# ── Service tests ───────────────────────────────────────────────────────


async def test_unknown_recipient_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(recipient_analytics.RecipientNotFound):
        await recipient_analytics.recipient_timeline(
            organization_id=ORG_A,
            recipient_id=RECIP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )


async def test_recipient_lookup_uses_single_where_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL guard: the recipient SELECT must combine
    ``id = %s AND organization_id = %s`` in the same WHERE clause —
    no two-step lookup that could leak via 404 vs 200 timing.
    """
    capture = _patch_pg(monkeypatch, [None])
    with pytest.raises(recipient_analytics.RecipientNotFound):
        await recipient_analytics.recipient_timeline(
            organization_id=ORG_A,
            recipient_id=RECIP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 30, tzinfo=UTC),
        )
    sql = capture[0]["sql"]
    assert "WHERE id = %s AND organization_id = %s" in sql
    assert capture[0]["params"] == (str(RECIP_A), str(ORG_A))


async def test_timeline_full_payload_time_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _full_queue())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["source"] == "postgres"
    assert result["recipient"]["id"] == str(RECIP_A)
    assert result["recipient"]["organization_id"] == str(ORG_A)

    # 3 dm + 2 membership = 5 events, descending by occurred_at.
    timestamps = [ev["occurred_at"] for ev in result["events"]]
    assert timestamps == sorted(timestamps, reverse=True)
    assert len(result["events"]) == 5
    assert result["pagination"] == {"limit": 100, "offset": 0, "total": 5}

    # Membership events surface as ``membership.{status}``.
    types = {ev["event_type"] for ev in result["events"]}
    assert "membership.scheduled" in types
    assert "membership.sent" in types
    assert "piece.delivered" in types

    summary = result["summary"]
    assert summary["total_events"] == 5
    assert summary["by_channel"]["direct_mail"] == 5
    assert summary["by_channel"]["voice_outbound"] == 0
    assert summary["by_channel"]["sms"] == 0
    assert summary["campaigns_touched"] == 1
    assert summary["channel_campaigns_touched"] == 1


async def test_timeline_pagination_slices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _full_queue())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
        limit=2,
        offset=1,
    )
    assert result["pagination"] == {"limit": 2, "offset": 1, "total": 5}
    assert len(result["events"]) == 2


async def test_timeline_clamps_limit_to_max(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(monkeypatch, _full_queue())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
        limit=10_000,
    )
    assert result["pagination"]["limit"] == 500


async def test_dm_events_query_carries_org_isolation_clause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct-mail events query must enforce org isolation either
    through ``s.organization_id = %s`` (step-tagged pieces) or through
    ``b.organization_id = %s`` (legacy ad-hoc operator sends).
    """
    capture = _patch_pg(monkeypatch, _full_queue())
    await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    dm_sql = capture[1]["sql"]
    assert "s.organization_id = %s" in dm_sql
    assert "b.organization_id = %s" in dm_sql
    # Also: recipient_id is bound from the function arg.
    assert str(RECIP_A) in capture[1]["params"]
    assert str(ORG_A) in capture[1]["params"]


async def test_membership_events_query_filters_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _patch_pg(monkeypatch, _full_queue())
    await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    membership_sql = capture[2]["sql"]
    assert "scr.recipient_id = %s" in membership_sql
    assert "scr.organization_id = %s" in membership_sql


async def test_timeline_empty_recipient(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(
        monkeypatch,
        [_recipient_row(), [], []],
    )
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    assert result["events"] == []
    assert result["summary"]["total_events"] == 0
    assert result["pagination"]["total"] == 0


# ── Dub events (Slice 4) ────────────────────────────────────────────────


def _full_queue_with_dub(
    *,
    dub_rows: list[tuple] | None = None,
) -> list[Any]:
    """Recipient + dm events + membership events + dub events."""
    return [
        *_full_queue(),
        dub_rows
        if dub_rows is not None
        else [
            (
                datetime(2026, 4, 12, 12, 30, tzinfo=UTC),  # AFTER delivered
                "link.clicked",
                "link_abc",
                "https://customer.example/lp",
                "US",
                "San Francisco",
                "Mobile",
                "Safari",
                "iOS",
                None,
                None,
                None,
                None,
                None,
                STEP_1,
                CC_1,
                CAMP_1,
            ),
            (
                datetime(2026, 4, 13, tzinfo=UTC),  # second click
                "link.clicked",
                "link_abc",
                "https://customer.example/lp",
                "US",
                None,
                "Desktop",
                "Chrome",
                "macOS",
                "https://t.co/x",
                None,
                None,
                None,
                None,
                STEP_1,
                CC_1,
                CAMP_1,
            ),
        ],
    ]


async def test_timeline_includes_dub_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, _full_queue_with_dub())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    # 3 dm + 2 membership + 2 dub = 7 events.
    assert result["pagination"]["total"] == 7
    assert result["summary"]["total_events"] == 7
    # All events are direct_mail-channel (provider differs).
    assert result["summary"]["by_channel"]["direct_mail"] == 7

    # Dub click events show up with provider=dub and event_type=dub.click.
    dub_events = [
        ev for ev in result["events"] if ev["event_type"] == "dub.click"
    ]
    assert len(dub_events) == 2
    for ev in dub_events:
        assert ev["channel"] == "direct_mail"
        assert ev["provider"] == "dub"
        assert ev["artifact_kind"] == "dub_link"
        assert ev["artifact_id"] == "link_abc"
        assert ev["channel_campaign_step_id"] == str(STEP_1)
        assert ev["campaign_id"] == str(CAMP_1)
        assert ev["metadata"]["click_url"] == "https://customer.example/lp"
        # Nones filtered out of metadata.
        assert "customer_id" not in ev["metadata"]
        assert "sale_amount_cents" not in ev["metadata"]


async def test_timeline_dub_events_interleave_chronologically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mix of piece events and clicks must sort by occurred_at DESC."""
    _patch_pg(monkeypatch, _full_queue_with_dub())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    timestamps = [ev["occurred_at"] for ev in result["events"]]
    assert timestamps == sorted(timestamps, reverse=True)
    # The 2026-04-13 click is the most-recent event.
    assert result["events"][0]["event_type"] == "dub.click"
    assert result["events"][0]["occurred_at"].startswith("2026-04-13")


async def test_timeline_dub_lead_event(monkeypatch: pytest.MonkeyPatch) -> None:
    lead_row = (
        datetime(2026, 4, 14, tzinfo=UTC),
        "lead.created",
        "link_abc",
        "https://customer.example/lp",
        None, None, None, None, None, None,
        "cus_42",
        "lead@example.com",
        None,
        None,
        STEP_1,
        CC_1,
        CAMP_1,
    )
    _patch_pg(monkeypatch, _full_queue_with_dub(dub_rows=[lead_row]))
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    lead = next(ev for ev in result["events"] if ev["event_type"] == "dub.lead")
    assert lead["metadata"]["customer_id"] == "cus_42"
    assert lead["metadata"]["customer_email"] == "lead@example.com"


async def test_timeline_dub_query_filters_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cross-org guard for dub events: the SQL must filter on
    s.organization_id (the link's step-attached org), AND on
    dl.recipient_id."""
    capture = _patch_pg(monkeypatch, _full_queue_with_dub())
    await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    # dub events query is the 4th SQL call (idx 3).
    dub_sql = capture[3]["sql"]
    assert "FROM dmaas_dub_events de" in dub_sql
    assert "JOIN dmaas_dub_links dl" in dub_sql
    assert "JOIN business.channel_campaign_steps s" in dub_sql
    assert "dl.recipient_id = %s" in dub_sql
    assert "s.organization_id = %s" in dub_sql
    # Both bound to params.
    assert str(RECIP_A) in capture[3]["params"]
    assert str(ORG_A) in capture[3]["params"]


async def test_timeline_pagination_across_merged_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pagination slices the combined dm + membership + dub stream."""
    _patch_pg(monkeypatch, _full_queue_with_dub())
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
        limit=3,
        offset=2,
    )
    assert result["pagination"] == {"limit": 3, "offset": 2, "total": 7}
    assert len(result["events"]) == 3


async def test_timeline_no_dub_events_when_recipient_has_no_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If dmaas_dub_links has no row for this recipient (or nothing
    bound to a step), the dub events query returns no rows."""
    _patch_pg(monkeypatch, [*_full_queue(), []])
    result = await recipient_analytics.recipient_timeline(
        organization_id=ORG_A,
        recipient_id=RECIP_A,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 30, tzinfo=UTC),
    )
    # Pre-Slice-4 baseline: 3 dm + 2 membership = 5 events.
    assert result["pagination"]["total"] == 5
    assert all(ev["provider"] != "dub" for ev in result["events"])


# ── HTTP tests ──────────────────────────────────────────────────────────


async def test_endpoint_returns_payload(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _full_queue())
    resp = await _get(f"/api/v1/analytics/recipients/{RECIP_A}/timeline")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["recipient"]["id"] == str(RECIP_A)
    assert body["pagination"]["total"] == 5


async def test_endpoint_404_when_recipient_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/recipients/{RECIP_A}/timeline")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "recipient_not_found"


async def test_endpoint_uses_org_from_auth(
    monkeypatch: pytest.MonkeyPatch, auth_org_b: None
) -> None:
    capture = _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/recipients/{RECIP_A}/timeline")
    assert resp.status_code == 404
    # The recipient lookup binds the org from auth (org B), not anything
    # in the URL.
    assert capture[0]["params"] == (str(RECIP_A), str(ORG_B))


async def test_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    monkeypatch.setattr(
        recipient_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get(f"/api/v1/analytics/recipients/{RECIP_A}/timeline")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_endpoint_rejects_invalid_recipient_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        recipient_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/recipients/not-a-uuid/timeline")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_recipient_id"


async def test_endpoint_pagination_query_params(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _full_queue())
    resp = await _get(
        f"/api/v1/analytics/recipients/{RECIP_A}/timeline?limit=2&offset=1"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["limit"] == 2
    assert body["pagination"]["offset"] == 1


async def test_endpoint_rejects_limit_above_max(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    """FastAPI ``Query(le=500)`` enforces the ceiling."""
    monkeypatch.setattr(
        recipient_analytics,
        "get_db_connection",
        lambda: pytest.fail("oversized limit should be rejected pre-service"),
    )
    resp = await _get(
        f"/api/v1/analytics/recipients/{RECIP_A}/timeline?limit=10000"
    )
    assert resp.status_code in (400, 422)
