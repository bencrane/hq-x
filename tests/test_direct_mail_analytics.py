"""Tests for the direct-mail funnel endpoint and ``summarize_direct_mail``.

Covers:

* Funnel mapping (status → queued/processed/in_transit/delivered/returned/failed).
* Daily-trends prefill (every day in the window appears, even if zero).
* Failure-reason breakdown sourced from
  ``direct_mail_piece_events.raw_payload->>'reason'``.
* All three optional drilldown filters (brand_id / channel_campaign_id /
  channel_campaign_step_id), including their cross-org guards.
* Org isolation: the piece queries always join through
  ``business.brands.organization_id``; an org-A user passing an org-B
  brand id gets 404.
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
from app.services import direct_mail_analytics

ORG_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ORG_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
USER_A = UUID("11111111-1111-1111-1111-111111111111")
BRAND_A = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
CC_A = UUID("e1111111-1111-1111-1111-111111111111")
STEP_A = UUID("f1111111-1111-1111-1111-111111111111")


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

    monkeypatch.setattr(direct_mail_analytics, "get_db_connection", _conn)
    return capture


async def _get(path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


# ── Canned data ─────────────────────────────────────────────────────────


def _no_filter_queue(
    *,
    total: int = 20,
    status_rows: list[tuple] | None = None,
    type_rows: list[tuple] | None = None,
    daily_created: list[tuple] | None = None,
    daily_events: list[tuple] | None = None,
    failure_rows: list[tuple] | None = None,
) -> list[Any]:
    """Six-query queue when no optional filters are supplied (no
    pre-validation queries fire)."""
    return [
        # 1. count check
        (total,),
        # 2. by status
        status_rows
        if status_rows is not None
        else [
            ("delivered", 12, 1),
            ("in_transit", 4, 0),
            ("returned", 1, 0),
            ("failed", 2, 0),
            ("queued", 1, 0),
        ],
        # 3. by type
        type_rows
        if type_rows is not None
        else [
            ("postcard", 14, 10, 1),
            ("letter", 6, 2, 2),
        ],
        # 4. daily created
        daily_created
        if daily_created is not None
        else [
            (date(2026, 4, 1), 5),
            (date(2026, 4, 2), 8),
        ],
        # 5. daily events
        daily_events
        if daily_events is not None
        else [
            (date(2026, 4, 5), "piece.delivered", 9),
            (date(2026, 4, 5), "piece.failed", 1),
            (date(2026, 4, 6), "piece.returned", 1),
        ],
        # 6. failure reasons
        failure_rows
        if failure_rows is not None
        else [
            ("address_undeliverable", 2),
            ("piece.returned", 1),
        ],
    ]


# ── Service tests ───────────────────────────────────────────────────────


async def test_summarize_no_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _patch_pg(monkeypatch, _no_filter_queue())
    start = datetime(2026, 4, 1, tzinfo=UTC)
    end = datetime(2026, 4, 5, tzinfo=UTC)
    result = await direct_mail_analytics.summarize_direct_mail(
        organization_id=ORG_A,
        brand_id=None,
        channel_campaign_id=None,
        channel_campaign_step_id=None,
        start=start,
        end=end,
    )
    assert result["source"] == "postgres"
    assert result["totals"]["pieces"] == 20
    assert result["totals"]["delivered"] == 12
    assert result["totals"]["in_transit"] == 4
    assert result["totals"]["returned"] == 1
    assert result["totals"]["failed"] == 2
    assert result["totals"]["test_mode_count"] == 1

    funnel = result["funnel"]
    assert funnel["delivered"] == 12
    assert funnel["in_transit"] == 4
    assert funnel["returned"] == 1
    assert funnel["failed"] == 2
    assert funnel["queued"] == 1

    by_pt = {row["piece_type"]: row for row in result["by_piece_type"]}
    assert by_pt["postcard"]["count"] == 14
    assert by_pt["postcard"]["delivered"] == 10
    assert by_pt["letter"]["failed"] == 2

    # Daily trends prefilled for every day in [start, end].
    days = [row["date"] for row in result["daily_trends"]]
    assert days == [
        "2026-04-01",
        "2026-04-02",
        "2026-04-03",
        "2026-04-04",
        "2026-04-05",
    ]
    by_day = {row["date"]: row for row in result["daily_trends"]}
    assert by_day["2026-04-01"]["created"] == 5
    assert by_day["2026-04-02"]["created"] == 8
    assert by_day["2026-04-03"]["created"] == 0
    assert by_day["2026-04-05"]["delivered"] == 9
    assert by_day["2026-04-05"]["failed"] == 1

    # Failure reasons.
    fr = {row["reason"]: row["count"] for row in result["failure_reason_breakdown"]}
    assert fr["address_undeliverable"] == 2

    # Every query in the batch carries org_id in params.
    for c in capture:
        assert str(ORG_A) in c["params"]


async def test_summarize_brand_filter_pre_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When brand_id is supplied and not in the org, 404 fires before the
    aggregate queries."""
    _patch_pg(monkeypatch, [None])  # pre-validation lookup → no row
    with pytest.raises(direct_mail_analytics.DirectMailFilterNotFound):
        await direct_mail_analytics.summarize_direct_mail(
            organization_id=ORG_A,
            brand_id=BRAND_A,
            channel_campaign_id=None,
            channel_campaign_step_id=None,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 5, tzinfo=UTC),
        )


