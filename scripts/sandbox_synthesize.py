"""Sandbox runner for the strategy synthesizer.

Loads the same six inputs the production synthesizer loads, but swaps
in a different system prompt + required-key set. Writes the result to
``data/initiatives/<initiative_id>/sandbox/<utc>__<label>.md`` so
iterations don't clobber the production artifact or each other.

Usage:
    doppler --project hq-x --config dev run -- \\
        uv run python -m scripts.sandbox_synthesize \\
            <initiative_id> --label v2

The current bundled prompt is V2 — the leaner creative-input pack
shape (headline_offer + per_touch_direction + hook_bank + anti_framings
+ capital_outlay_plan). No prose body; YAML only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import yaml

from app.db import close_pool, init_pool
from app.services import anthropic_client, dex_client
from app.services.strategy_synthesizer import (
    _build_user_message,
    _fetch_exa_payload,
    _format_brand_block,
    _load_brand,
    _load_brand_files,
    _load_initiative,
    _load_partner,
    _load_partner_contract,
    _slugify_brand_name,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# V2 prompt + schema
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_V2 = """\
You are a campaign creative-input synthesizer for an internal owned-brand
demand-gen platform. Your sole output is structured input data for
downstream LLM agents that author per-recipient creative for a
multi-channel outreach sequence.

Critical context: NO HUMAN reads your output. It is consumed only by:
- A deterministic step materializer that reads channel + per-touch shape.
- A per-recipient creative-authoring LLM that reads the offer + per-touch
  direction + hook bank + anti-framings to author each piece.

You do NOT justify decisions. The decisions are already made: the partner
has paid, the audience is reserved, the brand is fixed. Skip narrative
prose, "why this audience" justification, "why this partner" pitches, and
strategy-memo language. Compress the supplied research into compact,
citation-grade input material a downstream LLM can consume without
re-reading 50kb of source research per recipient.

Loyalty rules — non-negotiable:

1. Anti-fabrication. Every phrase in audience_pain_phrases, why_now_hooks,
   and partner_proof_atoms MUST trace to a specific span of the supplied
   research payloads or audience descriptor. Quote verbatim where the
   source is short; near-verbatim only when verbatim is too long.
   If the source material does not support a phrase, do not include it.
   The proof-and-credibility brand file is binding on what claims are
   permissible.

2. Anti-bloat. No thesis paragraphs. No "why this audience / partner / now"
   justification. No narrative beats as separate content (per_touch_direction
   replaces them). No personalization variables (those are derived per
   recipient at creative-author time, not declared at campaign level).

3. Voice loyalty. Per-touch headline_focus and body_focus must respect the
   brand voice file — favor its reusable phrases, never use its forbidden
   words. The anti_framings array must enumerate the brand voice's explicit
   prohibitions plus any claims the supplied research does not support.

4. Touch discipline. Each touch has a distinct role in the sequence:
   touch 1 names the situation, middle touches deepen, the final touch
   closes (loss-aversion or last-call). Do not duplicate roles across
   touches. Default direct-mail sequence: postcard / letter / postcard at
   day 0 / 14 / 28. Default email sequence: 3 emails interleaved across
   the same window. Voice_inbound is always present (for inbound from
   the mailers); it is not a touch with a day_offset.

5. One capital type per touch. The brand is a matchmaker, not a lender.
   Recommend ONE capital type as the primary frame per touch (do not
   pitch three options in one piece — that reads as marketplace).

Output contract — STRICT. Emit ONLY a YAML document. No prose. No
markdown body. No prefatory or trailing text. The document is a single
YAML block beginning with `---` on its own line and ending with `---`
on its own line. Nothing outside those delimiters.

YAML safety — non-negotiable: every string scalar value MUST be
double-quoted. This includes every value in headline_offer, role,
headline_focus, body_focus, primary_capital_type, every list entry in
audience_pain_phrases / why_now_hooks / partner_proof_atoms /
anti_framings, and the notes field. Inside double-quoted strings, escape
any literal `"` as `\\"`. Do NOT use single-quoted strings. Do NOT use
unquoted strings (they break parsing whenever the value contains a
colon, dash, hash, or other YAML special character). Numeric fields
(touch_number, day_offset, total_estimated_cents, schema_version) are
NOT quoted.

