"""Intro verdict gate — Cluster 3 last line of defense before send.

Reads the composed intro plus the source artifacts the composer was
given, and emits a structured ship/block decision. Mirrors the
actor/verdict pattern PR #184 set up for the gtm-pipeline subagents.

Backend selection:
  * 'auto' → 'anthropic' if ANTHROPIC_API_KEY set, else 'stub'.
  * 'stub'  → deterministic checks only (length, forbidden phrases,
              required structure). No factual-grounding check (that
              requires LLM judgment over the source artifact).
  * 'anthropic' → Haiku LLM-judge with a structured rubric.

On block, the caller (cluster3_dispatch) parks the lead_transfer in
'pending_review' status, fires a critical alert, and does NOT auto-retry
with a different composer prompt — that path is the goodhart trap. The
operator approves or rejects from the dashboard.

Output shape:

    {
      "ship": True | False,
      "score": 0..10,
      "blockers": ["hallucinated_partner_fact", ...],
      "rationale": "<short>",
      "backend": "anthropic" | "stub",
      "model": str | None,
      "usage": dict | None,
    }
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from app.config import settings
from app.services import anthropic_client

logger = logging.getLogger(__name__)


VerdictMode = Literal["anthropic", "stub", "auto"]


# Rubric sketched in operator-curated terms. Operator updates this in
# code as expectations sharpen. Frozen between updates.
_VERDICT_SYSTEM_PROMPT = """\
You are the verdict agent for a B2B intro email composer. You decide
whether the composer's output is shippable.

