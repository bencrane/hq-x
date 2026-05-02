"""Tests for app.services.strategy_synthesizer.

Mocks the Anthropic client to return a known-good YAML+markdown blob,
asserts disk write at data/initiatives/<id>/campaign_strategy.md,
asserts YAML front-matter parses, asserts the initiative
campaign_strategy_path is populated and status is strategy_ready. Also
covers the bad-YAML-then-retry path and the both-attempts-fail path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
import yaml

from app.services import anthropic_client, dex_client
from app.services import gtm_initiatives as gtm_svc
from app.services import strategy_synthesizer as synth

ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
INITIATIVE = UUID("11112222-3333-4444-5555-666677778888")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PARTNER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTRACT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
AUDIENCE = UUID("99999999-9999-9999-9999-999999999999")


VALID_STRATEGY_OUTPUT = """\
---
schema_version: 1
initiative_id: 11112222-3333-4444-5555-666677778888
generated_at: 2026-04-30T23:30:00Z
model: claude-opus-4-7

headline_offer: A warm intro to the right capital partner for fast-growing freight operators.
core_thesis: |
  Carriers expanding fleets need factoring AND equipment finance, but not as separate
  conversations. We see the situation and route to the right tool.
narrative_beats:
  - Capital is fragmented; you shouldn't have to figure out who funds what.
  - Net-60 broker terms strangle cash flow; transportation factors solve that.
  - New tractor purchases need 25% down; equipment finance unlocks the deal.
channel_mix:
  direct_mail:
    enabled: true
    touches:
      - touch_number: 1
        kind: postcard
        day_offset: 0
      - touch_number: 2
        kind: letter
        day_offset: 14
      - touch_number: 3
        kind: postcard
        day_offset: 28
  email:
    enabled: true
    touches:
      - touch_number: 1
        day_offset: 3
      - touch_number: 2
        day_offset: 17
      - touch_number: 3
        day_offset: 35
  voice_inbound:
    enabled: true
capital_outlay_plan:
  total_estimated_cents: 800000
  per_recipient_estimated_cents: 1600
personalization_variables:
  - name: dot_number
    how_to_pull: from FMCSA carrier row, field dot_number
  - name: power_unit_count
    how_to_pull: from FMCSA carrier row, field power_unit_count
  - name: legal_name
    how_to_pull: from FMCSA carrier row, field legal_name
anti_framings:
  - "Don't leave money on the table."
  - "Take your business to the next level."
  - solutions
---

# Capital Expansion × DAT — fast-growing carriers (90d)

## Why this audience, why this partner, why now
Carriers in the 10–50 power-unit band are growing faster than their cash position can support.

## The narrative beats expanded
Capital is fragmented; the right capital finds the right operator.

## Per-touch creative direction
Touch 1 names the situation; touch 2 explains the matching logic; touch 3 is loss-aversion.

## What we explicitly avoid
Marketing fluff vocabulary — see voice file.
"""


INVALID_STRATEGY_OUTPUT = """\
Sure — here is a strategy doc.

```
schema_version: 1
not even close to YAML front matter
```

