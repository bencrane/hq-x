"""End-to-end smoke for LobAdapter.activate_step against Lob test mode.

Exercises the real Lob HTTP path (campaign create → creative create →
upload create → file upload) with a synthesized step + channel_campaign
and a 3-recipient mock audience. The DB layer (audience-row query and
Dub mint) is monkey-patched so this runs without hq-x's Postgres.

Run:

    cd /Users/benjamincrane/hq-x
    doppler run -- python scripts/smoke_lob_activate_step.py

Prints the resulting cmp_*, crv_*, upl_* ids on success. Fails with a
non-zero exit + structured error on any sub-step failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

# Ensure the repo root is importable when run from the scripts dir.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.models.campaigns import (  # noqa: E402
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
)
from app.providers.lob import adapter as lob_adapter  # noqa: E402
from app.providers.lob.adapter import LobAdapter  # noqa: E402


def _now() -> datetime:
    return datetime.now(UTC)


def _step() -> ChannelCampaignStepResponse:
    """Synthesize a step row that looks like one the activator would
    receive, with operator-supplied creative for a 4x6 postcard."""
    front_html = (
        "<html><body style='font-family:Helvetica;font-size:24px;"
        "padding:1in'><h1>{{name}}</h1>"
        "<p>Smoke test postcard from hq-x DMaaS foundation.</p></body></html>"
    )
    back_html = (
        "<html><body style='font-family:Helvetica;font-size:18px;"
        "padding:1in'><p>Scan to learn more</p></body></html>"
    )
    return ChannelCampaignStepResponse(
        id=uuid.uuid4(),
        channel_campaign_id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        brand_id=uuid.uuid4(),
        step_order=1,
        name="DMaaS foundation smoke",
        delay_days_from_previous=0,
        scheduled_send_at=None,
        creative_ref=uuid.uuid4(),
        channel_specific_config={
            # Lob's campaigns API requires LIVE mode — there is no test
            # mode for /v1/campaigns. We use the live key but stop short
            # of /v1/campaigns/{id}/send so no pieces actually print or
            # bill. The created campaign + creative + upload are free
            # artifacts that can be deleted afterwards.
            "test_mode": False,
            "landing_page_url": "https://example.com/lp",
            "lob_creative_payload": {
                "resource_type": "postcard",
                "front": front_html,
                "back": back_html,
                "details": {"size": "4x6"},
                "from": {
                    "name": "HQ-X",
                    "address_line1": "210 King St",
                    "address_city": "San Francisco",
                    "address_state": "CA",
                    "address_zip": "94107",
                    "address_country": "US",
                },
            },
        },
        external_provider_id=None,
        external_provider_metadata={},
        status="pending",
        activated_at=None,
        metadata={},
        created_at=_now(),
        updated_at=_now(),
    )


def _cc() -> ChannelCampaignResponse:
    return ChannelCampaignResponse(
        id=uuid.uuid4(),
        campaign_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        brand_id=uuid.uuid4(),
        name="smoke cc",
        channel="direct_mail",
        provider="lob",
        audience_spec_id=None,
        audience_snapshot_count=None,
        status="draft",
        start_offset_days=0,
        scheduled_send_at=None,
        schedule_config={},
        provider_config={},
        design_id=None,
        metadata={},
        created_by_user_id=None,
        created_at=_now(),
        updated_at=_now(),
        archived_at=None,
    )


# Three Lob-test-friendly recipients. Lob's test mode accepts any
# well-formed US address; these are real-but-public addresses.
_AUDIENCE_ROWS = [
    (
        "Test Recipient One",
        {
            "line1": "1 Hacker Way",
            "line2": None,
            "city": "Menlo Park",
            "state": "CA",
            "zip": "94025",
            "country": "US",
        },
        "https://example.com/lp?r=one",
    ),
    (
        "Test Recipient Two",
        {
            "line1": "350 5th Ave",
            "line2": "Floor 21",
            "city": "New York",
            "state": "NY",
            "zip": "10118",
            "country": "US",
        },
        "https://example.com/lp?r=two",
    ),
    (
        "Test Recipient Three",
        {
            "line1": "1600 Amphitheatre Pkwy",
            "line2": None,
            "city": "Mountain View",
            "state": "CA",
            "zip": "94043",
            "country": "US",
        },
        "https://example.com/lp?r=three",
    ),
]


class _FakeCursor:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def execute(self, sql: str, params: Any = None) -> None:  # noqa: ARG002
        return None

    async def fetchall(self) -> list[tuple]:
        return list(self._rows)

    async def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows)


@asynccontextmanager
async def _fake_db():
    yield _FakeConn(_AUDIENCE_ROWS)


async def _no_op_mint(**kwargs: Any) -> None:  # noqa: ARG001
    return None


async def main() -> int:
    if not os.getenv("LOB_API_KEY"):
        print(
            "ERROR: LOB_API_KEY not in env. Run via "
            "`doppler run -- python scripts/smoke_lob_activate_step.py`. "
            "Note: Lob's /v1/campaigns endpoint requires LIVE mode; "
            "LOB_API_KEY_TEST cannot be used.",
            file=sys.stderr,
        )
        return 2

    # Patch DB + Dub mint so we don't require Postgres or Dub API keys.
    lob_adapter.get_db_connection = _fake_db  # type: ignore[assignment]
    lob_adapter.mint_links_for_step = _no_op_mint  # type: ignore[assignment]

    step = _step()
    cc = _cc()
    print(f"step.id            = {step.id}")
    print(f"channel_campaign   = {cc.id}")
    print("activating against Lob test mode…")

    result = await LobAdapter(test_mode=False).activate_step(step=step, channel_campaign=cc)

    print()
    print(f"status             = {result.status}")
    print(f"external_provider  = {result.external_provider_id}")
    for k, v in (result.metadata or {}).items():
        if k == "lob_upload_file_response":
            print(f"  {k:<22}= {v}")
        else:
            print(f"  {k:<22}= {v!r}")

    if result.status == "scheduled":
        print()
        print("✓ all four Lob calls succeeded.")
        print(f"  Lob campaign:  {result.external_provider_id}")
        print(f"  Lob creative:  {result.metadata.get('lob_creative_id')}")
        print(f"  Lob upload:    {result.metadata.get('lob_upload_id')}")
        print(f"  Audience rows: {result.metadata.get('audience_size')}")
        return 0
    print()
    print("✗ activation did not reach scheduled. See metadata above.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