async def test_summarize_with_brand_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(
        monkeypatch,
        [
            (1,),  # brand pre-validation says exists
            *_no_filter_queue(),
        ],
    )
    result = await direct_mail_analytics.summarize_direct_mail(
        organization_id=ORG_A,
        brand_id=BRAND_A,
        channel_campaign_id=None,
        channel_campaign_step_id=None,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 5, tzinfo=UTC),
    )
    assert result["totals"]["pieces"] == 20


async def test_summarize_channel_campaign_filter_pre_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(direct_mail_analytics.DirectMailFilterNotFound):
        await direct_mail_analytics.summarize_direct_mail(
            organization_id=ORG_A,
            brand_id=None,
            channel_campaign_id=CC_A,
            channel_campaign_step_id=None,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 5, tzinfo=UTC),
        )


async def test_summarize_step_filter_pre_validates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pg(monkeypatch, [None])
    with pytest.raises(direct_mail_analytics.DirectMailFilterNotFound):
        await direct_mail_analytics.summarize_direct_mail(
            organization_id=ORG_A,
            brand_id=None,
            channel_campaign_id=None,
            channel_campaign_step_id=STEP_A,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 5, tzinfo=UTC),
        )


async def test_summarize_query_always_joins_through_brands_org(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every aggregate SQL must contain
    ``b.organization_id = %s`` so cross-org leakage at the query plan
    level is impossible."""
    capture = _patch_pg(monkeypatch, _no_filter_queue())
    await direct_mail_analytics.summarize_direct_mail(
        organization_id=ORG_A,
        brand_id=None,
        channel_campaign_id=None,
        channel_campaign_step_id=None,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 5, tzinfo=UTC),
    )
    for c in capture:
        assert "JOIN business.brands b ON b.id = p.brand_id" in c["sql"]
        assert "b.organization_id = %s" in c["sql"]


async def test_summarize_max_rows_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the count check exceeds 20k pieces, the service raises (the
    router maps to 400, not 500)."""
    _patch_pg(monkeypatch, [(20_001,)])
    with pytest.raises(ValueError):
        await direct_mail_analytics.summarize_direct_mail(
            organization_id=ORG_A,
            brand_id=None,
            channel_campaign_id=None,
            channel_campaign_step_id=None,
            start=datetime(2026, 4, 1, tzinfo=UTC),
            end=datetime(2026, 4, 5, tzinfo=UTC),
        )


async def test_summarize_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pg(
        monkeypatch,
        [
            (0,),  # count
            [],  # status
            [],  # type
            [],  # daily created
            [],  # daily events
            [],  # failure
        ],
    )
    result = await direct_mail_analytics.summarize_direct_mail(
        organization_id=ORG_A,
        brand_id=None,
        channel_campaign_id=None,
        channel_campaign_step_id=None,
        start=datetime(2026, 4, 1, tzinfo=UTC),
        end=datetime(2026, 4, 3, tzinfo=UTC),
    )
    assert result["totals"]["pieces"] == 0
    assert result["funnel"]["delivered"] == 0
    assert result["by_piece_type"] == []
    # Daily trends still prefilled.
    assert len(result["daily_trends"]) == 3


# ── HTTP tests ──────────────────────────────────────────────────────────


async def test_endpoint_no_filters(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, _no_filter_queue())
    resp = await _get("/api/v1/analytics/direct-mail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "postgres"
    assert body["totals"]["pieces"] == 20


async def test_endpoint_404_when_brand_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(f"/api/v1/analytics/direct-mail?brand_id={BRAND_A}")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "filter_not_found"


async def test_endpoint_404_when_channel_campaign_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(
        f"/api/v1/analytics/direct-mail?channel_campaign_id={CC_A}"
    )
    assert resp.status_code == 404


async def test_endpoint_404_when_step_in_other_org(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    _patch_pg(monkeypatch, [None])
    resp = await _get(
        f"/api/v1/analytics/direct-mail?channel_campaign_step_id={STEP_A}"
    )
    assert resp.status_code == 404


async def test_endpoint_uses_org_from_auth(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    capture = _patch_pg(monkeypatch, _no_filter_queue())
    resp = await _get("/api/v1/analytics/direct-mail")
    assert resp.status_code == 200
    # Every captured params tuple includes org_a's id.
    for c in capture:
        assert str(ORG_A) in c["params"]


async def test_endpoint_requires_org_context(
    monkeypatch: pytest.MonkeyPatch, auth_no_org: None
) -> None:
    monkeypatch.setattr(
        direct_mail_analytics,
        "get_db_connection",
        lambda: pytest.fail("service should not be called without org"),
    )
    resp = await _get("/api/v1/analytics/direct-mail")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "organization_context_required"


async def test_endpoint_rejects_invalid_brand_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        direct_mail_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get("/api/v1/analytics/direct-mail?brand_id=not-a-uuid")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_brand_id"


async def test_endpoint_rejects_invalid_step_id(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        direct_mail_analytics,
        "get_db_connection",
        lambda: pytest.fail("malformed id should short-circuit"),
    )
    resp = await _get(
        "/api/v1/analytics/direct-mail?channel_campaign_step_id=not-a-uuid"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_channel_campaign_step_id"


async def test_endpoint_rejects_invalid_window(
    monkeypatch: pytest.MonkeyPatch, auth_org_a: None
) -> None:
    monkeypatch.setattr(
        direct_mail_analytics,
        "get_db_connection",
        lambda: pytest.fail("invalid window should short-circuit"),
    )
    resp = await _get(
        "/api/v1/analytics/direct-mail"
        "?from=2026-04-30T00:00:00Z&to=2026-04-01T00:00:00Z"
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_window"