# whatever
"""


@pytest.fixture
def stub(monkeypatch, tmp_path):
    """Stub every loader, point _INITIATIVES_ROOT at tmp_path so we
    write to a real but disposable directory."""
    state: dict[str, Any] = {
        "anthropic_calls": [],
        "anthropic_responses": [VALID_STRATEGY_OUTPUT],  # default: happy path
        "transitions": [],
        "set_paths": [],
        "initiative_status": "awaiting_strategy_synthesis",
        "campaign_strategy_path": None,
    }

    initiative_row = {
        "id": INITIATIVE,
        "organization_id": ORG,
        "brand_id": BRAND,
        "partner_id": PARTNER,
        "partner_contract_id": CONTRACT,
        "data_engine_audience_id": AUDIENCE,
        "partner_research_ref": "hqx://exa.exa_calls/partner-research-id",
        "strategic_context_research_ref": "hqx://exa.exa_calls/strategic-context-id",
        "campaign_strategy_path": None,
        "status": state["initiative_status"],
        "history": [],
        "metadata": {},
        "reservation_window_start": None,
        "reservation_window_end": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }

    async def fake_load_initiative(initiative_id):
        if initiative_id != INITIATIVE:
            from app.services.strategy_synthesizer import StrategySynthesizerError
            raise StrategySynthesizerError(f"initiative {initiative_id} not found")
        initiative_row["status"] = state["initiative_status"]
        initiative_row["campaign_strategy_path"] = state["campaign_strategy_path"]
        return initiative_row

    async def fake_load_partner(partner_id):
        return {
            "id": PARTNER,
            "name": "DAT",
            "domain": "dat.com",
            "primary_contact_name": "Lead",
            "primary_contact_email": "lead@dat.com",
            "primary_phone": None,
            "intro_email": None,
            "hours_of_operation_config": {},
            "metadata": {},
        }

    async def fake_load_contract(contract_id):
        return {
            "id": CONTRACT,
            "partner_id": PARTNER,
            "pricing_model": "flat_90d",
            "amount_cents": 2_500_000,
            "duration_days": 90,
            "max_capital_outlay_cents": 1_000_000,
            "qualification_rules": {"power_units_min": 10},
            "terms_blob": None,
            "status": "active",
            "starts_at": None,
            "ends_at": None,
        }

    async def fake_load_brand(brand_id):
        return {"id": BRAND, "name": "Capital Expansion", "domain": "capitalexpansion.com"}

    async def fake_get_audience_descriptor(spec_id, *, bearer_token=None):
        return {
            "spec": {"name": "DAT — fast-growing carriers (prototype)"},
            "template": {"name": "Fast-growing carriers", "slug": "fmcsa-fgc"},
            "audience_attributes": [],
        }

    async def fake_fetch_exa_payload(ref):
        return {"output": {"content": f"(payload for {ref})"}}

    async def fake_complete(*, system, messages, model=None, max_tokens=8192):
        state["anthropic_calls"].append(
            {"system": system, "messages": messages, "model": model}
        )
        if not state["anthropic_responses"]:
            raise AssertionError("no more anthropic responses queued")
        text = state["anthropic_responses"].pop(0)
        return {
            "text": text,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_creation_input_tokens": 50,
                "cache_read_input_tokens": 0,
            },
            "model": model or "claude-opus-4-7",
            "stop_reason": "end_turn",
        }

    async def fake_transition(initiative_id, *, new_status, history_event=None):
        state["transitions"].append(new_status)
        state["initiative_status"] = new_status
        return {**initiative_row, "status": new_status}

    async def fake_set_path(initiative_id, path):
        state["set_paths"].append(path)
        state["campaign_strategy_path"] = path

    monkeypatch.setattr(synth, "_load_initiative", fake_load_initiative)
    monkeypatch.setattr(synth, "_load_partner", fake_load_partner)
    monkeypatch.setattr(synth, "_load_partner_contract", fake_load_contract)
    monkeypatch.setattr(synth, "_load_brand", fake_load_brand)
    monkeypatch.setattr(synth, "_fetch_exa_payload", fake_fetch_exa_payload)
    monkeypatch.setattr(
        dex_client, "get_audience_descriptor", fake_get_audience_descriptor
    )
    monkeypatch.setattr(
        synth.dex_client, "get_audience_descriptor", fake_get_audience_descriptor
    )
    monkeypatch.setattr(anthropic_client, "complete", fake_complete)
    monkeypatch.setattr(synth.anthropic_client, "complete", fake_complete)
    monkeypatch.setattr(gtm_svc, "transition_status", fake_transition)
    monkeypatch.setattr(synth.gtm_svc, "transition_status", fake_transition)
    monkeypatch.setattr(gtm_svc, "set_campaign_strategy_path", fake_set_path)
    monkeypatch.setattr(synth.gtm_svc, "set_campaign_strategy_path", fake_set_path)

    # Redirect the on-disk root to tmp_path.
    monkeypatch.setattr(synth, "_INITIATIVES_ROOT", tmp_path)

    return state


# ── happy path ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_writes_strategy_and_transitions(stub, tmp_path):
    result = await synth.synthesize_initiative_strategy(
        initiative_id=INITIATIVE,
        organization_id=ORG,
    )
    expected_path = tmp_path / str(INITIATIVE) / "campaign_strategy.md"
    assert result["path"] == str(expected_path)
    assert expected_path.exists()
    written = expected_path.read_text()
    # YAML front-matter parses cleanly.
    parsed = synth._parse_front_matter(written)
    assert parsed is not None
    fm, _body = parsed
    for key in synth._REQUIRED_FRONT_MATTER_KEYS:
        assert key in fm, f"missing required key {key}"
    # Initiative state advanced.
    assert "strategy_ready" in stub["transitions"]
    assert stub["set_paths"] == [str(expected_path)]
    # Single Anthropic call, no retry.
    assert len(stub["anthropic_calls"]) == 1


# ── retry-on-bad-YAML path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_retries_once_on_invalid_yaml(stub, tmp_path):
    # First response invalid, second valid.
    stub["anthropic_responses"] = [
        INVALID_STRATEGY_OUTPUT,
        VALID_STRATEGY_OUTPUT,
    ]
    result = await synth.synthesize_initiative_strategy(
        initiative_id=INITIATIVE,
        organization_id=ORG,
    )
    expected_path = tmp_path / str(INITIATIVE) / "campaign_strategy.md"
    assert expected_path.exists()
    assert result["path"] == str(expected_path)
    # Two anthropic calls — original + retry.
    assert len(stub["anthropic_calls"]) == 2
    # The retry's user message should reference invalid YAML.
    retry_messages = stub["anthropic_calls"][1]["messages"]
    assert any(
        "invalid YAML front-matter" in m.get("content", "")
        for m in retry_messages
        if m.get("role") == "user"
    )
    assert "strategy_ready" in stub["transitions"]


# ── both-attempts-fail path ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_synthesize_marks_failed_when_both_attempts_invalid(stub, tmp_path):
    stub["anthropic_responses"] = [
        INVALID_STRATEGY_OUTPUT,
        INVALID_STRATEGY_OUTPUT,
    ]
    with pytest.raises(synth.StrategySynthesizerError):
        await synth.synthesize_initiative_strategy(
            initiative_id=INITIATIVE,
            organization_id=ORG,
        )
    failed_path = tmp_path / str(INITIATIVE) / "failed_synthesis.md"
    assert failed_path.exists()
    body = failed_path.read_text()
    assert "<original_attempt>" in body
    assert "<retry_attempt>" in body
    assert "failed" in stub["transitions"]
    # No campaign_strategy_path was set on the initiative.
    assert stub["set_paths"] == []


# ── front-matter validation unit ───────────────────────────────────────────


def test_parse_front_matter_round_trip():
    parsed = synth._parse_front_matter(VALID_STRATEGY_OUTPUT)
    assert parsed is not None
    fm, body = parsed
    assert fm["headline_offer"].startswith("A warm intro")
    assert isinstance(fm["narrative_beats"], list)
    assert "Capital Expansion" in body


def test_parse_front_matter_rejects_no_delimiters():
    assert synth._parse_front_matter(INVALID_STRATEGY_OUTPUT) is None


def test_front_matter_valid_requires_all_keys():
    fm = yaml.safe_load(
        VALID_STRATEGY_OUTPUT.split("---")[1]
    )
    ok, err = synth._front_matter_valid(fm)
    assert ok, err

    # Drop a required key — should fail.
    incomplete = {k: v for k, v in fm.items() if k != "anti_framings"}
    ok2, err2 = synth._front_matter_valid(incomplete)
    assert not ok2
    assert "anti_framings" in (err2 or "")