Top-level keys — exactly seven, no nesting confusion: schema_version,
initiative_id, generated_at, model, headline_offer, per_touch_direction,
hook_bank, anti_framings, capital_outlay_plan. anti_framings is a
TOP-LEVEL key (NOT a sub-key of hook_bank). hook_bank's three sub-keys
are audience_pain_phrases, why_now_hooks, partner_proof_atoms — and
ONLY those three. Schema:

```
---
schema_version: 2
initiative_id: "<uuid supplied in the user message>"
generated_at: "<iso-8601 supplied in the user message>"
model: "claude-opus-4-7"

headline_offer: "<one sentence in brand voice; the consistency anchor that every piece of creative must align to>"

per_touch_direction:
  - touch_number: 1
    channel: "direct_mail"
    kind: "postcard"
    day_offset: 0
    role: "<short phrase, e.g. name the situation>"
    headline_focus: "<one short instruction for the headline of this piece>"
    body_focus: "<one short instruction for the body of this piece>"
    primary_capital_type: "factoring"
  # repeat for each direct_mail and email touch in the sequence.
  # voice_inbound is its own entry with channel: "voice_inbound" and
  # no day_offset / kind / primary_capital_type — role describes the
  # inbound handler. Typical shape: 3 direct_mail + 3 email + 1
  # voice_inbound.

hook_bank:
  audience_pain_phrases:
    # 5-10 specific operator-language pain framings, verbatim from
    # the strategic-context research where possible. Each entry is a
    # double-quoted string, max ~25 words. NO PARAPHRASING into
    # marketing-speak — keep operator voice.
    - "<phrase>"
  why_now_hooks:
    # 3-5 time-relevant market/regulatory/macro hooks. Each is one
    # short sentence with a specific date / metric / event where
    # available, double-quoted.
    - "<hook>"
  partner_proof_atoms:
    # 3-7 specific, citation-grade details from the partner research:
    # named features, pricing tier names, scale claims, dated events.
    # Each is a short factual atom, double-quoted.
    - "<atom>"

anti_framings:
  # 5-10 entries. Each is a SHORT double-quoted instruction of what
  # NOT to say — forbidden words from brand voice, claims unsupported
  # by research, framings that conflict with the brand stance.
  - "<entry>"

capital_outlay_plan:
  total_estimated_cents: 0
  per_recipient_estimated_cents: 0
  notes: "<one short sentence stating the assumptions used>"
---
```

