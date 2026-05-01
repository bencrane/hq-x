"""Unit tests for app.services.materializer_execution.

The DB and DEX client are mocked — these tests assert the planning
logic, validation, and dispatch shape rather than DB-level behavior
(which is exercised by the e2e seed script).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import materializer_execution as me
from app.services import gtm_initiatives as gtm_svc
from app.services import dex_client


INITIATIVE_ID = UUID("11111111-2222-3333-4444-555555555555")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PARTNER_CONTRACT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
AUDIENCE = UUID("99999999-9999-9999-9999-999999999999")


def _initiative_dict() -> dict[str, Any]:
    return {
        "id": INITIATIVE_ID,
        "organization_id": ORG,
        "brand_id": BRAND,
        "partner_contract_id": PARTNER_CONTRACT,
        "data_engine_audience_id": AUDIENCE,
    }


def _well_formed_channel_step_plan() -> dict[str, Any]:
    return {
        "campaign": {
            "name": "initiative-cap-1",
            "description": "Test description",
            "metadata": {"initiative_id": str(INITIATIVE_ID)},
        },
        "channel_campaigns": [
            {"channel": "direct_mail", "provider": "lob", "name": "DM"},
            {"channel": "email", "provider": "emailbison", "name": "Email"},
            {"channel": "voice_inbound", "provider": "vapi", "name": "Voice"},
        ],
        "steps": [
            {
                "channel": "direct_mail",
                "step_index": 0,
                "name": "Touch 1",
                "delay_days_from_previous": 0,
                "channel_specific_config": {
                    "mailer_type": "postcard", "estimated_cost_cents": 150,
                },
                "landing_page_config_placeholder": {"channel_tier": "low"},
            },
            {
                "channel": "email",
                "step_index": 0,
                "name": "Email 1",
                "delay_days_from_previous": 3,
                "channel_specific_config": {},
                "landing_page_config_placeholder": {"channel_tier": "low"},
            },
        ],
    }


# ── execute_channel_step_plan validation ───────────────────────────────


@pytest.mark.asyncio
async def test_execute_channel_step_plan_rejects_unknown_channel(
    monkeypatch,
):
    async def fake_get(initiative_id, *, organization_id=None):
        return _initiative_dict()
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)

    plan = _well_formed_channel_step_plan()
    plan["channel_campaigns"].append(
        {"channel": "telepathy", "provider": "lob", "name": "??"}
    )
    with pytest.raises(me.MaterializerExecutionError, match="invalid channel"):
        await me.execute_channel_step_plan(INITIATIVE_ID, plan)


@pytest.mark.asyncio
async def test_execute_channel_step_plan_rejects_invalid_provider(
    monkeypatch,
):
    async def fake_get(initiative_id, *, organization_id=None):
        return _initiative_dict()
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)

    plan = _well_formed_channel_step_plan()
    plan["channel_campaigns"][0]["provider"] = "uspspersonally"
    with pytest.raises(me.MaterializerExecutionError, match="invalid provider"):
        await me.execute_channel_step_plan(INITIATIVE_ID, plan)


@pytest.mark.asyncio
async def test_execute_channel_step_plan_requires_campaign_section(
    monkeypatch,
):
    async def fake_get(initiative_id, *, organization_id=None):
        return _initiative_dict()
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)
    plan = _well_formed_channel_step_plan()
    plan["channel_campaigns"] = []
    with pytest.raises(me.MaterializerExecutionError, match="missing"):
        await me.execute_channel_step_plan(INITIATIVE_ID, plan)


@pytest.mark.asyncio
async def test_execute_channel_step_plan_requires_known_initiative(monkeypatch):
    async def fake_get(initiative_id, *, organization_id=None):
        return None
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)
    with pytest.raises(me.MaterializerExecutionError, match="not found"):
        await me.execute_channel_step_plan(
            INITIATIVE_ID, _well_formed_channel_step_plan()
        )


# ── execute_audience_plan decision dispatch ────────────────────────────


@pytest.mark.asyncio
async def test_execute_audience_plan_rejects_explicit_reject(monkeypatch):
    async def fake_get(initiative_id, *, organization_id=None):
        return _initiative_dict()
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)

    with pytest.raises(me.MaterializerExecutionError, match="rejected"):
        await me.execute_audience_plan(
            INITIATIVE_ID,
            {"decision": "reject_size_mismatch",
             "size_decision_reason": "outlay over cap"},
            dm_step_ids=[uuid4()],
        )


@pytest.mark.asyncio
async def test_execute_audience_plan_rejects_unknown_decision(monkeypatch):
    async def fake_get(initiative_id, *, organization_id=None):
        return _initiative_dict()
    monkeypatch.setattr(gtm_svc, "get_initiative", fake_get)
    with pytest.raises(me.MaterializerExecutionError, match="unsupported decision"):
        await me.execute_audience_plan(
            INITIATIVE_ID,
            {"decision": "make_it_bigger"},
            dm_step_ids=[uuid4()],
        )


# ── _member_to_spec ─────────────────────────────────────────────────────


def test_member_to_spec_returns_none_for_missing_dot():
    assert me._member_to_spec({"legal_name": "no dot"}) is None


def test_member_to_spec_uses_phys_address_when_present():
    spec = me._member_to_spec(
        {
            "dot_number": 1234567,
            "legal_name": "ACME Carriers",
            "phys_street": "1 Main",
            "phys_city": "Austin",
            "phys_state": "TX",
            "phys_zip": "73301",
        }
    )
    assert spec is not None
    assert spec.external_source == "fmcsa"
    assert spec.external_id == "1234567"
    assert spec.mailing_address["city"] == "Austin"
    assert spec.metadata["dot_number"] == 1234567


def test_member_to_spec_falls_back_to_mail_address():
    spec = me._member_to_spec(
        {
            "dot_number": 9988776,
            "mail_street": "PO 123",
            "mail_city": "Boise",
            "mail_state": "ID",
        }
    )
    assert spec is not None
    assert spec.mailing_address["city"] == "Boise"