You receive:
  <composed_intro>
    Subject: ...
    Body: ...
  </composed_intro>
  <partner_research>...</partner_research>     (markdown — may be empty)
  <recipient_gestalt>...</recipient_gestalt>   (markdown — may be empty)
  <reply_text>...</reply_text>                 (the recipient's positive reply)
  <model_emails>...</model_emails>             (operator-curated voice anchors)

Your job: ship-or-block. Block if any of these are true:

  hallucinated_partner_fact — the body cites a concrete fact about the
    partner (founder name, GMV, location, year, product, etc.) that is
    NOT supported by partner_research. Block on hallucination, period.

  hallucinated_recipient_fact — same, but for the recipient. Anything
    concrete must trace to recipient_gestalt or reply_text.

  length_over_budget — body is more than 14 lines OR more than 180 words.

  forbidden_phrase — body contains any of: 'hope this finds you well',
    'circling back', 'wanted to follow up', 'wanted to reach out',
    'just checking in', 'synergy', 'leverage' as a verb, '!', any "you
    should" advice to the recipient.

  missing_structure — body does not open with first names, OR does not
    have a one-line situational summary about recipient, OR does not
    have a one-line value summary about partner, OR does not end with
    operator first-name sign-off.

  tone_mismatch — voice is meaningfully off vs model_emails. Be lenient
    here unless it's clearly off — model_emails are the ceiling, not
    the floor.

  generic_intro — the body has no concrete specifics about either side
    (e.g., "this person is in your industry and might be a good fit"
    with no specifics). Block as too generic.

Output exactly this JSON shape, nothing else:

  {
    "ship": true|false,
    "score": <0..10 integer>,
    "blockers": ["<blocker_tag>", ...],
    "rationale": "<one sentence justification, max 200 chars>"
  }

Score 8+ = ship. Score < 8 = block, with blockers populated.
No code fences. No prose around the JSON. Just the JSON.
"""


_VALID_BLOCKERS = {
    "hallucinated_partner_fact",
    "hallucinated_recipient_fact",
    "length_over_budget",
    "forbidden_phrase",
    "missing_structure",
    "tone_mismatch",
    "generic_intro",
}


_FORBIDDEN_PHRASES = (
    "hope this finds you well",
    "circling back",
    "wanted to follow up",
    "wanted to reach out",
    "just checking in",
    "synergy",
)


async def review(
    *,
    composed_subject: str,
    composed_body_text: str,
    partner_research_md: str | None,
    recipient_gestalt_md: str | None,
    reply_text: str | None,
    model_emails: list[dict[str, Any]] | None,
    operator_first_name: str = "Ben",
    mode: VerdictMode = "auto",
    model: str | None = None,
) -> dict[str, Any]:
    chosen_mode: VerdictMode = mode
    if chosen_mode == "auto":
        chosen_mode = "anthropic" if settings.ANTHROPIC_API_KEY else "stub"

    structural = _structural_checks(
        composed_subject=composed_subject,
        composed_body_text=composed_body_text,
        operator_first_name=operator_first_name,
    )

    if chosen_mode == "stub":
        score = 10 if not structural["blockers"] else 5
        return {
            "ship": score >= 8,
            "score": score,
            "blockers": structural["blockers"],
            "rationale": structural["rationale"],
            "backend": "stub",
            "model": None,
            "usage": None,
        }

    return await _review_anthropic(
        composed_subject=composed_subject,
        composed_body_text=composed_body_text,
        partner_research_md=partner_research_md,
        recipient_gestalt_md=recipient_gestalt_md,
        reply_text=reply_text,
        model_emails=model_emails,
        structural_blockers=structural["blockers"],
        model=model,
    )


def _structural_checks(
    *, composed_subject: str, composed_body_text: str, operator_first_name: str
) -> dict[str, Any]:
    body = composed_body_text or ""
    blockers: list[str] = []

    word_count = len(body.split())
    line_count = len([l for l in body.splitlines() if l.strip()])
    if word_count > 180 or line_count > 14:
        blockers.append("length_over_budget")

    body_lower = body.lower()
    if "!" in body or any(p in body_lower for p in _FORBIDDEN_PHRASES):
        blockers.append("forbidden_phrase")
    # 'leverage' as a verb: heuristic check ("leverage X" with no following 'of')
    if re.search(r"\bleverage\s+(?!of\b)\w", body_lower):
        blockers.append("forbidden_phrase")

    sign_off_marker = f"— {operator_first_name}".lower()
    if sign_off_marker not in body_lower and f"-- {operator_first_name.lower()}" not in body_lower:
        blockers.append("missing_structure")

    return {
        "blockers": list(dict.fromkeys(blockers)),
        "rationale": ", ".join(blockers) if blockers else "structural checks pass",
    }


async def _review_anthropic(
    *,
    composed_subject: str,
    composed_body_text: str,
    partner_research_md: str | None,
    recipient_gestalt_md: str | None,
    reply_text: str | None,
    model_emails: list[dict[str, Any]] | None,
    structural_blockers: list[str],
    model: str | None,
) -> dict[str, Any]:
    parts: list[str] = []
    parts.append(
        _block(
            "composed_intro",
            f"Subject: {composed_subject}\n\nBody:\n{composed_body_text}",
        )
    )
    parts.append(
        _block(
            "partner_research",
            partner_research_md or "(no partner research artifact)",
        )
    )
    parts.append(
        _block(
            "recipient_gestalt",
            recipient_gestalt_md or "(no gestalt available)",
        )
    )
    parts.append(_block("reply_text", reply_text or "(no reply text)"))
    parts.append(_block("model_emails", _format_models(model_emails)))

    user_msg = "\n".join(parts)

    try:
        result = await anthropic_client.complete(
            system=_VERDICT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            model=model or "claude-haiku-4-5-20251001",
            max_tokens=512,
        )
    except anthropic_client.AnthropicClientError as exc:
        logger.warning("intro_verdict anthropic failed; structural-only: %s", exc)
        score = 10 if not structural_blockers else 4
        return {
            "ship": score >= 8,
            "score": score,
            "blockers": structural_blockers,
            "rationale": (
                f"anthropic unavailable; structural-only check. {exc!s:.200}"
            ),
            "backend": "stub",
            "model": None,
            "usage": None,
        }

    raw = (result.get("text") or "").strip()
    parsed = _parse_verdict(raw)
    blockers = list(
        dict.fromkeys(
            [
                b
                for b in (parsed.get("blockers") or [])
                if b in _VALID_BLOCKERS
            ]
            + structural_blockers
        )
    )
    score_val = parsed.get("score")
    score = (
        int(score_val)
        if isinstance(score_val, (int, float))
        else (4 if blockers else 8)
    )
    if blockers:
        score = min(score, 7)
    ship_val = parsed.get("ship")
    ship = bool(ship_val) if isinstance(ship_val, bool) else (score >= 8 and not blockers)
    rationale = str(parsed.get("rationale") or "")[:500] or (
        ", ".join(blockers) if blockers else "verdict ok"
    )

    return {
        "ship": ship,
        "score": score,
        "blockers": blockers,
        "rationale": rationale,
        "backend": "anthropic",
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "raw_output": raw[:1000],
    }


def _block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _format_models(model_emails: list[dict[str, Any]] | None) -> str:
    if not model_emails:
        return "(no model emails seeded)"
    out = []
    for i, m in enumerate(model_emails, 1):
        out.append(f"## Model {i} — {m.get('label', '')}")
        out.append(f"Subject: {m.get('subject', '')}")
        out.append("")
        out.append(str(m.get("body", "")))
        out.append("")
    return "\n".join(out).strip()


def _parse_verdict(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return {}


__all__ = ["review", "VerdictMode"]