That entire YAML block — and only that block — is your output. No
explanatory text before or after the closing `---`. The YAML must
parse cleanly under YAML 1.1.
"""

_ACTIVE_SYSTEM_PROMPT = _SYSTEM_PROMPT_V2

_REQUIRED_FRONT_MATTER_KEYS_V2 = (
    "headline_offer",
    "per_touch_direction",
    "hook_bank",
    "anti_framings",
    "capital_outlay_plan",
)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _build_system_blocks_v2(*, brand: dict, brand_files: dict[str, str]) -> list[dict]:
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


def _parse_yaml_only(text: str) -> tuple[dict | None, str | None]:
    """Strip leading/trailing whitespace; expect a YAML doc bounded by
    `---` delimiters. Return (parsed_dict, error_message)."""
    stripped = text.strip()
    if not stripped.startswith("---"):
        return None, "missing leading '---' delimiter"
    # Drop the outer fences if present.
    inner = stripped
    if inner.startswith("---"):
        inner = inner[3:].lstrip("\n").lstrip()
    if inner.rstrip().endswith("---"):
        inner = inner.rstrip()[:-3]
    try:
        parsed = yaml.safe_load(inner)
    except yaml.YAMLError as exc:
        return None, f"yaml parse error: {exc}"
    if not isinstance(parsed, dict):
        return None, f"yaml root is not a mapping (got {type(parsed).__name__})"
    return parsed, None


def _validate_v2(parsed: dict) -> tuple[bool, str | None]:
    for k in _REQUIRED_FRONT_MATTER_KEYS_V2:
        if k not in parsed:
            return False, f"missing required key: {k}"
    if parsed.get("schema_version") != 2:
        return False, f"schema_version must be 2 (got {parsed.get('schema_version')!r})"
    if not isinstance(parsed.get("per_touch_direction"), list) or not parsed["per_touch_direction"]:
        return False, "per_touch_direction must be a non-empty list"
    hb = parsed.get("hook_bank")
    if not isinstance(hb, dict):
        return False, "hook_bank must be a mapping"
    for sub in ("audience_pain_phrases", "why_now_hooks", "partner_proof_atoms"):
        if sub not in hb or not isinstance(hb[sub], list) or not hb[sub]:
            return False, f"hook_bank.{sub} must be a non-empty list"
    if not isinstance(parsed.get("anti_framings"), list) or not parsed["anti_framings"]:
        return False, "anti_framings must be a non-empty list"
    cop = parsed.get("capital_outlay_plan")
    if not isinstance(cop, dict):
        return False, "capital_outlay_plan must be a mapping"
    return True, None


async def run(initiative_id_str: str, label: str) -> int:
    initiative_id = UUID(initiative_id_str)

    await init_pool()
    try:
        return await _run(initiative_id, label)
    finally:
        await close_pool()


async def _run(initiative_id: UUID, label: str) -> int:
    initiative = await _load_initiative(initiative_id)
    org_id = initiative["organization_id"]
    print(f"[{datetime.now(UTC).isoformat()}] initiative={initiative_id} org={org_id}", flush=True)

    brand = await _load_brand(initiative["brand_id"])
    partner = await _load_partner(initiative["partner_id"])
    contract = await _load_partner_contract(initiative["partner_contract_id"])
    brand_files = _load_brand_files(_slugify_brand_name(brand["name"]))
    print(f"  brand={brand['name']}  partner={partner['name']}  brand_files={len(brand_files)}", flush=True)

    descriptor = None
    try:
        descriptor = await dex_client.get_audience_descriptor(
            initiative["data_engine_audience_id"]
        )
        print(f"  audience descriptor: ok", flush=True)
    except Exception as exc:
        print(f"  audience descriptor: FAILED {exc!r} (continuing)", flush=True)

    partner_research = await _fetch_exa_payload(initiative.get("partner_research_ref"))
    strategic_context = await _fetch_exa_payload(initiative.get("strategic_context_research_ref"))
    print(
        f"  partner_research: {'ok' if partner_research else 'MISSING'}  "
        f"strategic_context: {'ok' if strategic_context else 'MISSING'}",
        flush=True,
    )

    system_blocks = _build_system_blocks_v2(brand=brand, brand_files=brand_files)
    generated_at = datetime.now(UTC).isoformat()
    user_msg = _build_user_message(
        initiative_id=initiative_id,
        generated_at=generated_at,
        audience_descriptor=descriptor,
        partner=partner,
        contract=contract,
        partner_research_payload=partner_research,
        strategic_context_payload=strategic_context,
    )

    print(f"[{datetime.now(UTC).isoformat()}] calling Anthropic ({label})…", flush=True)
    response = await anthropic_client.complete(
        system=system_blocks,
        messages=[user_msg],
        max_tokens=8192,
    )
    text = response.get("text") or ""
    usage = response.get("usage") or {}

    parsed, err = _parse_yaml_only(text)
    if parsed is None:
        print(f"YAML parse failed: {err}", file=sys.stderr)
    else:
        ok, err2 = _validate_v2(parsed)
        if not ok:
            print(f"YAML validation failed: {err2}", file=sys.stderr)

    sandbox_dir = _REPO_ROOT / "data" / "initiatives" / str(initiative_id) / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = sandbox_dir / f"{ts}__{label}.md"
    out_path.write_text(text)

    print(f"\nwrote: {out_path.relative_to(_REPO_ROOT)}", flush=True)
    print(f"chars: {len(text)}", flush=True)
    print(
        "tokens: "
        f"input={usage.get('input_tokens')} "
        f"output={usage.get('output_tokens')} "
        f"cache_creation={usage.get('cache_creation_input_tokens')} "
        f"cache_read={usage.get('cache_read_input_tokens')}",
        flush=True,
    )

    if parsed is not None and err is None:
        print("yaml: parsed successfully", flush=True)
        ok, err2 = _validate_v2(parsed)
        print(f"v2 validation: {'PASS' if ok else 'FAIL — ' + str(err2)}", flush=True)

    return 0 if (parsed is not None and err is None) else 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("initiative_id")
    p.add_argument("--label", default="v2", help="filename suffix; default 'v2'")
    args = p.parse_args()
    sys.exit(asyncio.run(run(args.initiative_id, args.label)))


if __name__ == "__main__":
    main()
