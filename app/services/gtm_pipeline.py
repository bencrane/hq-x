"""Post-payment GTM pipeline orchestration: hq-x's seam between
Trigger.dev and the Anthropic Managed Agents API.

Trigger.dev sequences subagents and POSTs each step to
``/internal/gtm/initiatives/{id}/run-step``. Inside that single request
this service:

  1. Resolves agent_slug → registry row (anthropic_agent_id, role, model).
  2. Resolves the agent's currently-active system prompt from
     business.agent_prompt_versions (latest row).
  3. Marks downstream gtm_subagent_runs as ``superseded``.
  4. Inserts a new gtm_subagent_runs row in ``running`` state with the
     prompt snapshot + input blob captured.
  5. Opens a MAGS session, posts the user message, awaits the agent's
     terminal turn.
  6. Parses the assistant text per the agent's output contract.
  7. Updates the run row to ``succeeded`` / ``failed`` with output blob,
     mcp_calls trace, request ids, cost.
  8. Returns a StepResult dict the Trigger workflow consumes.

Everything else (kickoff_pipeline, request_rerun, status setters)
threads through this central run_step.

For v0, intermediate inputs from non-built upstream subagents are
inlined directly: #7 reads partner_research + audience_descriptor +
brand_content directly; #11 reads #7's output + brand_content + ONE
sample recipient directly. The per-recipient-creative actor + verdict
run ONCE per pipeline against the first sample recipient — at scale
they run per-recipient, but the foundation slice only needs to prove
the seam.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.config import settings
from app.db import get_db_connection
from app.services import activation_jobs, agent_prompts, anthropic_managed_agents
from app.services import dex_client, org_doctrine
from app.services import gtm_initiatives as gtm_svc
from app.services import materializer_execution

logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BRANDS_ROOT = _REPO_ROOT / "data" / "brands"
_INDEPENDENT_BRAND_DOCTRINE_PATH = (
    _BRANDS_ROOT / "_meta" / "independent-brand-doctrine.md"
)
_INITIATIVES_ROOT = _REPO_ROOT / "data" / "initiatives"

_TRIGGER_TASK_ID = "gtm.run-initiative-pipeline"


# Per-Lob-piece cost table — operator priors. Used as the per_piece_cost_table
# input to gtm-sequence-definer. These are sane v0 defaults; the operator
# can later move them into business.org_doctrine.parameters per-org.
_PER_PIECE_COST_TABLE: dict[str, int] = {
    "postcard": 150,
    "letter": 300,
    "self_mailer": 250,
    "snap_pack": 400,
    "booklet": 700,
}


# Pipeline ordering — must match src/trigger/gtm-run-initiative-pipeline.ts.
#
# Each entry is a dict so callers (Trigger.dev TS, e2e harness, supersede
# walker) can read either the slug pair or the fanout flag without
# importing a Python type. ``fanout_kind`` is set on the per-recipient
# steps; the orchestrator reads it to switch between the parent-task
# loop and the per-recipient child-task batchTrigger path.
PIPELINE_STEPS: list[dict[str, Any]] = [
    {
        "actor": "gtm-sequence-definer",
        "verdict": "gtm-sequence-definer-verdict",
        "is_fanout": False,
        "fanout_kind": None,
    },
    {
        "actor": "gtm-channel-step-materializer",
        "verdict": "gtm-channel-step-materializer-verdict",
        "is_fanout": False,
        "fanout_kind": None,
    },
    {
        "actor": "gtm-audience-materializer",
        "verdict": "gtm-audience-materializer-verdict",
        "is_fanout": False,
        "fanout_kind": None,
    },
    {
        "actor": "gtm-master-strategist",
        "verdict": "gtm-master-strategist-verdict",
        "is_fanout": False,
        "fanout_kind": None,
    },
    {
        "actor": "gtm-per-recipient-creative",
        "verdict": "gtm-per-recipient-creative-verdict",
        "is_fanout": True,
        "fanout_kind": "per_recipient_per_dm_step",
    },
]


# The set of agent slugs whose run row's output_blob carries a
# ``{plan, executed}`` shape (actor's plan plus the hq-x execution
# result). Read by `_extract_upstream` to surface the right object
# to downstream agents.
_MATERIALIZER_ACTOR_SLUGS = {
    "gtm-channel-step-materializer",
    "gtm-audience-materializer",
}


class GtmPipelineError(Exception):
    pass


class RunStepError(GtmPipelineError):
    pass


class AgentSlugNotRegistered(GtmPipelineError):
    pass


# ---------------------------------------------------------------------------
# Input loaders (dispatch by agent_slug)
# ---------------------------------------------------------------------------


async def _load_initiative_bundle(initiative_id: UUID) -> dict[str, Any]:
    """Load the inputs every subagent needs (initiative + partner +
    contract + brand + audience_descriptor) in one shot."""
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise RunStepError(f"initiative {initiative_id} not found")

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, domain, primary_contact_name,
                       primary_contact_email, primary_phone, intro_email,
                       hours_of_operation_config
                FROM business.demand_side_partners
                WHERE id = %s
                """,
                (str(initiative["partner_id"]),),
            )
            partner_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT id, partner_id, pricing_model, amount_cents,
                       duration_days, max_capital_outlay_cents,
                       qualification_rules, terms_blob, status,
                       starts_at, ends_at
                FROM business.partner_contracts
                WHERE id = %s
                """,
                (str(initiative["partner_contract_id"]),),
            )
            contract_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT id, name, domain
                FROM business.brands
                WHERE id = %s
                """,
                (str(initiative["brand_id"]),),
            )
            brand_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT content_key, content_format, content
                FROM business.brand_content
                WHERE brand_id = %s
                ORDER BY content_key
                """,
                (str(initiative["brand_id"]),),
            )
            brand_content_rows = await cur.fetchall()

    if not (partner_row and contract_row and brand_row):
        raise RunStepError(
            "initiative is missing partner/contract/brand FK targets"
        )

    partner = {
        "id": partner_row[0],
        "name": partner_row[1],
        "domain": partner_row[2],
        "primary_contact_name": partner_row[3],
        "primary_contact_email": partner_row[4],
        "primary_phone": partner_row[5],
        "intro_email": partner_row[6],
        "hours_of_operation_config": partner_row[7] or {},
    }
    contract = {
        "id": contract_row[0],
        "partner_id": contract_row[1],
        "pricing_model": contract_row[2],
        "amount_cents": contract_row[3],
        "duration_days": contract_row[4],
        "max_capital_outlay_cents": contract_row[5],
        "qualification_rules": contract_row[6] or {},
        "terms_blob": contract_row[7],
        "status": contract_row[8],
        "starts_at": contract_row[9],
        "ends_at": contract_row[10],
    }
    brand = {
        "id": brand_row[0],
        "name": brand_row[1],
        "domain": brand_row[2],
    }
    brand_content = {
        row[0]: {"format": row[1], "content": row[2]} for row in brand_content_rows
    }

    # Audience descriptor — best-effort.
    descriptor: dict[str, Any] | None = None
    try:
        descriptor = await dex_client.get_audience_descriptor(
            initiative["data_engine_audience_id"]
        )
    except Exception as exc:  # pragma: no cover — DEX call is network
        logger.warning(
            "dex.get_audience_descriptor failed initiative=%s err=%r",
            initiative_id, exc,
        )

    return {
        "initiative": initiative,
        "partner": partner,
        "contract": contract,
        "brand": brand,
        "brand_content": brand_content,
        "audience_descriptor": descriptor,
    }


async def _load_audience_sample(
    audience_id: UUID, *, limit: int = 8
) -> list[dict[str, Any]]:
    try:
        page = await dex_client.list_audience_members(
            audience_id, limit=limit, offset=0
        )
    except Exception as exc:  # pragma: no cover — DEX call is network
        logger.warning(
            "dex.list_audience_members failed audience=%s err=%r",
            audience_id, exc,
        )
        return []
    items = (page or {}).get("items") if isinstance(page, dict) else None
    return list(items) if items else []


async def _load_doctrine(organization_id: UUID) -> dict[str, Any] | None:
    return await org_doctrine.get_for_org(organization_id)


async def _load_recipient_and_step(
    *,
    bundle: dict[str, Any],
    recipient_id: UUID | None,
    channel_campaign_step_id: UUID | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Per-recipient input loader. When ``recipient_id`` and
    ``channel_campaign_step_id`` are supplied (the fanout path), load
    those concrete rows. When neither is supplied (orchestrator fallback
    or test harness), fall back to a single DEX sample member + a
    placeholder step description.
    """
    if recipient_id is None or channel_campaign_step_id is None:
        sample = await _load_audience_sample(
            bundle["initiative"]["data_engine_audience_id"], limit=1,
        )
        recipient = sample[0] if sample else {}
        return recipient, {
            "id": None,
            "channel": "direct_mail",
            "channel_specific_config": {"mailer_type": "postcard"},
            "step_order": 1,
            "name": "(no specific step — sample run)",
        }

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, organization_id, recipient_type, external_source,
                       external_id, display_name, mailing_address, phone,
                       email, metadata
                FROM business.recipients
                WHERE id = %s
                """,
                (str(recipient_id),),
            )
            r_row = await cur.fetchone()
            await cur.execute(
                """
                SELECT s.id, s.channel_campaign_id, s.campaign_id,
                       s.step_order, s.name, s.delay_days_from_previous,
                       s.channel_specific_config, s.metadata,
                       cc.channel
                FROM business.channel_campaign_steps s
                JOIN business.channel_campaigns cc
                    ON cc.id = s.channel_campaign_id
                WHERE s.id = %s
                """,
                (str(channel_campaign_step_id),),
            )
            s_row = await cur.fetchone()

    if r_row is None:
        raise RunStepError(
            f"recipient {recipient_id} not found for per-recipient input"
        )
    if s_row is None:
        raise RunStepError(
            f"channel_campaign_step {channel_campaign_step_id} not found"
        )

    recipient = {
        "id": r_row[0],
        "organization_id": r_row[1],
        "recipient_type": r_row[2],
        "external_source": r_row[3],
        "external_id": r_row[4],
        "display_name": r_row[5],
        "mailing_address": r_row[6] or {},
        "phone": r_row[7],
        "email": r_row[8],
        "dex_row": r_row[9] or {},
    }
    step = {
        "id": s_row[0],
        "channel_campaign_id": s_row[1],
        "campaign_id": s_row[2],
        "step_order": s_row[3],
        "name": s_row[4],
        "delay_days_from_previous": s_row[5],
        "channel_specific_config": s_row[6] or {},
        "metadata": s_row[7] or {},
        "channel": s_row[8],
    }
    return recipient, step


def _load_independent_brand_doctrine() -> str:
    if _INDEPENDENT_BRAND_DOCTRINE_PATH.is_file():
        return _INDEPENDENT_BRAND_DOCTRINE_PATH.read_text()
    return "(independent-brand doctrine missing on disk)"


async def _fetch_exa_payload(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    try:
        scheme, rest = ref.split("://", 1)
    except ValueError:
        return None
    if scheme != "hqx":
        return None
    try:
        _, exa_call_id = rest.rsplit("/", 1)
    except ValueError:
        return None
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT response_payload
                FROM exa.exa_calls
                WHERE id = %s
                """,
                (exa_call_id,),
            )
            row = await cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


