"""Subagent 2 — strategy synthesizer.

Reads the six inputs (initiative + partner-research + strategic-context
research + audience descriptor + brand .md files + partner contract),
calls Anthropic once, validates the model's YAML front-matter, and
writes ``data/initiatives/<initiative_id>/campaign_strategy.md`` to disk.

This is the first hq-x → Anthropic call site. The system prompt lives
as a versioned constant ``_SYSTEM_PROMPT_V1`` in this file. Iterate the
text in code; future versions become ``_SYSTEM_PROMPT_V2`` etc. with
``_ACTIVE_SYSTEM_PROMPT`` at the bottom of the file naming the
currently-deployed version. That's the one obvious place to edit.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from app.db import get_db_connection
from app.services import anthropic_client, dex_client
from app.services import gtm_initiatives as gtm_svc

logger = logging.getLogger(__name__)


class StrategySynthesizerError(Exception):
    pass


_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_BRANDS_ROOT = _REPO_ROOT / "data" / "brands"
_INITIATIVES_ROOT = _REPO_ROOT / "data" / "initiatives"

_REQUIRED_FRONT_MATTER_KEYS = (
    "headline_offer",
    "core_thesis",
    "narrative_beats",
    "channel_mix",
    "capital_outlay_plan",
    "personalization_variables",
    "anti_framings",
)


# ---------------------------------------------------------------------------
# Versioned prompt constants.
#
# _SYSTEM_PROMPT_V1 — first cut of the prompt. Iterate freely. The
# system prompt and the brand-content blocks below it are passed as a
# list of system blocks with cache_control so iteration on the dynamic
# user-message inputs is cheap.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_V1 = """\
You are a campaign strategist for an internal owned-brand demand-gen
platform. Your single output is a concise campaign strategy document
that downstream materializers (channel sequence, audience, per-recipient
creative, landing pages, voice agent) will consume.

Loyalty rules — non-negotiable:

1. Voice loyalty. The strategy must speak in the brand's voice as
   defined in the brand .md files supplied below. Do not import voice
   from outside that material. Never use the words/phrases the voice
   guide explicitly forbids; favor the reusable phrases it lists.
2. Anti-fabrication. Every proof claim, statistic, or named entity in
   the strategy must trace back to one of the supplied research
   payloads (partner research or strategic-context research) or to the
   audience descriptor. If the input research does not support a
   claim, do not include it. Where the brand has no track record yet,
   substitute specificity (real audience-pain framings, real capital
   types) for volume claims. The proof-and-credibility brand file is
   binding on what you can and cannot say.
3. Model discipline. The brand is a matchmaker, not a lender; the
   strategy must respect that stance. Do not recommend more than one
   capital type per touch in any per-touch direction. Do not suggest
   creative directions that conflict with the creative-directives
   brand file.

Output contract — strict:

Emit exactly one markdown document. The document begins with a YAML
front-matter block delimited by `---` on its own line above and below.
Then a markdown body. The shape:

```
---
schema_version: 1
initiative_id: <uuid supplied in the user message>
generated_at: <iso-8601 timestamp supplied in the user message>
model: claude-opus-4-7

headline_offer: <one sentence, brand-voice, names the offer>
core_thesis: <one paragraph, brand-voice, why this audience + this partner + now>
narrative_beats:
  - <beat 1, one short line>
  - <beat 2, one short line>
  - <beat 3, one short line>
  - <beat 4, one short line, optional>
  - <beat 5, one short line, optional>
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
  total_estimated_cents: <int — bound by the partner-contract guardrails>
  per_recipient_estimated_cents: <int>
personalization_variables:
  - name: <variable name a downstream creative author will read>
    how_to_pull: <which spec attribute or member-row field it maps to>
anti_framings:
  - <thing the copywriter must NOT say>
  - <another>
---

# <human-readable strategy doc body>

## Why this audience, why this partner, why now
...

## The narrative beats expanded
...

## Per-touch creative direction
...

## What we explicitly avoid
...
```

Constraints on the YAML:

- The front-matter must be valid YAML 1.1.
- All keys in the listed shape are required.
- `narrative_beats` is 3–5 items.
- `channel_mix.direct_mail.touches` and `channel_mix.email.touches` are
  arrays of objects with the fields shown. Use integer day_offsets.
- `capital_outlay_plan.total_estimated_cents` must respect the
  contract's `max_capital_outlay_cents` if provided.
- `personalization_variables` is 3–8 items.
- `anti_framings` is 3–6 items, drawn from the brand voice file's
  "words and framings to avoid" plus any audience-specific landmines
  surfaced in the strategic-context research.

Body constraints:

