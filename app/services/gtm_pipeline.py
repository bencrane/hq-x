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
PIPELINE_STEPS: list[dict[str, str]] = [
    {"actor": "gtm-sequence-definer", "verdict": "gtm-sequence-definer-verdict"},
    {"actor": "gtm-master-strategist", "verdict": "gtm-master-strategist-verdict"},
    {"actor": "gtm-per-recipient-creative", "verdict": "gtm-per-recipient-creative-verdict"},
]


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
        sample = await _load_audience_sample(
            bundle["initiative"]["data_engine_audience_id"], limit=1,
        )
        recipient = sample[0] if sample else {}
        return _assemble_per_recipient_creative(
            bundle=bundle,
            recipient=recipient,
            upstream_outputs=upstream_outputs,
            hint=hint,
        )

    if agent_slug == "gtm-per-recipient-creative-verdict":
        sample = await _load_audience_sample(
            bundle["initiative"]["data_engine_audience_id"], limit=1,
        )
        recipient = sample[0] if sample else {}
        return _assemble_per_recipient_creative_verdict(
            bundle=bundle,
            recipient=recipient,
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


def _assemble_per_recipient_creative(
    *,
    bundle: dict[str, Any],
    recipient: dict[str, Any],
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
        f"<recipient>\n{json.dumps(recipient, default=str, indent=2)}\n</recipient>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        f"<spec_zone_catalog>\n{json.dumps(spec_zone_catalog, indent=2)}\n</spec_zone_catalog>\n\n"
        + (f"\n<hint>\n{hint}\n</hint>\n\n" if hint else "")
        + "Produce the per-piece JSON array per the system prompt."
    )
    blob = {
        "agent_slug": "gtm-per-recipient-creative",
        "recipient_keys": sorted(recipient.keys()),
        "hint": hint,
    }
    return text, blob


def _assemble_per_recipient_creative_verdict(
    *,
    bundle: dict[str, Any],
    recipient: dict[str, Any],
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
        f"<recipient>\n{json.dumps(recipient, default=str, indent=2)}\n</recipient>\n\n"
        f"<brand_content>\n{_format_brand_content(bundle['brand_content'])}\n</brand_content>\n\n"
        f"<independent_brand_doctrine>\n{_load_independent_brand_doctrine()}\n</independent_brand_doctrine>\n\n"
        f"<spec_zone_catalog>\n{json.dumps(spec_zone_catalog, indent=2)}\n</spec_zone_catalog>\n\n"
        f"<actor_output>\n{actor_output}\n</actor_output>\n\n"
        "Return your verdict JSON per the system prompt."
    )
    blob = {"agent_slug": "gtm-per-recipient-creative-verdict"}
    return text, blob


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
    return s


# ---------------------------------------------------------------------------
# gtm_subagent_runs row helpers
# ---------------------------------------------------------------------------


async def _next_run_index(initiative_id: UUID, agent_slug: str) -> int:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COALESCE(MAX(run_index), 0) + 1
                FROM business.gtm_subagent_runs
                WHERE initiative_id = %s AND agent_slug = %s
                """,
                (str(initiative_id), agent_slug),
            )
            row = await cur.fetchone()
    return int(row[0])


async def _supersede_downstream(
    initiative_id: UUID, agent_slug: str,
) -> int:
    """Mark every gtm_subagent_runs row for the initiative whose agent_slug
    is downstream of `agent_slug` (or is `agent_slug` itself, in any
    succeeded/running/queued state) as 'superseded'.

    Downstream is defined by PIPELINE_STEPS order. agent_slug's pair
    (actor + verdict) and everything after counts as 'this and downstream'.
    """
    # Resolve which slugs to supersede.
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

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE business.gtm_subagent_runs
                SET status = 'superseded',
                    completed_at = COALESCE(completed_at, NOW())
                WHERE initiative_id = %s
                  AND agent_slug = ANY(%s)
                  AND status IN ('queued', 'running', 'succeeded', 'failed')
                RETURNING id
                """,
                (str(initiative_id), list(slugs)),
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
) -> UUID:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.gtm_subagent_runs (
                    initiative_id, agent_slug, run_index, parent_run_id,
                    status, input_blob, system_prompt_snapshot,
                    prompt_version_id, anthropic_agent_id, model
                )
                VALUES (%s, %s, %s, %s, 'running', %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(initiative_id), agent_slug, run_index,
                    str(parent_run_id) if parent_run_id else None,
                    Jsonb(input_blob),
                    system_prompt_snapshot,
                    str(prompt_version_id) if prompt_version_id else None,
                    anthropic_agent_id, model,
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
    initiative_id: UUID, parent_actor_slug: str,
) -> UUID | None:
    """Find the latest run_id for the given parent actor — used to set
    parent_run_id on verdict rows."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id FROM business.gtm_subagent_runs
                WHERE initiative_id = %s AND agent_slug = %s
                  AND status IN ('running', 'succeeded', 'failed')
                ORDER BY run_index DESC
                LIMIT 1
                """,
                (str(initiative_id), parent_actor_slug),
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


async def run_step(
    *,
    initiative_id: UUID,
    agent_slug: str,
    hint: str | None = None,
    upstream_outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Single-call execution of one agent against one initiative.

    Returns the StepResult dict the Trigger workflow consumes. Never
    raises for verdict-failed-to-ship — that's a normal succeeded run
    with `output_blob.ship == False`. Raises RunStepError only on
    Anthropic errors, parse errors that have nothing recoverable in
    them, or DB errors.
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
    )

    parent_run_id: UUID | None = None
    if registry["role"] in ("verdict", "critic"):
        parent_actor_slug = registry["parent_actor_slug"]
        if parent_actor_slug:
            parent_run_id = await _find_actor_run_id(
                initiative_id, parent_actor_slug
            )

    # Mark this agent_slug + downstream rows as superseded BEFORE inserting
    # the new running row. _supersede_downstream walks PIPELINE_STEPS so
    # it correctly handles both actor and verdict slugs.
    await _supersede_downstream(initiative_id, agent_slug)

    run_index = await _next_run_index(initiative_id, agent_slug)
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
        output_blob = {
            "shape": parsed["shape"],
            "raw_chars": len(parsed["raw"]),
            "value": parsed["value"]
            if parsed["shape"] == "json"
            else parsed["raw"][:8000],
            "raw_excerpt": parsed["raw"][:1500],
            "terminal_status": session_result["terminal_status"],
            "stop_reason": session_result["stop_reason"],
        }
        status = (
            "succeeded"
            if session_result["terminal_status"] in {"completed", "stopped", "running"}
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
                       cost_cents, model, started_at, completed_at, error_blob
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
    base["system_prompt_snapshot"] = row[19]
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
