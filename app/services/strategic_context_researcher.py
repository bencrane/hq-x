"""Subagent 1 — strategic-context researcher.

Thin wrapper around the existing exa_research_jobs pipeline. Does NOT
call Exa directly — it just constructs the audience-scoped,
operator-voice-sourced research instructions, creates an
exa_research_jobs row with `objective='strategic_context_research'` and
`objective_ref='initiative:<uuid>'`, enqueues the existing
`exa.process_research_job` Trigger task, and transitions the initiative
to `awaiting_strategic_research`.

The completion path is handled inside the internal exa router's
post-process-by-objective dispatcher, which detects the
`strategic_context_research` objective and writes back to the initiative
when the underlying exa job succeeds.

The research-instruction template is the **one obvious place to edit
the prompt** as we iterate. Everything else is plumbing.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from uuid import UUID

from app.db import get_db_connection
from app.services import activation_jobs as jobs_svc
from app.services import dex_client
from app.services import exa_research_jobs as exa_jobs_svc
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)


class StrategicContextResearcherError(Exception):
    pass


_TASK_IDENTIFIER = "exa.process_research_job"

# Where the brand-context .md files live, relative to the repo root.
# Resolved per-call so tests can monkeypatch.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BRANDS_ROOT = _REPO_ROOT / "data" / "brands"


# ---------------------------------------------------------------------------
# Research instruction template — iterate freely. The synthesizer's
# system prompt has a sibling constant in app/services/strategy_synthesizer.py
# that is structured the same way (versioned constant, one obvious edit
# point).
# ---------------------------------------------------------------------------

_RESEARCH_INSTRUCTION_TEMPLATE = """\
Produce strategic-context research for an outbound GTM motion that will
be run UNDER the brand "{brand_display_name}" ({brand_slug}) — a
matchmaker-not-lender capital advisory brand — TO the audience defined
below, on behalf of the demand-side partner described below.

The objective is to surface what the audience THEMSELVES are saying
right now about the operational pain that the partner solves. We
already have a partner-research run that mapped the partner's own
positioning, products, and proof; do not reproduce that. We are looking
for the operator-side discourse that maps onto it.

# Audience

{audience_block}

# Brand positioning (the brand under which outreach runs)

{brand_positioning_block}

# Partner

{partner_block}

# Partner research already in hand (do not re-derive)

{partner_research_summary_block}

# What this research must surface

Return findings organized under these sections. Cite inline with primary
URLs. Prefer the operator's own voice — verbatim phrases from forums,
review sites, trade press — over marketing copy.

1. Operator-side discourse on the pain the partner addresses.
   - Where the audience itself talks about this pain right now (last
     6–12 months only). Reddit subforums, trade press, G2 / Trustpilot /
     Capterra reviews of the partner AND of its alternatives, industry
     blogs, podcast transcripts. Quote the verbatim phrases the
     audience uses, not paraphrases.
   - Common "before" feelings, in the audience's own words.
   - The most-discussed adjacent grievances that often co-occur with
     the core pain (so the GTM motion can hook on the broader frame).

2. Audience perception of the partner and its alternatives.
   - How operators publicly describe their experience with the
     partner. Positive AND negative; the negative is more useful for
     positioning.
   - What operators say about the partner's named alternatives. Where
     they switch, why they switch, what they say after switching.
   - Concrete operator-language phrases that distinguish the partner
     from substitutes in the audience's own framing.

3. Time-relevant context (last 6–12 months).
   - Rate environment, regulatory shifts, macro conditions, supply
     chain or commodity dynamics, recent news that materially affects
     the audience's business right now.
   - What a "why-now" outreach hook would credibly cite, with sources.

4. Audience-side concerns that COULD blunt outreach.
   - Recent fraud, scam, or "spam from people pretending to know me"
     patterns the audience is currently reacting to. Over-pitched
     channels. Any channel exhaustion or compliance friction.
   - What outreach the audience is currently fatigued by — so we can
     position around it rather than into it.

5. Concrete language hooks.
   - 10–20 verbatim phrases the audience uses about this domain that a
     copywriter could weave into bespoke per-recipient creative without
     sounding marketed-at.
   - The 2–3 framings the audience treats as table-stakes (using them
     buys nothing) and the 2–3 framings the audience treats as
     differentiators (using them earns attention).

6. Brand fit notes (very short).
   - Anything in the operator-side discourse that suggests where the
     "{brand_display_name}" matchmaker positioning would land
     particularly well, OR particularly poorly. One short paragraph.

# Constraints

- Time window: last 6–12 months. Older sources only when establishing
  durable structural facts.
- Sources to favor: operator-voice forums (Reddit, industry forums),
  third-party review sites, trade-press reporting on the audience
  segment, recent macro/regulatory pieces. Cite primary sources with
  URLs.
- Sources to AVOID: the partner's own marketing pages (already covered
  in partner-research). The brand's own marketing pages (the brand is
  new). Aggregator/SEO content farms.
