"""Tests for ``LobAdapter.activate_step`` — the Slice 1 flow.

Exercises:
* Pre-flight validation (missing creative_ref / landing_page_url /
  lob_creative_payload → status='failed' with a structured error).
* Happy path: dub mint → campaign create → creative create → audience
  CSV → upload create → upload file → status='scheduled' with all ids
  in metadata.
* Idempotent retry: a second call against a step that already has
  ``external_provider_id`` and partial metadata skips the sub-steps
  that already succeeded.
* Partial-failure semantics: a creative-create / upload-create /
  upload-file failure leaves the step in 'activating' with whichever
  ids did succeed persisted.

The Lob HTTP client is patched at the function level. The audience-row
query (`get_db_connection` inside the adapter) is patched with a
queue-driven fake mirroring the analytics-test pattern.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.models.campaigns import (
    ChannelCampaignResponse,
    ChannelCampaignStepResponse,
)
from app.providers.lob import adapter as lob_adapter
from app.providers.lob import client as lob_client
from app.providers.lob.adapter import LobActivationResult, LobAdapter


def _step(**overrides) -> ChannelCampaignStepResponse:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "id": uuid4(),
        "channel_campaign_id": uuid4(),
        "campaign_id": uuid4(),
        "organization_id": uuid4(),
        "brand_id": uuid4(),
        "step_order": 1,
        "name": "Postcard day 0",
        "delay_days_from_previous": 0,
        "scheduled_send_at": None,
        "creative_ref": uuid4(),
        "channel_specific_config": {
            "landing_page_url": "https://customer.example/lp",
            "lob_creative_payload": {
                "resource_type": "postcard",
                "front": "<html>front</html>",
                "back": "<html>back</html>",
                "details": {"size": "4x6"},
            },
        },
        "external_provider_id": None,
        "external_provider_metadata": {},
        "status": "pending",
        "activated_at": None,
        "metadata": {},
        "created_at": now,
        "updated_at": now,
    }
    base.update(overrides)
    return ChannelCampaignStepResponse(**base)


def _cc() -> ChannelCampaignResponse:
    now = datetime.now(UTC)
    return ChannelCampaignResponse(
        id=uuid4(),
        campaign_id=uuid4(),
        organization_id=uuid4(),
        brand_id=uuid4(),
        name="cc",
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
        created_at=now,
        updated_at=now,
        archived_at=None,
    )


# ── DB fake for the audience-row query ──────────────────────────────────


class _FakeCursor:
    def __init__(self, rows: list[tuple], capture: list[dict[str, Any]]):
        self._rows = rows
        self._capture = capture

    async def __aenter__(self) -> _FakeCursor:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def execute(self, sql: str, params: Any = None) -> None:
        self._capture.append({"sql": sql, "params": params})

    async def fetchone(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[tuple]:
        return list(self._rows)


class _FakeConn:
    def __init__(self, rows: list[tuple], capture: list[dict[str, Any]]):
        self._rows = rows
        self._capture = capture

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._rows, self._capture)


@pytest.fixture
def patch_db(monkeypatch):
    state: dict[str, Any] = {
        "audience_rows": [
            (
                "Acme Inc.",
                {
                    "line1": "123 Main St",
                    "line2": None,
                    "city": "San Francisco",
                    "state": "CA",
                    "zip": "94103",
                    "country": "US",
                },
                "https://dub.sh/abc123",
            ),
            (
                "Beta LLC",
                {
                    "line1": "456 Oak Ave",
                    "line2": "Suite 4",
                    "city": "Brooklyn",
                    "state": "NY",
                    "zip": "11201",
                    "country": "US",
                },
                "https://dub.sh/def456",
            ),
        ],
        "capture": [],
    }

    @asynccontextmanager
    async def _conn():
        yield _FakeConn(state["audience_rows"], state["capture"])

    async def _fake_cc_context(*, channel_campaign_id):
        # Default: legacy / no initiative. Tests that exercise the
        # initiative-tag path can override via state["cc_context"].
        return state.get("cc_context")

    monkeypatch.setattr(lob_adapter, "get_db_connection", _conn)
    monkeypatch.setattr(
        lob_adapter, "get_channel_campaign_context", _fake_cc_context
    )
    return state


@pytest.fixture
def patch_api_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LOB_API_KEY", "live_key", raising=False)
    monkeypatch.setattr(
        settings, "LOB_API_KEY_TEST", "test_key", raising=False
    )


@pytest.fixture
def patch_dub_mint(monkeypatch):
    """No-op mint by default; tests that need a mint failure override
    state["raise"] to a ``StepLinkMintingError``."""
    state: dict[str, Any] = {"calls": [], "raise": None}

    async def fake_mint(**kwargs):
        state["calls"].append(kwargs)
        if state["raise"]:
            raise state["raise"]

    monkeypatch.setattr(lob_adapter, "mint_links_for_step", fake_mint)
    return state


@pytest.fixture
def patch_lob(monkeypatch):
    """Patch the four Lob HTTP calls the adapter makes."""
    state: dict[str, Any] = {
        "campaign_calls": [],
        "creative_calls": [],
        "upload_calls": [],
        "file_calls": [],
        "campaign_response": {"id": "cmp_xyz", "name": "step-1"},
        "creative_response": {"id": "crv_xyz"},
        "upload_response": {"id": "upl_xyz", "state": "Draft"},
        "file_response": {"message": "File uploaded successfully"},
        "campaign_raise": None,
        "creative_raise": None,
        "upload_raise": None,
        "file_raise": None,
    }

    def fake_create_campaign(api_key, payload, **kwargs):
        state["campaign_calls"].append((api_key, payload, kwargs))
        if state["campaign_raise"]:
            raise state["campaign_raise"]
        return state["campaign_response"]

    def fake_create_creative(api_key, payload, **kwargs):
        state["creative_calls"].append((api_key, payload, kwargs))
        if state["creative_raise"]:
            raise state["creative_raise"]
        return state["creative_response"]

    def fake_create_upload(api_key, payload, **kwargs):
        state["upload_calls"].append((api_key, payload, kwargs))
        if state["upload_raise"]:
            raise state["upload_raise"]
        return state["upload_response"]

    def fake_upload_file(api_key, upload_id, **kwargs):
        state["file_calls"].append((api_key, upload_id, kwargs))
        if state["file_raise"]:
            raise state["file_raise"]
        return state["file_response"]

    monkeypatch.setattr(lob_client, "create_campaign", fake_create_campaign)
    monkeypatch.setattr(lob_client, "create_creative", fake_create_creative)
    monkeypatch.setattr(lob_client, "create_upload", fake_create_upload)
    monkeypatch.setattr(lob_client, "upload_file", fake_upload_file)
    return state


# ── Pre-flight validation ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_channel_raises(patch_api_key):
    cc = _cc()
    cc = cc.model_copy(update={"channel": "email"})
    with pytest.raises(lob_client.LobProviderError):
        await LobAdapter().activate_step(step=_step(), channel_campaign=cc)


@pytest.mark.asyncio
async def test_missing_creative_ref_returns_failed(patch_api_key):
    step = _step(creative_ref=None)
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "missing_creative_ref"


@pytest.mark.asyncio
async def test_missing_landing_page_url_returns_failed(patch_api_key):
    step = _step(channel_specific_config={"lob_creative_payload": {}})
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "missing_landing_page_url"


@pytest.mark.asyncio
async def test_missing_lob_creative_payload_returns_failed(patch_api_key):
    step = _step(
        channel_specific_config={"landing_page_url": "https://x.example"}
    )
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "missing_lob_creative_payload"


@pytest.mark.asyncio
async def test_invalid_resource_type_returns_failed(patch_api_key):
    step = _step(
        channel_specific_config={
            "landing_page_url": "https://x.example",
            "lob_creative_payload": {
                "resource_type": "snap_pack",  # not allowed in V1
                "front": "<html>x</html>",
                "back": "<html>y</html>",
                "details": {},
            },
        }
    )
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "missing_lob_creative_payload"


# ── Happy path ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_activation_happy_path(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    step = _step()
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())

    assert result.status == "scheduled"
    assert result.external_provider_id == "cmp_xyz"
    assert result.metadata["lob_creative_id"] == "crv_xyz"
    assert result.metadata["lob_upload_id"] == "upl_xyz"
    assert result.metadata["audience_size"] == 2

    # All four Lob calls fired in order, exactly once.
    assert len(patch_lob["campaign_calls"]) == 1
    assert len(patch_lob["creative_calls"]) == 1
    assert len(patch_lob["upload_calls"]) == 1
    assert len(patch_lob["file_calls"]) == 1

    # Campaign create carries the six-tuple metadata.
    campaign_payload = patch_lob["campaign_calls"][0][1]
    md = campaign_payload["metadata"]
    assert md["organization_id"] == str(step.organization_id)
    assert md["channel_campaign_step_id"] == str(step.id)

    # Creative bound to the campaign.
    creative_payload = patch_lob["creative_calls"][0][1]
    assert creative_payload["campaign_id"] == "cmp_xyz"
    assert creative_payload["resource_type"] == "postcard"
    assert creative_payload["front"] == "<html>front</html>"

    # Upload references the campaign + uses our column mappings.
    upload_payload = patch_lob["upload_calls"][0][1]
    assert upload_payload["campaignId"] == "cmp_xyz"
    assert upload_payload["requiredAddressColumnMapping"]["name"] == "recipient_name"
    assert (
        upload_payload["mergeVariableColumnMapping"]["qr_code_redirect_url"]
        == "qr_code_redirect_url"
    )

    # CSV bytes carry both rows + the QR URL.
    file_call = patch_lob["file_calls"][0]
    assert file_call[1] == "upl_xyz"
    csv_bytes: bytes = file_call[2]["file_content"]
    text = csv_bytes.decode("utf-8")
    assert "Acme Inc." in text
    assert "Beta LLC" in text
    assert "https://dub.sh/abc123" in text


@pytest.mark.asyncio
async def test_idempotency_keys_derived_from_step_id(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    step = _step()
    await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    campaign_kwargs = patch_lob["campaign_calls"][0][2]
    creative_kwargs = patch_lob["creative_calls"][0][2]
    assert campaign_kwargs["idempotency_key"] == f"hqx-step-{step.id}-campaign"
    assert creative_kwargs["idempotency_key"] == f"hqx-step-{step.id}-creative"


# ── Failure semantics ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dub_mint_failure_short_circuits(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    from app.dmaas.step_link_minting import StepLinkMintingError

    patch_dub_mint["raise"] = StepLinkMintingError(
        "dub down", recipient_id=uuid4()
    )
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "dub_mint_failed"
    # No Lob calls fired.
    assert patch_lob["campaign_calls"] == []


@pytest.mark.asyncio
async def test_campaign_create_failure_returns_failed(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    patch_lob["campaign_raise"] = lob_client.LobProviderError("campaign 500")
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "failed"
    assert result.metadata["error"] == "lob_campaign_create_failed"
    # Creative + upload not attempted.
    assert patch_lob["creative_calls"] == []


@pytest.mark.asyncio
async def test_creative_failure_returns_activating_with_campaign(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    patch_lob["creative_raise"] = lob_client.LobProviderError("creative 500")
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "activating"
    assert result.external_provider_id == "cmp_xyz"
    assert result.metadata["error"] == "lob_creative_create_failed"
    assert patch_lob["upload_calls"] == []


@pytest.mark.asyncio
async def test_upload_create_failure_returns_activating_with_creative(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    patch_lob["upload_raise"] = lob_client.LobProviderError("upload 500")
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "activating"
    assert result.external_provider_id == "cmp_xyz"
    assert result.metadata["error"] == "lob_upload_create_failed"
    assert result.metadata["lob_creative_id"] == "crv_xyz"
    assert patch_lob["file_calls"] == []


@pytest.mark.asyncio
async def test_upload_file_failure_returns_activating_with_upload(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    patch_lob["file_raise"] = lob_client.LobProviderError("file 500")
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "activating"
    assert result.metadata["error"] == "lob_upload_file_failed"
    assert result.metadata["lob_upload_id"] == "upl_xyz"
    assert result.metadata["lob_creative_id"] == "crv_xyz"


@pytest.mark.asyncio
async def test_empty_audience_returns_activating(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    """No pending memberships → nothing to upload; mark activating so the
    operator can materialize the audience and retry."""
    patch_db["audience_rows"] = []
    result = await LobAdapter().activate_step(step=_step(), channel_campaign=_cc())
    assert result.status == "activating"
    assert result.metadata["error"] == "audience_empty"
    # Campaign + creative were created but no upload.
    assert len(patch_lob["campaign_calls"]) == 1
    assert len(patch_lob["creative_calls"]) == 1
    assert patch_lob["upload_calls"] == []


# ── Idempotent retry ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_retry_skips_campaign_and_creative_when_metadata_set(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    """Step already has external_provider_id (campaign) and a creative id
    in metadata → adapter resumes at the upload step."""
    step = _step(
        external_provider_id="cmp_existing",
        external_provider_metadata={"lob_creative_id": "crv_existing"},
    )
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())

    assert result.status == "scheduled"
    assert result.external_provider_id == "cmp_existing"
    assert result.metadata["lob_creative_id"] == "crv_existing"

    # Campaign + creative skipped. Upload + file fired.
    assert patch_lob["campaign_calls"] == []
    assert patch_lob["creative_calls"] == []
    assert len(patch_lob["upload_calls"]) == 1
    assert len(patch_lob["file_calls"]) == 1
    assert patch_lob["upload_calls"][0][1]["campaignId"] == "cmp_existing"


@pytest.mark.asyncio
async def test_retry_skips_upload_when_existing_upload_id_set(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    """All three Lob objects already exist; only the file POST runs."""
    step = _step(
        external_provider_id="cmp_existing",
        external_provider_metadata={
            "lob_creative_id": "crv_existing",
            "lob_upload_id": "upl_existing",
        },
    )
    result = await LobAdapter().activate_step(step=step, channel_campaign=_cc())

    assert result.status == "scheduled"
    assert patch_lob["campaign_calls"] == []
    assert patch_lob["creative_calls"] == []
    assert patch_lob["upload_calls"] == []
    assert len(patch_lob["file_calls"]) == 1
    assert patch_lob["file_calls"][0][1] == "upl_existing"


# ── Audience-row query org isolation ────────────────────────────────────


@pytest.mark.asyncio
async def test_audience_row_query_filters_by_step_and_org(
    patch_api_key, patch_db, patch_dub_mint, patch_lob
):
    step = _step()
    await LobAdapter().activate_step(step=step, channel_campaign=_cc())
    # The audience query is the only DB call the adapter makes.
    assert len(patch_db["capture"]) == 1
    sql = patch_db["capture"][0]["sql"]
    assert "scr.channel_campaign_step_id = %s" in sql
    assert "scr.organization_id = %s" in sql
    assert "scr.status = 'pending'" in sql
    params = patch_db["capture"][0]["params"]
    assert str(step.id) in params
    assert str(step.organization_id) in params


def test_validate_lob_creative_payload_accepts_minimal_postcard():
    err = lob_adapter._validate_lob_creative_payload(
        {
            "resource_type": "postcard",
            "front": "x",
            "back": "y",
            "details": {"size": "4x6"},
        }
    )
    assert err is None


@pytest.mark.parametrize(
    "missing_field", ["front", "back", "details"]
)
def test_validate_lob_creative_payload_rejects_missing_field(missing_field):
    payload = {
        "resource_type": "postcard",
        "front": "x",
        "back": "y",
        "details": {"size": "4x6"},
    }
    payload.pop(missing_field)
    err = lob_adapter._validate_lob_creative_payload(payload)
    assert err is not None
    assert missing_field in err


def test_validate_lob_creative_payload_rejects_non_dict():
    assert lob_adapter._validate_lob_creative_payload(None) is not None
    assert lob_adapter._validate_lob_creative_payload("string") is not None


def _quiet():
    """Silence unused-import warnings for the LobActivationResult import in
    pure-test files. We keep the import for type-name visibility in IDEs."""
    return LobActivationResult