def _exa_text(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "(payload missing)"
    output = payload.get("output") if isinstance(payload, dict) else None
    content = ""
    if isinstance(output, dict):
        content = output.get("content") or ""
    return content or "(no content field)"


def _format_brand_content(brand_content: dict[str, dict[str, str]]) -> str:
    if not brand_content:
        return "(no brand_content rows)"
    parts: list[str] = []
    for key, blob in brand_content.items():
        parts.append(f"## brand_content: {key} ({blob.get('format')})")
        parts.append(blob.get("content") or "")
        parts.append("")
    return "\n".join(parts)


def _format_doctrine_block(doctrine: dict[str, Any] | None) -> tuple[str, str]:
    """Returns (markdown_block, parameters_json_block)."""
    if not doctrine:
        return ("(operator doctrine missing — pipeline will fail)", "{}")
    return (
        doctrine.get("doctrine_markdown") or "",
        json.dumps(doctrine.get("parameters") or {}, indent=2),
    )


# ---------------------------------------------------------------------------
# Per-agent input assemblers
# ---------------------------------------------------------------------------


async def _assemble_input(
    *,
    agent_slug: str,
    initiative_id: UUID,
    bundle: dict[str, Any],
    upstream_outputs: dict[str, Any] | None,
    hint: str | None,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> tuple[str, dict[str, Any]]:
    """Returns (user_message_text, input_blob_for_run_capture)."""

    organization_id = bundle["initiative"]["organization_id"]
    doctrine = await _load_doctrine(organization_id)

    if agent_slug == "gtm-sequence-definer":
        return _assemble_sequence_definer(
            bundle=bundle,
            doctrine=doctrine,
            hint=hint,
        )

    if agent_slug == "gtm-sequence-definer-verdict":
        return _assemble_sequence_definer_verdict(
            bundle=bundle,
            doctrine=doctrine,
            upstream_outputs=upstream_outputs,
        )

    if agent_slug == "gtm-channel-step-materializer":
        return _assemble_channel_step_materializer(
            bundle=bundle,
            doctrine=doctrine,
            upstream_outputs=upstream_outputs,
            hint=hint,
        )

    if agent_slug == "gtm-channel-step-materializer-verdict":
        return _assemble_channel_step_materializer_verdict(
            bundle=bundle,
            doctrine=doctrine,
            upstream_outputs=upstream_outputs,
        )

    if agent_slug == "gtm-audience-materializer":
        return _assemble_audience_materializer(
            bundle=bundle,
            doctrine=doctrine,
            upstream_outputs=upstream_outputs,
            hint=hint,
        )

    if agent_slug == "gtm-audience-materializer-verdict":
        return _assemble_audience_materializer_verdict(
            bundle=bundle,
            doctrine=doctrine,
            upstream_outputs=upstream_outputs,
        )

    if agent_slug == "gtm-master-strategist":
        sample = await _load_audience_sample(
            bundle["initiative"]["data_engine_audience_id"]
        )
        partner_research = await _fetch_exa_payload(
            bundle["initiative"].get("partner_research_ref")
        )
        return _assemble_master_strategist(
            initiative_id=initiative_id,
            bundle=bundle,
            sample_members=sample,
            partner_research=partner_research,
            upstream_outputs=upstream_outputs,
            hint=hint,
        )

    if agent_slug == "gtm-master-strategist-verdict":
        sample = await _load_audience_sample(
            bundle["initiative"]["data_engine_audience_id"]
        )
        partner_research = await _fetch_exa_payload(
            bundle["initiative"].get("partner_research_ref")
        )
        return _assemble_master_strategist_verdict(
            initiative_id=initiative_id,
            bundle=bundle,
            sample_members=sample,
            partner_research=partner_research,
            upstream_outputs=upstream_outputs,
        )

    if agent_slug == "gtm-per-recipient-creative":
        recipient, step = await _load_recipient_and_step(
            bundle=bundle,
            recipient_id=recipient_id,
            channel_campaign_step_id=channel_campaign_step_id,
        )
        return _assemble_per_recipient_creative(
            bundle=bundle,
            recipient=recipient,
            step=step,
            upstream_outputs=upstream_outputs,
            hint=hint,
        )

    if agent_slug == "gtm-per-recipient-creative-verdict":
        recipient, step = await _load_recipient_and_step(
            bundle=bundle,
            recipient_id=recipient_id,
            channel_campaign_step_id=channel_campaign_step_id,
        )
        return _assemble_per_recipient_creative_verdict(
            bundle=bundle,
            recipient=recipient,
            step=step,
            upstream_outputs=upstream_outputs,
        )

    raise RunStepError(f"no input assembler for agent_slug={agent_slug!r}")


def _assemble_sequence_definer(
    *, bundle: dict[str, Any], doctrine: dict[str, Any] | None, hint: str | None,
) -> tuple[str, dict[str, Any]]:
    md, params_json = _format_doctrine_block(doctrine)
    text = (
        f"<partner_contract>\n{json.dumps(bundle['contract'], default=str, indent=2)}\n</partner_contract>\n\n"
        f"<audience_descriptor>\n{json.dumps(bundle['audience_descriptor'], default=str, indent=2)}\n</audience_descriptor>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<doctrine_parameters>\n{params_json}\n</doctrine_parameters>\n\n"
        f"<per_piece_cost_table>\n{json.dumps(_PER_PIECE_COST_TABLE, indent=2)}\n</per_piece_cost_table>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the JSON sequence per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-sequence-definer",
        "doctrine_markdown_chars": len(md),
        "has_audience_descriptor": bundle["audience_descriptor"] is not None,
        "hint": hint,
    }
    return text, blob


def _assemble_sequence_definer_verdict(
    *,
    bundle: dict[str, Any],
    doctrine: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    md, params_json = _format_doctrine_block(doctrine)
    actor_output = _extract_upstream(
        upstream_outputs, "gtm-sequence-definer"
    )
    text = (
        f"<partner_contract>\n{json.dumps(bundle['contract'], default=str, indent=2)}\n</partner_contract>\n\n"
        f"<audience_descriptor>\n{json.dumps(bundle['audience_descriptor'], default=str, indent=2)}\n</audience_descriptor>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<doctrine_parameters>\n{params_json}\n</doctrine_parameters>\n\n"
        f"<per_piece_cost_table>\n{json.dumps(_PER_PIECE_COST_TABLE, indent=2)}\n</per_piece_cost_table>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-sequence-definer-verdict",
        "doctrine_markdown_chars": len(md),
    }
    return text, blob


def _assemble_master_strategist(
    *,
    initiative_id: UUID,
    bundle: dict[str, Any],
    sample_members: list[dict[str, Any]],
    partner_research: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
    hint: str | None,
) -> tuple[str, dict[str, Any]]:
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    text = (
        f"<initiative_id>{initiative_id}</initiative_id>\n\n"
        f"<generated_at>{datetime.now(UTC).isoformat()}</generated_at>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<audience_descriptor>\n{json.dumps(bundle['audience_descriptor'], default=str, indent=2)}\n</audience_descriptor>\n\n"
        f"<audience_sample_members>\n{json.dumps(sample_members, default=str, indent=2)}\n</audience_sample_members>\n\n"
        f"<partner_research>\n{_exa_text(partner_research)}\n</partner_research>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the Master Strategy markdown per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-master-strategist",
        "sample_member_count": len(sample_members),
        "has_partner_research": partner_research is not None,
        "brand_content_keys": sorted(bundle["brand_content"].keys()),
        "hint": hint,
    }
    return text, blob


def _assemble_master_strategist_verdict(
    *,
    initiative_id: UUID,
    bundle: dict[str, Any],
    sample_members: list[dict[str, Any]],
    partner_research: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    actor_output = _extract_upstream(
        upstream_outputs, "gtm-master-strategist"
    )
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    text = (
        f"<initiative_id>{initiative_id}</initiative_id>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<audience_descriptor>\n{json.dumps(bundle['audience_descriptor'], default=str, indent=2)}\n</audience_descriptor>\n\n"
        f"<audience_sample_members>\n{json.dumps(sample_members, default=str, indent=2)}\n</audience_sample_members>\n\n"
        f"<partner_research>\n{_exa_text(partner_research)}\n</partner_research>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-master-strategist-verdict",
        "sample_member_count": len(sample_members),
    }
    return text, blob


def _assemble_channel_step_materializer(
    *,
    bundle: dict[str, Any],
    doctrine: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
    hint: str | None,
) -> tuple[str, dict[str, Any]]:
    md, _ = _format_doctrine_block(doctrine)
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    text = (
        f"<initiative_id>{bundle['initiative']['id']}</initiative_id>\n\n"
        f"<brand>\n{json.dumps(bundle['brand'], default=str, indent=2)}\n</brand>\n\n"
        f"<partner_contract>\n{json.dumps(bundle['contract'], default=str, indent=2)}\n</partner_contract>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the channel-step materialization plan JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-channel-step-materializer",
        "doctrine_markdown_chars": len(md),
        "hint": hint,
    }
    return text, blob


def _assemble_channel_step_materializer_verdict(
    *,
    bundle: dict[str, Any],
    doctrine: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    md, _ = _format_doctrine_block(doctrine)
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    actor_output = _extract_upstream(
        upstream_outputs, "gtm-channel-step-materializer"
    )
    text = (
        f"<initiative_id>{bundle['initiative']['id']}</initiative_id>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-channel-step-materializer-verdict",
    }
    return text, blob


def _assemble_audience_materializer(
    *,
    bundle: dict[str, Any],
    doctrine: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
    hint: str | None,
) -> tuple[str, dict[str, Any]]:
    md, params_json = _format_doctrine_block(doctrine)
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    channel_step_executed = _extract_executed(
        upstream_outputs, "gtm-channel-step-materializer"
    )
    text = (
        f"<initiative_id>{bundle['initiative']['id']}</initiative_id>\n\n"
        f"<data_engine_audience_id>{bundle['initiative']['data_engine_audience_id']}</data_engine_audience_id>\n\n"
        f"<audience_descriptor>\n{json.dumps(bundle['audience_descriptor'], default=str, indent=2)}\n</audience_descriptor>\n\n"
        f"<partner_contract>\n{json.dumps(bundle['contract'], default=str, indent=2)}\n</partner_contract>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<channel_step_executed>\n{json.dumps(channel_step_executed, default=str, indent=2)}\n</channel_step_executed>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<doctrine_parameters>\n{params_json}\n</doctrine_parameters>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the audience materialization plan JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-audience-materializer",
        "hint": hint,
    }
    return text, blob


def _assemble_audience_materializer_verdict(
    *,
    bundle: dict[str, Any],
    doctrine: dict[str, Any] | None,
    upstream_outputs: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    md, params_json = _format_doctrine_block(doctrine)
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    actor_output = _extract_upstream(
        upstream_outputs, "gtm-audience-materializer"
    )
    text = (
        f"<initiative_id>{bundle['initiative']['id']}</initiative_id>\n\n"
        f"<partner_contract>\n{json.dumps(bundle['contract'], default=str, indent=2)}\n</partner_contract>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<doctrine_markdown>\n{md}\n</doctrine_markdown>\n\n"
        f"<doctrine_parameters>\n{params_json}\n</doctrine_parameters>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-audience-materializer-verdict",
    }
    return text, blob


def _assemble_per_recipient_creative(
    *,
    bundle: dict[str, Any],
    recipient: dict[str, Any],
    step: dict[str, Any],
    upstream_outputs: dict[str, Any] | None,
    hint: str | None,
) -> tuple[str, dict[str, Any]]:
    master_strategy = _extract_upstream(
        upstream_outputs, "gtm-master-strategist"
    )
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    # Lightweight zone catalog stub for v0 — the real one would come from
    # app.dmaas.service.bind_spec_zones per piece type. Keeping it inline
    # so the agent has SOMETHING structured to reason against; the
    # production wiring lands in a follow-up directive that integrates
    # with dmaas zone binding.
    spec_zone_catalog = {
        "postcard": {
            "front": ["headline", "subhead", "footer", "background"],
            "back": ["headline", "body", "cta", "url", "address_block"],
        },
        "letter": ["salutation", "body", "closing", "signature", "cta"],
        "self_mailer": {
            "outside": ["headline", "subhead", "footer"],
            "inside": ["headline", "body", "cta"],
        },
        "snap_pack": {
            "outside": ["headline", "subhead", "footer"],
            "inside": ["headline", "body", "cta"],
        },
        "booklet": {
            "cover": ["headline", "subhead"],
            "spreads": ["headline", "body"],
            "back_cover": ["cta", "proof_block"],
        },
    }
    text = (
        f"<master_strategy>\n{master_strategy}\n</master_strategy>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<step>\n{json.dumps(step, default=str, indent=2)}\n</step>\n\n"
        f"<recipient>\n{json.dumps(recipient, default=str, indent=2)}\n</recipient>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        f"<spec_zone_catalog>\n{json.dumps(spec_zone_catalog, indent=2)}\n</spec_zone_catalog>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the per-piece JSON array per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-per-recipient-creative",
        "recipient_id": str(recipient.get("id")) if recipient.get("id") else None,
        "channel_campaign_step_id": str(step.get("id")) if step.get("id") else None,
        "step_mailer_type": (step.get("channel_specific_config") or {}).get("mailer_type"),
        "hint": hint,
    }
    return text, blob


def _assemble_per_recipient_creative_verdict(
    *,
    bundle: dict[str, Any],
    recipient: dict[str, Any],
    step: dict[str, Any],
    upstream_outputs: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    master_strategy = _extract_upstream(
        upstream_outputs, "gtm-master-strategist"
    )
    sequence = _extract_upstream(upstream_outputs, "gtm-sequence-definer")
    actor_output = _extract_upstream(
        upstream_outputs, "gtm-per-recipient-creative"
    )
    spec_zone_catalog = {
        "postcard": {
            "front": ["headline", "subhead", "footer", "background"],
            "back": ["headline", "body", "cta", "url", "address_block"],
        },
        "letter": ["salutation", "body", "closing", "signature", "cta"],
        "self_mailer": {
            "outside": ["headline", "subhead", "footer"],
            "inside": ["headline", "body", "cta"],
        },
        "snap_pack": {
            "outside": ["headline", "subhead", "footer"],
            "inside": ["headline", "body", "cta"],
        },
        "booklet": {
            "cover": ["headline", "subhead"],
            "spreads": ["headline", "body"],
            "back_cover": ["cta", "proof_block"],
        },
    }
    text = (
        f"<master_strategy>\n{master_strategy}\n</master_strategy>\n\n"
        f"<sequence>\n{sequence}\n</sequence>\n\n"
        f"<step>\n{json.dumps(step, default=str, indent=2)}\n</step>\n\n"
        f"<recipient>\n{json.dumps(recipient, default=str, indent=2)}\n</recipient>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        f"<spec_zone_catalog>\n{json.dumps(spec_zone_catalog, indent=2)}\n</spec_zone_catalog>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-per-recipient-creative-verdict",
        "recipient_id": str(recipient.get("id")) if recipient.get("id") else None,
        "channel_campaign_step_id": str(step.get("id")) if step.get("id") else None,
    }
    return text, blob


def _extract_executed(
    upstream: dict[str, Any] | None, slug: str,
) -> dict[str, Any] | None:
    """Pull the ``executed`` sub-blob produced by materializer agents.

    For materializer slugs, ``run_step`` writes the actor's plan AND
    the hq-x execution result into ``output_blob.value`` as
    ``{plan, executed}``. The orchestrator forwards
    ``output_blob.value`` into ``upstream_outputs[slug]``, so
    ``upstream_outputs[slug]['executed']`` is the materialized id
    triple. Downstream agents (audience materializer, per-recipient
    creative) need the executed result, not the actor's plan, when
    reasoning about the materialized step ids.
    """
    if not upstream:
        return None
    raw = upstream.get(slug)
    if isinstance(raw, dict):
        executed = raw.get("executed")
        if isinstance(executed, dict):
            return executed
    return None


def _extract_upstream(
    upstream: dict[str, Any] | None, slug: str,
) -> str:
    if not upstream:
        return f"(upstream output for {slug} not provided)"
    raw = upstream.get(slug)
    if raw is None:
        return f"(upstream output for {slug} missing)"
    if isinstance(raw, str):
        return raw
    return json.dumps(raw, default=str, indent=2)


# ---------------------------------------------------------------------------
# Output parsing per agent slug
# ---------------------------------------------------------------------------


def _parse_actor_output(agent_slug: str, text: str) -> dict[str, Any]:
    """Per-agent parser. Actors emit either JSON (sequence-definer,
    per-recipient-creative) or markdown with YAML front-matter
    (master-strategist). Verdicts always emit JSON.

    Returns ``{shape: 'json'|'markdown', value: <parsed>, raw: text,
    parse_ok: bool, parse_error: str|None}``.

    On parse failure the caller still gets a structured row — output_blob
    contains the raw text + parse_error so the operator can debug from
    the UI.
    """
    text = (text or "").strip()
    if not text:
        return {
            "shape": "empty",
            "value": None,
            "raw": "",
            "parse_ok": False,
            "parse_error": "empty assistant text",
        }
    if agent_slug == "gtm-master-strategist":
        # Expect markdown with YAML front-matter beginning `---`.
        if text.startswith("---"):
            return {
                "shape": "markdown_with_yaml",
                "value": text,
                "raw": text,
                "parse_ok": True,
                "parse_error": None,
            }
        return {
            "shape": "markdown_with_yaml",
            "value": text,
            "raw": text,
            "parse_ok": False,
            "parse_error": "expected '---' YAML front-matter at start",
        }
    # Everyone else — JSON.
    cleaned = _strip_json_fence(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return {
            "shape": "json",
            "value": None,
            "raw": text,
            "parse_ok": False,
            "parse_error": f"JSONDecodeError: {exc.msg} at line {exc.lineno}",
        }
    return {
        "shape": "json",
        "value": parsed,
        "raw": text,
        "parse_ok": True,
        "parse_error": None,
    }


_FENCE_PREFIXES = ("```json\n", "```JSON\n", "```\n")


def _strip_json_fence(text: str) -> str:
    s = text.strip()
    for prefix in _FENCE_PREFIXES:
        if s.startswith(prefix) and s.endswith("```"):
            return s[len(prefix) : -len("```")].strip()
    # If the agent emitted prose preamble before the JSON, find the first
    # `{` or `[` and try to parse from there. Brace-balanced extraction so
    # we can capture trailing prose as well (rare, but possible). Fall
    # back to the original string if no balanced object/array is found —
    # callers will surface a parse error against the unstripped text.
    first_brace = -1
    for i, ch in enumerate(s):
        if ch in ("{", "["):
            first_brace = i
            break
    if first_brace < 0:
        return s
    open_ch = s[first_brace]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    escape = False
    for j in range(first_brace, len(s)):
        ch = s[j]
        if escape:
            escape = False
            continue
        if in_str:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[first_brace : j + 1]
    # unbalanced — return from first brace anyway so JSONDecodeError
    # surfaces against the actual JSON-shaped substring rather than the
    # preamble.
    return s[first_brace:]


# ---------------------------------------------------------------------------
# gtm_subagent_runs row helpers
# ---------------------------------------------------------------------------


async def _next_run_index(
    initiative_id: UUID,
    agent_slug: str,
    *,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> int:
    """Compute the next run_index, scoped by the fanout dimensions when set.

    For per-recipient agents the count is per (initiative, agent_slug,
    recipient, step) tuple — re-running the per-recipient creative for
    one specific recipient bumps that recipient's run_index without
    affecting any other recipient's run history.
    """
    where = ["initiative_id = %s", "agent_slug = %s"]
    args: list[Any] = [str(initiative_id), agent_slug]
    if recipient_id is not None:
        where.append("recipient_id = %s")
        args.append(str(recipient_id))
    else:
        where.append("recipient_id IS NULL")
    if channel_campaign_step_id is not None:
        where.append("channel_campaign_step_id = %s")
        args.append(str(channel_campaign_step_id))
    else:
        where.append("channel_campaign_step_id IS NULL")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT COALESCE(MAX(run_index), 0) + 1
                FROM business.gtm_subagent_runs
                WHERE {' AND '.join(where)}
                """,
                args,
            )
            row = await cur.fetchone()
    return int(row[0])


async def _supersede_downstream(
    initiative_id: UUID,
    agent_slug: str,
    *,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> int:
    """Mark every gtm_subagent_runs row for the initiative whose agent_slug
    is downstream of `agent_slug` (or is `agent_slug` itself, in any
    succeeded/running/queued state) as 'superseded'.

    Downstream is defined by PIPELINE_STEPS order. agent_slug's pair
    (actor + verdict) and everything after counts as 'this and downstream'.

    When ``recipient_id`` and ``channel_campaign_step_id`` are supplied
    (per-recipient rerun), only rows scoped to that (recipient, step)
    tuple are superseded — rerunning one recipient's creative does not
    invalidate other recipients' runs. Without those kwargs the function
    treats the rerun as upstream-of-fanout and supersedes every row
    whose slug is downstream regardless of fanout dimensions.
    """
    slugs: set[str] = set()
    found = False
    for pair in PIPELINE_STEPS:
        if pair["actor"] == agent_slug or pair["verdict"] == agent_slug:
            found = True
        if found:
            slugs.add(pair["actor"])
            slugs.add(pair["verdict"])
    if not slugs:
        return 0

    where = [
        "initiative_id = %s",
        "agent_slug = ANY(%s)",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    ]
    args: list[Any] = [str(initiative_id), list(slugs)]
    if recipient_id is not None:
        where.append("recipient_id = %s")
        args.append(str(recipient_id))
    if channel_campaign_step_id is not None:
        where.append("channel_campaign_step_id = %s")
        args.append(str(channel_campaign_step_id))

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE business.gtm_subagent_runs
                SET status = 'superseded',
                    completed_at = COALESCE(completed_at, NOW())
                WHERE {' AND '.join(where)}
                RETURNING id
                """,
                args,
            )
            rows = await cur.fetchall()
        await conn.commit()
    return len(rows)


async def _insert_running_run(
    *,
    initiative_id: UUID,
    agent_slug: str,
    run_index: int,
    parent_run_id: UUID | None,
    input_blob: dict[str, Any],
    system_prompt_snapshot: str,
    prompt_version_id: UUID | None,
    anthropic_agent_id: str,
    model: str,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.gtm_subagent_runs (
                    initiative_id, agent_slug, run_index, parent_run_id,
                    status, input_blob, system_prompt_snapshot,
                    prompt_version_id, anthropic_agent_id, model,
                    recipient_id, channel_campaign_step_id
                )
                VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(initiative_id), agent_slug, run_index,
                    str(parent_run_id) if parent_run_id else None,
                    Jsonb(input_blob),
                    system_prompt_snapshot,
                    str(prompt_version_id) if prompt_version_id else None,
                    anthropic_agent_id, model,
                    str(recipient_id) if recipient_id else None,
                    str(channel_campaign_step_id) if channel_campaign_step_id else None,
                ),
            )
            row = await cur.fetchone()
        await conn.commit()
    return row[0]


async def _finalize_run(
    *,
    run_id: UUID,
    status: str,
    output_blob: dict[str, Any] | None,
    output_artifact_path: str | None,
    anthropic_session_id: str | None,
    anthropic_request_ids: list[str] | None,
    mcp_calls: list[dict[str, Any]] | None,
    cost_cents: int | None,
    error_blob: dict[str, Any] | None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_subagent_runs
                SET status = %s,
                    output_blob = %s,
                    output_artifact_path = %s,
                    anthropic_session_id = %s,
                    anthropic_request_ids = %s,
                    mcp_calls = %s,
                    cost_cents = %s,
                    error_blob = %s,
                    completed_at = NOW()
                WHERE id = %s
                """,
                (
                    status,
                    Jsonb(output_blob) if output_blob is not None else None,
                    output_artifact_path,
                    anthropic_session_id,
                    Jsonb(anthropic_request_ids) if anthropic_request_ids else None,
                    Jsonb(mcp_calls) if mcp_calls else None,
                    cost_cents,
                    Jsonb(error_blob) if error_blob else None,
                    str(run_id),
                ),
            )
        await conn.commit()


async def _find_actor_run_id(
    initiative_id: UUID,
    parent_actor_slug: str,
    *,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> UUID | None:
    """Find the latest run_id for the given parent actor — used to set
    parent_run_id on verdict rows. For fanout verdicts the lookup is
    scoped to the same (recipient, step) tuple so the verdict pairs
    with that recipient's actor row, not a sibling's.
    """
    where = ["initiative_id = %s", "agent_slug = %s"]
    args: list[Any] = [str(initiative_id), parent_actor_slug]
    if recipient_id is not None:
        where.append("recipient_id = %s")
        args.append(str(recipient_id))
    if channel_campaign_step_id is not None:
        where.append("channel_campaign_step_id = %s")
        args.append(str(channel_campaign_step_id))
    where.append("status IN ('running', 'succeeded', 'failed')")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id FROM business.gtm_subagent_runs
                WHERE {' AND '.join(where)}
                ORDER BY run_index DESC
                LIMIT 1
                """,
                args,
            )
            row = await cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Pipeline status helpers (on business.gtm_initiatives)
# ---------------------------------------------------------------------------


async def set_pipeline_status(
    initiative_id: UUID,
    status: str,
    *,
    bump_started_at: bool = False,
) -> None:
    fields = ["pipeline_status = %s"]
    args: list[Any] = [status]
    if bump_started_at:
        fields.append("last_pipeline_run_started_at = NOW()")
    fields.append("updated_at = NOW()")
    args.append(str(initiative_id))
    sql = (
        "UPDATE business.gtm_initiatives "
        f"SET {', '.join(fields)} WHERE id = %s"
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, args)
        await conn.commit()


async def set_gating_mode(initiative_id: UUID, gating_mode: str) -> None:
    if gating_mode not in ("auto", "manual"):
        raise ValueError(f"invalid gating_mode={gating_mode!r}")
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_initiatives
                SET gating_mode = %s,
                    updated_at = NOW()
                WHERE id = %s
                """,
                (gating_mode, str(initiative_id)),
            )
        await conn.commit()


# ---------------------------------------------------------------------------
# kickoff / run_step / request_rerun
# ---------------------------------------------------------------------------


async def kickoff_pipeline(
    initiative_id: UUID,
    *,
    gating_mode: str = "auto",
    start_from: str | None = None,
) -> dict[str, Any]:
    """Mark the initiative running, fire the Trigger.dev workflow.

    Returns ``{trigger_run_id, pipeline_status}``.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise GtmPipelineError(f"initiative {initiative_id} not found")
    if gating_mode not in ("auto", "manual"):
        raise GtmPipelineError(f"invalid gating_mode={gating_mode!r}")

    await set_gating_mode(initiative_id, gating_mode)
    await set_pipeline_status(
        initiative_id, "running", bump_started_at=True,
    )

    payload: dict[str, Any] = {
        "initiativeId": str(initiative_id),
        "gatingMode": gating_mode,
    }
    if start_from:
        payload["startFrom"] = start_from

    trigger_run_id = await activation_jobs.enqueue_via_trigger(
        task_identifier=_TRIGGER_TASK_ID,
        payload_override=payload,
    )
    return {
        "trigger_run_id": trigger_run_id,
        "pipeline_status": "running",
        "gating_mode": gating_mode,
        "start_from": start_from,
    }


async def request_rerun(
    initiative_id: UUID, from_agent_slug: str,
) -> dict[str, Any]:
    """Mark from_agent_slug + downstream rows as superseded; re-fire the
    pipeline starting from from_agent_slug."""
    superseded = await _supersede_downstream(initiative_id, from_agent_slug)
    logger.info(
        "request_rerun superseded %d rows initiative=%s from=%s",
        superseded, initiative_id, from_agent_slug,
    )
    return await kickoff_pipeline(
        initiative_id, gating_mode="auto", start_from=from_agent_slug,
    )


async def _execute_materializer_plan(
    *,
    agent_slug: str,
    initiative_id: UUID,
    plan: dict[str, Any],
    upstream_outputs: dict[str, Any] | None,
) -> dict[str, Any]:
    """Dispatch to the right materializer_execution function. Returns
    the ``executed`` blob written into output_blob.value.executed.

    For audience_materializer the dm step ids come from the upstream
    channel-step-materializer's executed result. We honor an optional
    ``MATERIALIZER_AUDIENCE_LIMIT`` env var so dev runs can cap the
    audience materialization at a small number of recipients.
    """
    if agent_slug == "gtm-channel-step-materializer":
        result = await materializer_execution.execute_channel_step_plan(
            initiative_id, plan,
        )
        return {
            "campaign_id": str(result["campaign_id"]),
            "channel_campaign_ids": {
                k: str(v) for k, v in result["channel_campaign_ids"].items()
            },
            "channel_campaign_step_ids": [
                str(s) for s in result["channel_campaign_step_ids"]
            ],
            "dm_step_ids": [str(s) for s in result["dm_step_ids"]],
        }
    if agent_slug == "gtm-audience-materializer":
        upstream_executed = _extract_executed(
            upstream_outputs, "gtm-channel-step-materializer"
        ) or {}
        dm_step_ids = [
            UUID(s) for s in (upstream_executed.get("dm_step_ids") or [])
        ]
        if not dm_step_ids:
            raise materializer_execution.MaterializerExecutionError(
                "audience materializer cannot run — upstream channel-step "
                "materializer's executed.dm_step_ids is missing/empty"
            )
        import os
        raw_limit = os.environ.get("MATERIALIZER_AUDIENCE_LIMIT")
        audience_limit: int | None = None
        if raw_limit:
            try:
                audience_limit = max(0, int(raw_limit))
            except ValueError:
                audience_limit = None
        result = await materializer_execution.execute_audience_plan(
            initiative_id,
            plan,
            dm_step_ids=dm_step_ids,
            audience_limit=audience_limit,
        )
        return {**result, "audience_limit_applied": audience_limit}
    raise materializer_execution.MaterializerExecutionError(
        f"_execute_materializer_plan called for non-materializer slug={agent_slug!r}"
    )


async def run_step(
    *,
    initiative_id: UUID,
    agent_slug: str,
    hint: str | None = None,
    upstream_outputs: dict[str, Any] | None = None,
    recipient_id: UUID | None = None,
    channel_campaign_step_id: UUID | None = None,
) -> dict[str, Any]:
    """Single-call execution of one agent against one initiative.

    Returns the StepResult dict the Trigger workflow consumes. Never
    raises for verdict-failed-to-ship — that's a normal succeeded run
    with `output_blob.ship == False`. Raises RunStepError only on
    Anthropic errors, parse errors that have nothing recoverable in
    them, or DB errors.

    ``recipient_id`` / ``channel_campaign_step_id`` are set for fanout
    runs (per-recipient creative + verdict). When supplied:
      * ``_assemble_input`` loads ONLY that recipient + step
      * supersede + run_index lookups are scoped to that (recipient,
        step) pair, so concurrent fanout invocations don't collide
        and re-running one recipient doesn't invalidate another.
    """
    registry = await agent_prompts.get_registry_row(agent_slug)
    if registry is None or registry.get("deactivated_at"):
        raise AgentSlugNotRegistered(
            f"agent_slug={agent_slug!r} not active in business.gtm_agent_registry"
        )

    latest_version = await agent_prompts.get_latest_version(agent_slug)
    system_prompt = (latest_version or {}).get("system_prompt") or ""
    prompt_version_id_str = (latest_version or {}).get("id")
    prompt_version_id = (
        prompt_version_id_str if prompt_version_id_str else None
    )

    bundle = await _load_initiative_bundle(initiative_id)
    user_text, input_blob = await _assemble_input(
        agent_slug=agent_slug,
        initiative_id=initiative_id,
        bundle=bundle,
        upstream_outputs=upstream_outputs,
        hint=hint,
        recipient_id=recipient_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )

    parent_run_id: UUID | None = None
    if registry["role"] in ("verdict", "critic"):
        parent_actor_slug = registry["parent_actor_slug"]
        if parent_actor_slug:
            parent_run_id = await _find_actor_run_id(
                initiative_id, parent_actor_slug,
                recipient_id=recipient_id,
                channel_campaign_step_id=channel_campaign_step_id,
            )

    # Mark this agent_slug + downstream rows as superseded BEFORE inserting
    # the new running row. For per-recipient rerun the supersede is scoped
    # to that (recipient, step) tuple only.
    await _supersede_downstream(
        initiative_id, agent_slug,
        recipient_id=recipient_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )

    run_index = await _next_run_index(
        initiative_id, agent_slug,
        recipient_id=recipient_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )
    run_id = await _insert_running_run(
        initiative_id=initiative_id,
        agent_slug=agent_slug,
        run_index=run_index,
        parent_run_id=parent_run_id,
        input_blob=input_blob,
        system_prompt_snapshot=system_prompt,
        prompt_version_id=prompt_version_id,
        anthropic_agent_id=registry["anthropic_agent_id"],
        model=registry["model"],
        recipient_id=recipient_id,
        channel_campaign_step_id=channel_campaign_step_id,
    )

    try:
        session_result = await anthropic_managed_agents.run_session(
            agent_id=registry["anthropic_agent_id"],
            user_message=user_text,
            title=f"gtm:{agent_slug}:initiative={initiative_id}:run={run_index}",
            metadata={
                "initiative_id": str(initiative_id),
                "agent_slug": agent_slug,
                "run_id": str(run_id),
                "run_index": str(run_index),
            },
        )
    except anthropic_managed_agents.ManagedAgentsError as exc:
        await _finalize_run(
            run_id=run_id,
            status="failed",
            output_blob={
                "error": "anthropic_call_failed",
                "message": exc.message,
                "status_code": exc.status_code,
            },
            output_artifact_path=None,
            anthropic_session_id=None,
            anthropic_request_ids=None,
            mcp_calls=None,
            cost_cents=None,
            error_blob={
                "kind": "anthropic_error",
                "status_code": exc.status_code,
                "message": exc.message,
                "response_body": exc.response_body,
            },
        )
        raise RunStepError(
            f"anthropic call failed for agent_slug={agent_slug}: {exc.message}"
        ) from exc

    parsed = _parse_actor_output(agent_slug, session_result["assistant_text"])

    output_artifact_path: str | None = None
    if (
        agent_slug == "gtm-master-strategist"
        and parsed.get("parse_ok")
        and parsed.get("shape") == "markdown_with_yaml"
    ):
        out_dir = _INITIATIVES_ROOT / str(initiative_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"master_strategy.{run_index:03d}.md"
        path.write_text(parsed["value"])
        output_artifact_path = str(path)

    if parsed["parse_ok"]:
        # For materializer actors, the parsed JSON is the agent's PLAN.
        # hq-x then executes that plan (writes campaigns/steps/audience
        # rows to the DB) and folds the execution result into output_blob
        # so downstream agents can read both the plan and the executed
        # ids via _extract_executed.
        executed: dict[str, Any] | None = None
        execution_error: str | None = None
        if (
            agent_slug in _MATERIALIZER_ACTOR_SLUGS
            and parsed["shape"] == "json"
            and isinstance(parsed["value"], dict)
        ):
            try:
                executed = await _execute_materializer_plan(
                    agent_slug=agent_slug,
                    initiative_id=initiative_id,
                    plan=parsed["value"],
                    upstream_outputs=upstream_outputs,
                )
            except materializer_execution.MaterializerExecutionError as exc:
                logger.exception(
                    "materializer execution failed initiative=%s slug=%s",
                    initiative_id, agent_slug,
                )
                execution_error = str(exc)

        if executed is not None:
            value_payload: Any = {
                "plan": parsed["value"],
                "executed": executed,
            }
        elif execution_error is not None:
            value_payload = {
                "plan": parsed["value"],
                "executed": None,
                "execution_error": execution_error,
            }
        elif parsed["shape"] == "json":
            value_payload = parsed["value"]
        else:
            value_payload = parsed["raw"][:8000]

        output_blob = {
            "shape": parsed["shape"],
            "raw_chars": len(parsed["raw"]),
            "value": value_payload,
            "raw_excerpt": parsed["raw"][:1500],
            "terminal_status": session_result["terminal_status"],
            "stop_reason": session_result["stop_reason"],
        }
        if execution_error is not None:
            status = "failed"
        else:
            # `idle` = agent finished its turn awaiting next user message;
            # for our one-shot run_session pattern that IS the success
            # state (we never send a follow-up message). `completed` /
            # `stopped` / `running` are the other observed-success values.
            status = (
                "succeeded"
                if session_result["terminal_status"] in {"completed", "stopped", "running", "idle"}
                else "failed"
            )
    else:
        output_blob = {
            "shape": parsed["shape"],
            "raw_chars": len(parsed["raw"]),
            "value": None,
            "raw_excerpt": parsed["raw"][:1500],
            "parse_error": parsed["parse_error"],
            "terminal_status": session_result["terminal_status"],
            "stop_reason": session_result["stop_reason"],
        }
        status = "failed"

    await _finalize_run(
        run_id=run_id,
        status=status,
        output_blob=output_blob,
        output_artifact_path=output_artifact_path,
        anthropic_session_id=session_result["session_id"],
        anthropic_request_ids=session_result["request_ids"],
        mcp_calls=session_result["mcp_calls"],
        cost_cents=None,  # cost computation lands in a follow-up directive
        error_blob=(
            {
                "kind": "parse_error",
                "message": parsed["parse_error"],
            }
            if not parsed["parse_ok"]
            else None
        ),
    )

    return {
        "run_id": str(run_id),
        "run_index": run_index,
        "status": status,
        "output_blob": output_blob,
        "output_artifact_path": output_artifact_path,
        "prompt_version_id": str(prompt_version_id) if prompt_version_id else None,
        "anthropic_session_id": session_result["session_id"],
        "anthropic_request_ids": session_result["request_ids"],
        "cost_cents": None,
    }


# ---------------------------------------------------------------------------
# Read paths for the admin router
# ---------------------------------------------------------------------------


async def list_runs_for_initiative(
    initiative_id: UUID,
    *,
    agent_slug: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where = ["initiative_id = %s"]
    args: list[Any] = [str(initiative_id)]
    if agent_slug:
        where.append("agent_slug = %s")
        args.append(agent_slug)
    args.extend([limit, offset])
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, initiative_id, agent_slug, run_index, parent_run_id,
                       status, input_blob, output_blob, output_artifact_path,
                       prompt_version_id, anthropic_agent_id,
                       anthropic_session_id, anthropic_request_ids, mcp_calls,
                       cost_cents, model, started_at, completed_at, error_blob,
                       recipient_id, channel_campaign_step_id
                FROM business.gtm_subagent_runs
                WHERE {' AND '.join(where)}
                ORDER BY started_at DESC
                LIMIT %s OFFSET %s
                """,
                args,
            )
            rows = await cur.fetchall()
    return [_run_row_to_dict(r) for r in rows]


async def get_run(run_id: UUID) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, initiative_id, agent_slug, run_index, parent_run_id,
                       status, input_blob, output_blob, output_artifact_path,
                       prompt_version_id, anthropic_agent_id,
                       anthropic_session_id, anthropic_request_ids, mcp_calls,
                       cost_cents, model, started_at, completed_at, error_blob,
                       recipient_id, channel_campaign_step_id,
                       system_prompt_snapshot
                FROM business.gtm_subagent_runs
                WHERE id = %s
                """,
                (str(run_id),),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    base = _run_row_to_dict(row)
    base["system_prompt_snapshot"] = row[21]
    return base


def _run_row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "id": row[0],
        "initiative_id": row[1],
        "agent_slug": row[2],
        "run_index": row[3],
        "parent_run_id": row[4],
        "status": row[5],
        "input_blob": row[6] or {},
        "output_blob": row[7],
        "output_artifact_path": row[8],
        "prompt_version_id": row[9],
        "anthropic_agent_id": row[10],
        "anthropic_session_id": row[11],
        "anthropic_request_ids": row[12] or [],
        "mcp_calls": row[13] or [],
        "cost_cents": row[14],
        "model": row[15],
        "started_at": row[16],
        "completed_at": row[17],
        "error_blob": row[18],
        "recipient_id": row[19] if len(row) > 19 else None,
        "channel_campaign_step_id": row[20] if len(row) > 20 else None,
    }


__all__ = [
    "GtmPipelineError",
    "RunStepError",
    "AgentSlugNotRegistered",
    "PIPELINE_STEPS",
    "kickoff_pipeline",
    "request_rerun",
    "run_step",
    "set_pipeline_status",
    "set_gating_mode",
    "list_runs_for_initiative",
    "get_run",
]