- Anti-fabrication: where you cannot find an operator-voice source for
  a claim, say so. Do not invent quotes or paraphrase secondary
  marketing copy as if it were operator language.
- Write the report as research output, not as outreach copy. The
  downstream synthesizer turns this into strategy.
"""


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


async def _load_partner(partner_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, domain, primary_contact_name,
                       primary_contact_email, primary_phone, intro_email,
                       hours_of_operation_config, metadata
                FROM business.demand_side_partners
                WHERE id = %s
                """,
                (str(partner_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise StrategicContextResearcherError(f"partner {partner_id} not found")
    return {
        "id": row[0],
        "name": row[1],
        "domain": row[2],
        "primary_contact_name": row[3],
        "primary_contact_email": row[4],
        "primary_phone": row[5],
        "intro_email": row[6],
        "hours_of_operation_config": row[7] or {},
        "metadata": row[8] or {},
    }


async def _load_partner_contract(contract_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, partner_id, pricing_model, amount_cents,
                       duration_days, max_capital_outlay_cents,
                       qualification_rules, terms_blob, status,
                       starts_at, ends_at
                FROM business.partner_contracts
                WHERE id = %s
                """,
                (str(contract_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise StrategicContextResearcherError(f"contract {contract_id} not found")
    return {
        "id": row[0],
        "partner_id": row[1],
        "pricing_model": row[2],
        "amount_cents": row[3],
        "duration_days": row[4],
        "max_capital_outlay_cents": row[5],
        "qualification_rules": row[6] or {},
        "terms_blob": row[7],
        "status": row[8],
        "starts_at": row[9],
        "ends_at": row[10],
    }


async def _load_brand(brand_id: UUID) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT id, name, domain
                FROM business.brands
                WHERE id = %s
                """,
                (str(brand_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise StrategicContextResearcherError(f"brand {brand_id} not found")
    return {"id": row[0], "name": row[1], "domain": row[2]}


def _slugify_brand_name(name: str) -> str:
    """Brand slug derivation: lowercase, spaces to dashes. The brand row
    doesn't carry a slug column today; we derive from name, matching the
    on-disk directory naming convention under data/brands/."""
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _load_brand_positioning(brand_slug: str) -> str:
    path = _BRANDS_ROOT / brand_slug / "positioning.md"
    if not path.exists():
        # Don't fail — research can proceed without brand positioning text.
        return f"(brand positioning file not found at {path}; brand slug={brand_slug})"
    return path.read_text()


def _summarize_partner_research(payload: dict[str, Any] | None) -> str:
    """Boil the partner_research exa_calls payload down to ~1.5KB so the
    Exa instruction stays focused. The synthesizer (subagent 2) reads
    the full payload separately."""
    if not payload:
        return "(no partner-research payload available)"
    output = payload.get("output") if isinstance(payload, dict) else None
    if isinstance(output, dict):
        content = output.get("content")
        if isinstance(content, str) and content:
            # Take first N chars; the strategic-context researcher
            # doesn't need the full report, just the partner shape.
            head = content[:1500]
            if len(content) > 1500:
                head += "\n…(truncated; full report lives in exa.exa_calls)"
            return head
    shape = (
        list(payload.keys())
        if isinstance(payload, dict)
        else type(payload).__name__
    )
    return f"(unexpected payload shape: {shape})"


async def _fetch_partner_research_payload(
    partner_research_ref: str | None,
) -> dict[str, Any] | None:
    """Fetch the partner-research response_payload by ref.

    ref shape: '<destination>://exa.exa_calls/<uuid>'.

    For 'hqx://...' we hit the local DB. For 'dex://...' we'd need a
    cross-DB read; that's out of scope for slice 1 — we degrade
    gracefully by returning None so the instruction template falls back
    to the sentinel string.
    """
    if not partner_research_ref:
        return None
    try:
        scheme, rest = partner_research_ref.split("://", 1)
    except ValueError:
        logger.warning("malformed partner_research_ref=%r", partner_research_ref)
        return None
    if scheme != "hqx":
        logger.info(
            "partner_research_ref destination=%s not supported by slice 1; "
            "synthesizer can still load it directly",
            scheme,
        )
        return None
    # rest like 'exa.exa_calls/<uuid>'
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


def _format_audience_block(descriptor: dict[str, Any] | None) -> str:
    if not descriptor:
        return "(no audience descriptor available — DEX call failed)"
    spec = descriptor.get("spec") or {}
    template = descriptor.get("template") or {}
    attrs = descriptor.get("audience_attributes") or []
    lines: list[str] = []
    if name := spec.get("name"):
        lines.append(f"Spec name: {name}")
    if template_name := template.get("name"):
        lines.append(f"Template: {template_name} (slug={template.get('slug')})")
    if desc := template.get("description"):
        lines.append(f"Template description: {desc}")
    if attrs:
        lines.append("Resolved attributes:")
        for a in attrs:
            schema = a.get("schema") or {}
            type_hint = schema.get("type", "?")
            lines.append(f"  - {a.get('key')} ({type_hint}) = {a.get('value')!r}")
    if not lines:
        return f"(descriptor empty: {descriptor!r})"
    return "\n".join(lines)


def _format_partner_block(
    partner: dict[str, Any], contract: dict[str, Any]
) -> str:
    lines = [
        f"Name: {partner['name']}",
        f"Domain: {partner.get('domain') or '(none)'}",
    ]
    if partner.get("primary_contact_email"):
        lines.append(
            f"Primary contact: "
            f"{partner.get('primary_contact_name') or '(name unset)'} "
            f"<{partner['primary_contact_email']}>"
        )
    lines.append("Contract:")
    lines.append(f"  pricing_model={contract['pricing_model']}")
    lines.append(f"  duration_days={contract['duration_days']}")
    if contract.get("amount_cents") is not None:
        lines.append(f"  amount_cents={contract['amount_cents']}")
    if contract.get("max_capital_outlay_cents") is not None:
        lines.append(
            f"  max_capital_outlay_cents={contract['max_capital_outlay_cents']}"
        )
    if contract.get("qualification_rules"):
        lines.append(f"  qualification_rules={contract['qualification_rules']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def run_strategic_context_research(
    *,
    initiative_id: UUID,
    organization_id: UUID,
    created_by_user_id: UUID | None,
) -> dict[str, Any]:
    """Render the research instruction, create the exa job, transition
    the initiative to `awaiting_strategic_research`.

    Returns ``{"exa_job_id": UUID, "status": "queued"}``. Raises
    ``StrategicContextResearcherError`` for input-loading failures and
    ``gtm_svc.InvalidStatusTransition`` for state-machine refusals.
    """
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise StrategicContextResearcherError(
            f"initiative {initiative_id} not found"
        )

    brand = await _load_brand(initiative["brand_id"])
    partner = await _load_partner(initiative["partner_id"])
    contract = await _load_partner_contract(initiative["partner_contract_id"])

    # Audience descriptor — DEX call. Best-effort: degrade to a sentinel
    # if DEX is unreachable so we still kick the research run and surface
    # the failure on the resulting report rather than 500-ing here.
    descriptor: dict[str, Any] | None
    try:
        descriptor = await dex_client.get_audience_descriptor(
            initiative["data_engine_audience_id"]
        )
    except dex_client.DexClientError as exc:
        logger.warning(
            "dex.get_audience_descriptor failed during strategic-context "
            "research for initiative=%s err=%r",
            initiative_id, exc,
        )
        descriptor = None

    partner_research_payload = await _fetch_partner_research_payload(
        initiative.get("partner_research_ref")
    )

    brand_slug = _slugify_brand_name(brand["name"])
    instructions = _RESEARCH_INSTRUCTION_TEMPLATE.format(
        brand_display_name=brand["name"],
        brand_slug=brand_slug,
        audience_block=_format_audience_block(descriptor),
        brand_positioning_block=_load_brand_positioning(brand_slug),
        partner_block=_format_partner_block(partner, contract),
        partner_research_summary_block=_summarize_partner_research(
            partner_research_payload
        ),
    )

    # Create / dedupe the exa research job. Idempotency keys are scoped
    # to the initiative so re-firing the endpoint returns the same job.
    job = await exa_jobs_svc.create_job(
        organization_id=organization_id,
        created_by_user_id=created_by_user_id,
        endpoint="research",
        destination="hqx",
        objective="strategic_context_research",
        objective_ref=f"initiative:{initiative_id}",
        request_payload={"instructions": instructions},
        idempotency_key=f"strategic-context-{initiative_id}",
    )

    # Only enqueue Trigger if this is a fresh job. On idempotency replay
    # the existing job already has its trigger run.
    if not job.get("trigger_run_id"):
        try:
            run_id = await jobs_svc.enqueue_via_trigger(
                task_identifier=_TASK_IDENTIFIER,
                payload_override={"job_id": str(job["id"])},
            )
        except jobs_svc.TriggerEnqueueError as exc:
            await exa_jobs_svc.mark_failed(
                job["id"],
                error={
                    "reason": "trigger_enqueue_failed",
                    "message": str(exc)[:500],
                },
            )
            raise StrategicContextResearcherError(
                f"failed to enqueue strategic-context-research trigger: {exc}"
            ) from exc
        await exa_jobs_svc.update_trigger_run_id(job["id"], run_id)

    # State-machine transition. If we're already in
    # `awaiting_strategic_research` (replay) the transition is a no-op
    # by design; only allow the move when current status is `draft` or
    # `failed`. The endpoint pre-checks and refuses other states.
    if initiative["status"] in ("draft", "failed"):
        await gtm_svc.transition_status(
            initiative_id,
            new_status="awaiting_strategic_research",
            history_event={
                "kind": "transition",
                "trigger": "strategic_context_researcher",
                "exa_job_id": str(job["id"]),
            },
        )

    return {"exa_job_id": job["id"], "status": "queued"}


__all__ = [
    "StrategicContextResearcherError",
    "run_strategic_context_research",
]
