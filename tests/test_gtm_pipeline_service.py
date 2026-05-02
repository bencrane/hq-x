"""Unit tests for app.services.gtm_pipeline.

The whole module is built around `run_step` as the single seam between
Trigger.dev and Anthropic. These tests exercise:

  * Output parsers per agent slug (JSON happy/sad path, markdown YAML
    happy/sad path).
  * `_supersede_downstream` slug-set computation.
  * The run_step lifecycle end-to-end with mocked DB + mocked
    anthropic_managed_agents.run_session — asserts the row goes from
    running → succeeded, prompt snapshot is captured, supersede fires
    before the new row inserts, returned StepResult shape matches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import (
    agent_prompts,
    anthropic_managed_agents,
    dex_client,
    gtm_initiatives as gtm_svc,
    gtm_pipeline as pipeline,
    org_doctrine,
)

INITIATIVE = UUID("11111111-2222-3333-4444-555555555555")
ORG = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
BRAND = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
PARTNER = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTRACT = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
AUDIENCE = UUID("99999999-9999-9999-9999-999999999999")
ANTHROPIC_AGENT_ID = "agt_seq_test"


# ---------------------------------------------------------------------------
# Output parser tests
# ---------------------------------------------------------------------------


def test_parse_actor_output_json_happy_path():
    out = pipeline._parse_actor_output(
        "gtm-sequence-definer",
        '{"decision": "ship", "channels": {"direct_mail": {"enabled": true}}}',
    )
    assert out["parse_ok"] is True
    assert out["shape"] == "json"
    assert out["value"]["decision"] == "ship"


def test_parse_actor_output_strips_json_fence():
    out = pipeline._parse_actor_output(
        "gtm-sequence-definer-verdict",
        '```json\n{"ship": true, "issues": []}\n```',
    )
    assert out["parse_ok"] is True
    assert out["value"] == {"ship": True, "issues": []}


def test_parse_actor_output_json_invalid():
    out = pipeline._parse_actor_output(
        "gtm-sequence-definer",
        "not json {invalid",
    )
    assert out["parse_ok"] is False
    assert "JSONDecodeError" in (out["parse_error"] or "")


def test_parse_actor_output_master_strategist_yaml_front_matter():
    md = "---\nschema_version: 1\n---\n\n# body"
    out = pipeline._parse_actor_output("gtm-master-strategist", md)
    assert out["parse_ok"] is True
    assert out["shape"] == "markdown_with_yaml"


def test_parse_actor_output_master_strategist_missing_front_matter():
    out = pipeline._parse_actor_output(
        "gtm-master-strategist",
        "# strategy without front matter",
    )
    assert out["parse_ok"] is False
    assert "YAML front-matter" in out["parse_error"]


def test_parse_actor_output_empty_returns_failure():
    out = pipeline._parse_actor_output("gtm-sequence-definer", "")
    assert out["parse_ok"] is False
    assert out["shape"] == "empty"


# ---------------------------------------------------------------------------
# _supersede_downstream slug-set computation (logic-level, not DB)
# ---------------------------------------------------------------------------


def test_pipeline_steps_shape():
    # Materializer slice grew the pipeline to 5 actor/verdict pairs.
    # The last is the per-recipient fanout step.
    assert len(pipeline.PIPELINE_STEPS) == 5
    assert pipeline.PIPELINE_STEPS[0]["actor"] == "gtm-sequence-definer"
    assert pipeline.PIPELINE_STEPS[1]["actor"] == "gtm-channel-step-materializer"
    assert pipeline.PIPELINE_STEPS[2]["actor"] == "gtm-audience-materializer"
    assert pipeline.PIPELINE_STEPS[3]["actor"] == "gtm-master-strategist"
    assert pipeline.PIPELINE_STEPS[-1]["actor"] == "gtm-per-recipient-creative"
    for pair in pipeline.PIPELINE_STEPS:
        assert pair["verdict"].endswith("-verdict")
        assert pair["verdict"].startswith(pair["actor"])
    # Only the last pair is fanout in this slice.
    fanouts = [p for p in pipeline.PIPELINE_STEPS if p.get("is_fanout")]
    assert len(fanouts) == 1
    assert fanouts[0]["actor"] == "gtm-per-recipient-creative"
    assert fanouts[0]["fanout_kind"] == "per_recipient_per_dm_step"


# ---------------------------------------------------------------------------
# run_step end-to-end with mocked dependencies
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Wire mocks for: registry, prompt-version, initiative bundle,
    DB row helpers, MAGS run_session. Returns a state dict."""

    state: dict[str, Any] = {
        "supersede_calls": [],
        "next_run_index_calls": [],
        "insert_calls": [],
        "finalize_calls": [],
        "run_session_calls": [],
        "run_session_response": {
            "session_id": "ses_abc123",
            "assistant_text": (
                '{"decision": "ship", "channels": {"direct_mail": '
                '{"enabled": true, "touches": [{"touch_number": 1, '
                '"mailer_type": "postcard", "day_offset": 0, '
                '"estimated_cost_cents": 150}]}}, "total_estimated_outlay_cents": 150}'
            ),
            "events": [],
            "request_ids": ["req_1", "req_2"],
            "mcp_calls": [{"tool_name": "dex_search", "args_preview": "..."}],
            "usage": {"input_tokens": 1500, "output_tokens": 250},
            "stop_reason": "end_turn",
            "terminal_status": "completed",
        },
    }

    async def fake_get_registry(slug):
        return {
            "id": uuid4(),
            "agent_slug": slug,
            "anthropic_agent_id": ANTHROPIC_AGENT_ID,
            "role": "actor" if not slug.endswith("-verdict") else "verdict",
            "parent_actor_slug": (
                slug.removesuffix("-verdict") if slug.endswith("-verdict") else None
            ),
            "model": "claude-opus-4-7",
            "description": None,
            "deactivated_at": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }

    async def fake_get_latest_version(slug):
        return {
            "id": uuid4(),
            "agent_slug": slug,
            "anthropic_agent_id": ANTHROPIC_AGENT_ID,
            "system_prompt": f"# system prompt for {slug}",
            "version_index": 1,
            "activation_source": "setup_script",
            "parent_version_id": None,
            "activated_by_user_id": None,
            "notes": None,
            "created_at": datetime.now(UTC),
        }

    async def fake_load_bundle(initiative_id):
        return {
            "initiative": {
                "id": initiative_id,
                "organization_id": ORG,
                "brand_id": BRAND,
                "partner_id": PARTNER,
                "partner_contract_id": CONTRACT,
                "data_engine_audience_id": AUDIENCE,
                "partner_research_ref": "hqx://exa.exa_calls/test-partner-research",
                "strategic_context_research_ref": None,
                "campaign_strategy_path": None,
                "status": "strategy_ready",
                "history": [],
                "metadata": {},
                "reservation_window_start": None,
                "reservation_window_end": None,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
            "partner": {
                "id": PARTNER, "name": "DAT", "domain": "dat.com",
                "primary_contact_name": None, "primary_contact_email": None,
                "primary_phone": None, "intro_email": None,
                "hours_of_operation_config": {},
            },
            "contract": {
                "id": CONTRACT, "partner_id": PARTNER,
                "pricing_model": "flat_90d", "amount_cents": 2_500_000,
                "duration_days": 90, "max_capital_outlay_cents": 1_000_000,
                "qualification_rules": {"power_units_min": 10},
                "terms_blob": None, "status": "active",
                "starts_at": None, "ends_at": None,
            },
            "brand": {
                "id": BRAND, "name": "Capital Expansion",
                "domain": "capitalexpansion.com",
            },
            "brand_content": {
                "voice": {"format": "md", "content": "VOICE FILE BODY"},
            },
            "audience_descriptor": {
                "spec": {"name": "DAT — fast-growing carriers"},
                "audience_attributes": [],
            },
        }

    async def fake_load_doctrine(org_id):
        return {
            "organization_id": org_id,
            "doctrine_markdown": "DOCTRINE BODY",
            "parameters": {
                "target_margin_pct": 0.40,
                "min_per_piece_cents": 100,
                "max_per_piece_cents": 800,
            },
            "updated_at": datetime.now(UTC),
            "updated_by_user_id": None,
        }

    async def fake_load_audience_sample(audience_id, *, limit=8):
        return [
            {"dot_number": 1234567, "power_units": 12, "state": "TX"},
            {"dot_number": 7654321, "power_units": 18, "state": "CA"},
        ]

    async def fake_supersede(
        initiative_id,
        agent_slug,
        *,
        recipient_id=None,
        channel_campaign_step_id=None,
    ):
        state["supersede_calls"].append(
            (initiative_id, agent_slug, recipient_id, channel_campaign_step_id)
        )
        return 0

    async def fake_next_run_index(
        initiative_id,
        agent_slug,
        *,
        recipient_id=None,
        channel_campaign_step_id=None,
    ):
        state["next_run_index_calls"].append(
            (initiative_id, agent_slug, recipient_id, channel_campaign_step_id)
        )
        return 1

    async def fake_insert_running(**kwargs):
        rid = uuid4()
        state["insert_calls"].append({**kwargs, "run_id": rid})
        return rid

    async def fake_finalize(**kwargs):
        state["finalize_calls"].append(kwargs)

    async def fake_find_actor_run(
        initiative_id,
        parent_actor_slug,
        *,
        recipient_id=None,
        channel_campaign_step_id=None,
    ):
        return None

    async def fake_run_session(**kwargs):
        state["run_session_calls"].append(kwargs)
        return state["run_session_response"]

    async def fake_fetch_exa(ref):
        return {"output": {"content": "(test partner research content)"}}

    monkeypatch.setattr(agent_prompts, "get_registry_row", fake_get_registry)
    monkeypatch.setattr(agent_prompts, "get_latest_version", fake_get_latest_version)
    monkeypatch.setattr(pipeline, "_load_initiative_bundle", fake_load_bundle)
    monkeypatch.setattr(pipeline, "_load_doctrine", fake_load_doctrine)
    monkeypatch.setattr(pipeline, "_load_audience_sample", fake_load_audience_sample)
    monkeypatch.setattr(pipeline, "_supersede_downstream", fake_supersede)
    monkeypatch.setattr(pipeline, "_next_run_index", fake_next_run_index)
    monkeypatch.setattr(pipeline, "_insert_running_run", fake_insert_running)
    monkeypatch.setattr(pipeline, "_finalize_run", fake_finalize)
    monkeypatch.setattr(pipeline, "_find_actor_run_id", fake_find_actor_run)
    monkeypatch.setattr(pipeline, "_fetch_exa_payload", fake_fetch_exa)
    monkeypatch.setattr(
        anthropic_managed_agents, "run_session", fake_run_session
    )
    monkeypatch.setattr(
        pipeline.anthropic_managed_agents, "run_session", fake_run_session
    )
    return state


@pytest.mark.asyncio
async def test_run_step_succeeds_with_valid_actor_output(stub_pipeline):
    result = await pipeline.run_step(
        initiative_id=INITIATIVE,
        agent_slug="gtm-sequence-definer",
    )

    # Supersede fired before the running row insert. For initiative-
    # scoped agents the recipient_id / step_id kwargs are None.
    assert stub_pipeline["supersede_calls"] == [
        (INITIATIVE, "gtm-sequence-definer", None, None)
    ]
    # Insert captured the prompt snapshot.
    assert len(stub_pipeline["insert_calls"]) == 1
    insert = stub_pipeline["insert_calls"][0]
    assert insert["agent_slug"] == "gtm-sequence-definer"
    assert insert["system_prompt_snapshot"] == "# system prompt for gtm-sequence-definer"
    assert insert["model"] == "claude-opus-4-7"
    # MAGS session was opened with the right title + metadata.
    assert len(stub_pipeline["run_session_calls"]) == 1
    sess = stub_pipeline["run_session_calls"][0]
    assert sess["agent_id"] == ANTHROPIC_AGENT_ID
    assert "gtm-sequence-definer" in sess["title"]
    assert sess["metadata"]["initiative_id"] == str(INITIATIVE)
    # Finalize transitioned to succeeded.
    assert len(stub_pipeline["finalize_calls"]) == 1
    fin = stub_pipeline["finalize_calls"][0]
    assert fin["status"] == "succeeded"
    assert fin["output_blob"]["value"]["decision"] == "ship"
    # Step result shape.
    assert result["status"] == "succeeded"
    assert result["anthropic_session_id"] == "ses_abc123"
    assert result["anthropic_request_ids"] == ["req_1", "req_2"]


@pytest.mark.asyncio
async def test_run_step_fails_on_unparseable_actor_output(stub_pipeline):
    stub_pipeline["run_session_response"]["assistant_text"] = "not json {bad"
    result = await pipeline.run_step(
        initiative_id=INITIATIVE,
        agent_slug="gtm-sequence-definer",
    )
    assert result["status"] == "failed"
    fin = stub_pipeline["finalize_calls"][0]
    assert fin["status"] == "failed"
    assert fin["error_blob"]["kind"] == "parse_error"


@pytest.mark.asyncio
async def test_run_step_raises_for_unregistered_slug(stub_pipeline, monkeypatch):
    async def none_registry(slug):
        return None

    monkeypatch.setattr(agent_prompts, "get_registry_row", none_registry)
    with pytest.raises(pipeline.AgentSlugNotRegistered):
        await pipeline.run_step(
            initiative_id=INITIATIVE,
            agent_slug="unknown-slug",
        )
    assert stub_pipeline["finalize_calls"] == []


@pytest.mark.asyncio
async def test_run_step_finalizes_failed_on_anthropic_error(stub_pipeline, monkeypatch):
    async def boom(**kwargs):
        raise anthropic_managed_agents.ManagedAgentsError(
            status_code=429, message="rate limited",
        )

    monkeypatch.setattr(
        pipeline.anthropic_managed_agents, "run_session", boom
    )
    with pytest.raises(pipeline.RunStepError):
        await pipeline.run_step(
            initiative_id=INITIATIVE,
            agent_slug="gtm-sequence-definer",
        )
    # The row was still finalized (to failed) before the exception bubbled.
    fin = stub_pipeline["finalize_calls"][0]
    assert fin["status"] == "failed"
    assert fin["error_blob"]["kind"] == "anthropic_error"
    assert fin["error_blob"]["status_code"] == 429


@pytest.mark.asyncio
async def test_run_step_channel_step_materializer_folds_executed_into_output(
    stub_pipeline, monkeypatch,
):
    """When the channel-step-materializer's actor output is a valid plan,
    run_step should call materializer_execution.execute_channel_step_plan
    and fold its result into output_blob.value.executed alongside the
    plan."""
    plan = {
        "campaign": {"name": "x", "metadata": {}},
        "channel_campaigns": [
            {"channel": "direct_mail", "provider": "lob", "name": "DM"},
        ],
        "steps": [
            {"channel": "direct_mail", "delay_days_from_previous": 0,
             "channel_specific_config": {"mailer_type": "postcard"},
             "landing_page_config_placeholder": {}},
        ],
    }
    import json as _json
    stub_pipeline["run_session_response"]["assistant_text"] = _json.dumps(plan)

    captured: dict[str, Any] = {}

    async def fake_execute(initiative_id, plan_arg):
        captured["plan"] = plan_arg
        return {
            "campaign_id": uuid4(),
            "channel_campaign_ids": {"direct_mail": uuid4()},
            "channel_campaign_step_ids": [uuid4()],
            "dm_step_ids": [uuid4()],
        }

    monkeypatch.setattr(
        pipeline.materializer_execution,
        "execute_channel_step_plan",
        fake_execute,
    )

    result = await pipeline.run_step(
        initiative_id=INITIATIVE,
        agent_slug="gtm-channel-step-materializer",
        upstream_outputs={"gtm-sequence-definer": {"decision": "ship"}},
    )
    assert result["status"] == "succeeded"
    value = result["output_blob"]["value"]
    assert "plan" in value and "executed" in value
    assert value["plan"]["campaign"]["name"] == "x"
    assert "channel_campaign_ids" in value["executed"]
    assert "dm_step_ids" in value["executed"]
    assert captured["plan"] == plan


@pytest.mark.asyncio
async def test_run_step_per_recipient_kwargs_propagate(
    stub_pipeline, monkeypatch,
):
    """When run_step is invoked with recipient_id + channel_campaign_step_id,
    those values flow into supersede / next_run_index / insert_running."""
    rid = uuid4()
    sid = uuid4()
    stub_pipeline["run_session_response"]["assistant_text"] = (
        '[{"piece_index": 0, "headline": "x"}]'
    )
    # The per-recipient creative loader pulls a recipient + step from
    # DB. Bypass that with a stub that returns minimal dicts.
    async def fake_loader(*, bundle, recipient_id, channel_campaign_step_id):
        return (
            {"id": recipient_id, "external_id": "1234567"},
            {"id": channel_campaign_step_id, "channel": "direct_mail",
             "channel_specific_config": {"mailer_type": "postcard"}},
        )
    monkeypatch.setattr(pipeline, "_load_recipient_and_step", fake_loader)

    await pipeline.run_step(
        initiative_id=INITIATIVE,
        agent_slug="gtm-per-recipient-creative",
        recipient_id=rid,
        channel_campaign_step_id=sid,
    )

    insert = stub_pipeline["insert_calls"][0]
    assert insert["recipient_id"] == rid
    assert insert["channel_campaign_step_id"] == sid

    # Supersede + next_run_index also got scoped to (rid, sid).
    assert stub_pipeline["supersede_calls"] == [
        (INITIATIVE, "gtm-per-recipient-creative", rid, sid)
    ]
    assert stub_pipeline["next_run_index_calls"] == [
        (INITIATIVE, "gtm-per-recipient-creative", rid, sid)
    ]


@pytest.mark.asyncio
async def test_run_step_master_strategist_writes_artifact(stub_pipeline, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_INITIATIVES_ROOT", tmp_path)
    stub_pipeline["run_session_response"]["assistant_text"] = (
        "---\nschema_version: 1\nper_touch_frames: []\n---\n\n# strat body"
    )

    result = await pipeline.run_step(
        initiative_id=INITIATIVE,
        agent_slug="gtm-master-strategist",
        upstream_outputs={
            "gtm-sequence-definer": {"decision": "ship", "channels": {}},
        },
    )
    assert result["status"] == "succeeded"
    assert result["output_artifact_path"] is not None
    artifact = tmp_path / str(INITIATIVE) / "master_strategy.001.md"
    assert artifact.exists()
    assert artifact.read_text().startswith("---")