- Markdown body is brand-voice, peer-to-peer, plain prose. No marketing
  fluff vocabulary; consult the brand voice file for the words/phrases
  to avoid.
- Body length target: 600–1200 words.
- Cite operator-voice phrases verbatim from the strategic-context
  research where they support a beat. Use quotation marks when quoting.

Return only the YAML+markdown document. No preamble, no commentary, no
trailing sign-off. The first three characters of your output must be
`---`."""


_ACTIVE_SYSTEM_PROMPT = _SYSTEM_PROMPT_V1


# ---------------------------------------------------------------------------
# Input loaders
# ---------------------------------------------------------------------------


async def _load_initiative(initiative_id: UUID) -> dict[str, Any]:
    initiative = await gtm_svc.get_initiative(initiative_id)
    if initiative is None:
        raise StrategySynthesizerError(f"initiative {initiative_id} not found")
    return initiative


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
        raise StrategySynthesizerError(f"partner {partner_id} not found")
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
        raise StrategySynthesizerError(f"contract {contract_id} not found")
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
        raise StrategySynthesizerError(f"brand {brand_id} not found")
    return {"id": row[0], "name": row[1], "domain": row[2]}


def _slugify_brand_name(name: str) -> str:
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def _load_brand_files(brand_slug: str) -> dict[str, str]:
    """Load every .md in data/brands/<slug>/. Returns a dict keyed by
    filename (without extension) so the prompt builder can format
    them as labeled sections."""
    brand_dir = _BRANDS_ROOT / brand_slug
    if not brand_dir.exists():
        return {}
    out: dict[str, str] = {}
    for path in sorted(brand_dir.glob("*.md")):
        out[path.stem] = path.read_text()
    return out


async def _fetch_exa_payload(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    try:
        scheme, rest = ref.split("://", 1)
    except ValueError:
        return None
    if scheme != "hqx":
        # dex-side payload — out of scope for slice 1; the strategy
        # synthesizer notes the absence so the prompt sees a sentinel.
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
    """Flatten an Exa research payload to its content string. The
    research endpoint returns ``{output: {content: '...'}, citations:
    [...]}``; we surface the content text and append a short
    citations block when present."""
    if not payload:
        return "(payload missing)"
    output = payload.get("output") if isinstance(payload, dict) else None
    content = ""
    if isinstance(output, dict):
        content = output.get("content") or ""
    cits = payload.get("citations") if isinstance(payload, dict) else None
    cit_block = ""
    if isinstance(cits, list) and cits:
        # Cap to avoid blowing the prompt budget — synthesizer cites
        # already-quoted research, so the URL list is reference more
        # than fuel.
        urls = []
        for c in cits[:30]:
            url = (c or {}).get("url")
            if url:
                urls.append(url)
        if urls:
            cit_block = "\n\n# citations\n" + "\n".join(f"- {u}" for u in urls)
    return (content or "(no content field)") + cit_block


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def _format_audience_block(descriptor: dict[str, Any] | None) -> str:
    if not descriptor:
        return "(audience descriptor unavailable)"
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
        lines.append("Resolved audience attributes:")
        for a in attrs:
            schema = a.get("schema") or {}
            type_hint = schema.get("type", "?")
            lines.append(f"  - {a.get('key')} ({type_hint}) = {a.get('value')!r}")
    return "\n".join(lines) if lines else "(empty descriptor)"


def _format_partner_block(
    partner: dict[str, Any], contract: dict[str, Any]
) -> str:
    lines = [
        f"Name: {partner['name']}",
        f"Domain: {partner.get('domain') or '(none)'}",
    ]
    if partner.get("primary_contact_email"):
        lines.append(
            f"Primary contact: {partner.get('primary_contact_name') or '(unset)'} "
            f"<{partner['primary_contact_email']}>"
        )
    if partner.get("primary_phone"):
        lines.append(f"Primary phone: {partner['primary_phone']}")
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


def _format_brand_block(brand: dict[str, Any], brand_files: dict[str, str]) -> str:
    """Build a single concatenated block of all brand .md content,
    labeled by filename. Goes into the SYSTEM portion (cached) since
    brand content is stable across initiatives."""
    parts: list[str] = [
        f"# Brand: {brand['name']} (domain={brand.get('domain') or '(none)'})",
        "",
    ]
    if not brand_files:
        parts.append("(no brand .md content found on disk)")
        return "\n".join(parts)
    for stem, body in brand_files.items():
        parts.append(f"## brand_file: {stem}.md")
        parts.append(body)
        parts.append("")
    return "\n".join(parts)


def _build_system_blocks(
    *, brand: dict[str, Any], brand_files: dict[str, str]
) -> list[dict[str, Any]]:
    """Two cache-controlled blocks: the static framing prompt + the
    static brand content. Both are stable across re-syntheses for the
    same brand, so cache_control on each lets the model serve the
    prefix from cache when we iterate inputs.
    """
    return [
        {
            "type": "text",
            "text": _ACTIVE_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                "<brand_context>\n"
                + _format_brand_block(brand, brand_files)
                + "\n</brand_context>"
            ),
            "cache_control": {"type": "ephemeral"},
        },
    ]


def _build_user_message(
    *,
    initiative_id: UUID,
    generated_at: str,
    audience_descriptor: dict[str, Any] | None,
    partner: dict[str, Any],
    contract: dict[str, Any],
    partner_research_payload: dict[str, Any] | None,
    strategic_context_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """Single labeled-section user message. Sections are tagged so the
    model can address them by name. ``initiative_id`` and
    ``generated_at`` are supplied here (rather than left for the model
    to fabricate) so the YAML front-matter has stable, real values."""
    text = (
        f"<initiative_id>{initiative_id}</initiative_id>\n\n"
        f"<generated_at>{generated_at}</generated_at>\n\n"
        f"<audience>\n{_format_audience_block(audience_descriptor)}\n</audience>\n\n"
        f"<partner>\n{_format_partner_block(partner, contract)}\n</partner>\n\n"
        f"<partner_research>\n{_exa_text(partner_research_payload)}\n</partner_research>\n\n"
        f"<strategic_context_research>\n"
        f"{_exa_text(strategic_context_payload)}\n"
        f"</strategic_context_research>\n\n"
        "Produce the campaign strategy document per the system prompt's "
        "output contract."
    )
    return {"role": "user", "content": text}


# ---------------------------------------------------------------------------
# YAML front-matter validation
# ---------------------------------------------------------------------------


_FRONT_MATTER_RE = re.compile(
    r"^---\s*\n(.*?)\n---\s*\n(.*)$",
    re.DOTALL,
)


def _parse_front_matter(text: str) -> tuple[dict[str, Any], str] | None:
    """Return (front_matter_dict, body) on success, None on shape error."""
    m = _FRONT_MATTER_RE.match(text.lstrip())
    if not m:
        return None
    raw_yaml = m.group(1)
    body = m.group(2)
    try:
        loaded = yaml.safe_load(raw_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded, body


def _front_matter_valid(fm: dict[str, Any]) -> tuple[bool, str | None]:
    for key in _REQUIRED_FRONT_MATTER_KEYS:
        if key not in fm:
            return False, f"missing key: {key}"
    if not isinstance(fm.get("narrative_beats"), list):
        return False, "narrative_beats must be a list"
    if not isinstance(fm.get("channel_mix"), dict):
        return False, "channel_mix must be a dict"
    if not isinstance(fm.get("capital_outlay_plan"), dict):
        return False, "capital_outlay_plan must be a dict"
    if not isinstance(fm.get("personalization_variables"), list):
        return False, "personalization_variables must be a list"
    if not isinstance(fm.get("anti_framings"), list):
        return False, "anti_framings must be a list"
    return True, None


# ---------------------------------------------------------------------------
# Disk I/O
# ---------------------------------------------------------------------------


def _initiative_dir(initiative_id: UUID) -> Path:
    return _INITIATIVES_ROOT / str(initiative_id)


def _strategy_path(initiative_id: UUID) -> Path:
    return _initiative_dir(initiative_id) / "campaign_strategy.md"


def _failed_synthesis_path(initiative_id: UUID) -> Path:
    return _initiative_dir(initiative_id) / "failed_synthesis.md"


def _write_strategy_file(initiative_id: UUID, content: str) -> Path:
    path = _strategy_path(initiative_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _write_failed_file(initiative_id: UUID, content: str) -> Path:
    path = _failed_synthesis_path(initiative_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def synthesize_initiative_strategy(
    *,
    initiative_id: UUID,
    organization_id: UUID,
) -> dict[str, Any]:
    """Read the six inputs, call Anthropic once, validate the YAML
    front-matter, write the strategy doc, and transition the initiative
    to ``strategy_ready``.

    Returns ``{path, model, tokens_used, cache_read_input_tokens,
    cache_creation_input_tokens}``.

    On malformed YAML the synthesizer retries once with a corrective
    follow-up. If the retry also fails the initiative is transitioned to
    ``failed`` and the raw model output is persisted to
    ``data/initiatives/<id>/failed_synthesis.md`` for inspection.
    """
    initiative = await _load_initiative(initiative_id)
    if initiative["organization_id"] != organization_id:
        raise StrategySynthesizerError(
            f"initiative {initiative_id} does not belong to org {organization_id}"
        )

    brand = await _load_brand(initiative["brand_id"])
    partner = await _load_partner(initiative["partner_id"])
    contract = await _load_partner_contract(initiative["partner_contract_id"])
    brand_files = _load_brand_files(_slugify_brand_name(brand["name"]))

    # Audience descriptor — best-effort.
    descriptor: dict[str, Any] | None
    try:
        descriptor = await dex_client.get_audience_descriptor(
            initiative["data_engine_audience_id"]
        )
    except dex_client.DexClientError as exc:
        logger.warning(
            "dex.get_audience_descriptor failed during synthesis "
            "for initiative=%s err=%r",
            initiative_id, exc,
        )
        descriptor = None

    partner_research_payload = await _fetch_exa_payload(
        initiative.get("partner_research_ref")
    )
    strategic_context_payload = await _fetch_exa_payload(
        initiative.get("strategic_context_research_ref")
    )

    system_blocks = _build_system_blocks(brand=brand, brand_files=brand_files)
    generated_at = datetime.now(UTC).isoformat()
    user_msg = _build_user_message(
        initiative_id=initiative_id,
        generated_at=generated_at,
        audience_descriptor=descriptor,
        partner=partner,
        contract=contract,
        partner_research_payload=partner_research_payload,
        strategic_context_payload=strategic_context_payload,
    )

    messages: list[dict[str, Any]] = [user_msg]
    response = await anthropic_client.complete(
        system=system_blocks,
        messages=messages,
        max_tokens=8192,
    )

    text = response["text"] or ""
    parsed = _parse_front_matter(text)
    if parsed is not None:
        fm, _body = parsed
        ok, err = _front_matter_valid(fm)
    else:
        ok, err = False, "front-matter not parseable"

    if not ok:
        # Retry once with a corrective follow-up. The original assistant
        # turn must be appended verbatim — no edits, no truncation —
        # so the model has full context for the correction request.
        messages.append({"role": "assistant", "content": text})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your previous response had invalid YAML front-matter "
                    f"(reason: {err}). Re-emit the strategy document with "
                    "STRICT YAML 1.1 front-matter following the exact "
                    "schema in the system prompt. Begin your output with "
                    "the literal string `---` and no preamble."
                ),
            }
        )
        retry = await anthropic_client.complete(
            system=system_blocks,
            messages=messages,
            max_tokens=8192,
        )
        retry_text = retry["text"] or ""
        retry_parsed = _parse_front_matter(retry_text)
        if retry_parsed is not None:
            fm, _body = retry_parsed
            ok, err = _front_matter_valid(fm)
        else:
            ok, err = False, "front-matter not parseable on retry"

        if not ok:
            failed_path = _write_failed_file(
                initiative_id,
                "<original_attempt>\n"
                + text
                + "\n</original_attempt>\n\n"
                "<retry_attempt>\n"
                + retry_text
                + "\n</retry_attempt>\n",
            )
            try:
                await gtm_svc.transition_status(
                    initiative_id,
                    new_status="failed",
                    history_event={
                        "kind": "transition",
                        "trigger": "strategy_synthesizer",
                        "reason": err,
                        "failed_synthesis_path": str(failed_path),
                    },
                )
            except gtm_svc.InvalidStatusTransition:
                logger.exception(
                    "could not transition initiative %s to failed",
                    initiative_id,
                )
            raise StrategySynthesizerError(
                f"synthesis failed validation twice: {err}"
            )

        # Retry succeeded; use retry response from here.
        text = retry_text
        response = retry

    path = _write_strategy_file(initiative_id, text)
    await gtm_svc.set_campaign_strategy_path(initiative_id, str(path))
    try:
        await gtm_svc.transition_status(
            initiative_id,
            new_status="strategy_ready",
            history_event={
                "kind": "transition",
                "trigger": "strategy_synthesizer",
                "model": response.get("model"),
                "campaign_strategy_path": str(path),
                "usage": response.get("usage", {}),
            },
        )
    except gtm_svc.InvalidStatusTransition as exc:
        # Don't raise — the artifact is on disk and the path is
        # persisted; future runs will see strategy_ready next time
        # the dispatcher fires. Log for visibility.
        logger.warning(
            "transition to strategy_ready refused for initiative=%s err=%r",
            initiative_id, exc,
        )

    usage = response.get("usage", {}) or {}
    return {
        "path": str(path),
        "model": response.get("model"),
        "tokens_used": (usage.get("input_tokens", 0) or 0)
        + (usage.get("output_tokens", 0) or 0),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "synthesized_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "StrategySynthesizerError",
    "synthesize_initiative_strategy",
]
