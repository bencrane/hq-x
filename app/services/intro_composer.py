"""Intro composer — Cluster 3 outbound message generation.

Reads the full bundle (recipient, partner, partner research artifact,
audience context artifact, original outreach, positive reply text,
recipient gestalt if present, model emails for tone reference) and
produces ``{subject, body_text, body_html}``.

Tone: merchant-banker. Less lead-gen-vendor. The structural template:

    "John and Barry — connecting you two because timing here made sense.
     John's <one-line situation summary>. Barry's <one-line value prop
     for that situation>. Took the liberty of patching you in directly..."

Two backends, same backend selection contract as ``reply_classifier``:

* ``anthropic`` — Claude Sonnet via ``anthropic_client.complete``.
* ``stub`` — deterministic merchant-banker template render. Used when
  ``settings.ANTHROPIC_API_KEY`` is missing or in simulation. The stub
  output is structurally correct and good-enough for end-to-end test
  but obviously will not have agent-quality phrasing.

Output contract — always returns dict with these keys:

    {
      "subject": str,
      "body_text": str,
      "body_html": str | None,
      "model": str | None,
      "usage": dict | None,
      "rationale_md": str | None,   # composer's own notes (anthropic mode)
      "backend": "anthropic" | "stub",
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from app.config import settings
from app.services import anthropic_client

logger = logging.getLogger(__name__)


ComposerMode = Literal["anthropic", "stub", "auto"]


_COMPOSER_SYSTEM_PROMPT = """\
You compose a single intro email that connects two parties:

  • A demand-side partner (the "buyer") who has prepaid the operator
    for access to a curated audience and is looking for high-fit leads
    in that audience.
  • A supply-side recipient (the "seller-of-themselves" — the audience
    member) who has just replied positively to a Cluster 2 outreach
    message saying they're interested in talking.

This is the third message in a three-message arc; the recipient already
opted in. You are not selling them anything. You are introducing two
people who both have reason to talk.

The voice is merchant-banker, not lead-gen-vendor:

  • Specific. Cite one concrete thing about each party.
  • Spare. No over-explaining, no "I wanted to follow up", no boilerplate.
  • Confident. The intro itself is the value — both sides see why
    timing aligns. The recipient already opted in; you don't need to
    re-pitch.
  • Warm but not chummy. First-name tone. No "hope this finds you well",
    no exclamation points.

Structural template (adapt freely; do not output the literal placeholders):

  Subject: [Recipient first name] x [Partner first name or company]

  Body:
    [Recipient first name] and [Partner first name],

    Connecting you two because the timing made sense.

    [One-line situational summary about Recipient — what's salient about
    where they are right now, drawn from gestalt + reply text. Concrete.]

    [One-line value summary about Partner — what they specifically do that
    matches Recipient's situation, drawn from partner_research artifact.
    Concrete.]

    Took the liberty of patching you in directly. [Optional: one
    sentence of next-step framing — "compare notes when you have a
    minute" / "I'll get out of the way."]

    — [Operator first name] (default: "Ben")

You receive an XML-tagged input bundle:

  <recipient>{first_name, last_name, email, company}</recipient>
  <recipient_gestalt>...markdown...</recipient_gestalt>      # may be empty
  <reply_text>...the recipient's positive reply text...</reply_text>
  <partner>{name, primary_contact_name}</partner>
  <partner_research>...markdown about the partner...</partner_research>  # may be empty
  <audience_context>...markdown about the audience...</audience_context>  # may be empty
  <model_emails>...0-3 reference emails...</model_emails>     # may be empty
  <operator>{first_name}</operator>

Output exactly this shape (XML-tagged, no JSON, no code fences):

  <subject>...</subject>
  <body_text>
  ...plain-text email body...
  </body_text>

If the bundle is missing partner_research or recipient_gestalt, do not
hallucinate — keep the situational/value lines briefer and lean on what
you have. Better short and true than long and embellished.
"""


async def compose(
    *,
    recipient: dict[str, Any] | None,
    partner: dict[str, Any] | None,
    partner_research_md: str | None,
    audience_context_md: str | None,
    recipient_gestalt_md: str | None,
    reply_text: str | None,
    reply_subject: str | None,
    model_emails: list[dict[str, Any]] | None,
    operator_first_name: str = "Ben",
    mode: ComposerMode = "auto",
    model: str | None = None,
) -> dict[str, Any]:
    chosen_mode: ComposerMode = mode
    if chosen_mode == "auto":
        chosen_mode = "anthropic" if settings.ANTHROPIC_API_KEY else "stub"

    if chosen_mode == "stub":
        return _compose_stub(
            recipient=recipient,
            partner=partner,
            partner_research_md=partner_research_md,
            recipient_gestalt_md=recipient_gestalt_md,
            reply_text=reply_text,
            operator_first_name=operator_first_name,
        )

    return await _compose_anthropic(
        recipient=recipient,
        partner=partner,
        partner_research_md=partner_research_md,
        audience_context_md=audience_context_md,
        recipient_gestalt_md=recipient_gestalt_md,
        reply_text=reply_text,
        reply_subject=reply_subject,
        model_emails=model_emails,
        operator_first_name=operator_first_name,
        model=model,
    )


async def _compose_anthropic(
    *,
    recipient: dict[str, Any] | None,
    partner: dict[str, Any] | None,
    partner_research_md: str | None,
    audience_context_md: str | None,
    recipient_gestalt_md: str | None,
    reply_text: str | None,
    reply_subject: str | None,
    model_emails: list[dict[str, Any]] | None,
    operator_first_name: str,
    model: str | None,
) -> dict[str, Any]:
    parts: list[str] = []
    parts.append(_xml_block("recipient", _stringify(recipient or {})))
    parts.append(
        _xml_block(
            "recipient_gestalt", recipient_gestalt_md or "(no gestalt available)"
        )
    )
    parts.append(_xml_block("reply_text", reply_text or "(no reply text captured)"))
    if reply_subject:
        parts.append(_xml_block("reply_subject", reply_subject))
    parts.append(_xml_block("partner", _stringify(partner or {})))
    parts.append(
        _xml_block(
            "partner_research",
            partner_research_md or "(no partner research artifact yet)",
        )
    )
    parts.append(
        _xml_block(
            "audience_context",
            audience_context_md or "(no audience context artifact yet)",
        )
    )
    parts.append(_xml_block("model_emails", _format_model_emails(model_emails)))
    parts.append(_xml_block("operator", f'{{"first_name": "{operator_first_name}"}}'))

    user_msg = "\n".join(parts)

    try:
        result = await anthropic_client.complete(
            system=_COMPOSER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            model=model or settings.ANTHROPIC_DEFAULT_MODEL or "claude-opus-4-7",
            max_tokens=2048,
        )
    except anthropic_client.AnthropicClientError as exc:
        logger.warning("intro_composer anthropic failed; stub fallback: %s", exc)
        stub = _compose_stub(
            recipient=recipient,
            partner=partner,
            partner_research_md=partner_research_md,
            recipient_gestalt_md=recipient_gestalt_md,
            reply_text=reply_text,
            operator_first_name=operator_first_name,
        )
        stub["fallback_reason"] = str(exc)[:200]
        return stub

    raw = (result.get("text") or "").strip()
    subject, body_text = _parse_composer_output(raw)
    if subject is None or body_text is None:
        logger.warning(
            "intro_composer output unparseable; stub fallback. raw=%s",
            raw[:300],
        )
        stub = _compose_stub(
            recipient=recipient,
            partner=partner,
            partner_research_md=partner_research_md,
            recipient_gestalt_md=recipient_gestalt_md,
            reply_text=reply_text,
            operator_first_name=operator_first_name,
        )
        stub["fallback_reason"] = "unparseable_anthropic_output"
        stub["raw_output"] = raw[:500]
        return stub

    return {
        "subject": subject,
        "body_text": body_text,
        "body_html": None,
        "model": result.get("model"),
        "usage": result.get("usage", {}),
        "rationale_md": None,
        "backend": "anthropic",
    }


def _compose_stub(
    *,
    recipient: dict[str, Any] | None,
    partner: dict[str, Any] | None,
    partner_research_md: str | None,
    recipient_gestalt_md: str | None,
    reply_text: str | None,
    operator_first_name: str,
) -> dict[str, Any]:
    rec_first = (recipient or {}).get("first_name") or "there"
    partner_name = (partner or {}).get("name") or "our partner"
    partner_contact = (partner or {}).get("primary_contact_name") or partner_name
    partner_contact_first = partner_contact.split()[0] if partner_contact else partner_name

    situational = _first_meaningful_line(recipient_gestalt_md or reply_text or "")
    value = _first_meaningful_line(partner_research_md or "")

    subject = f"{rec_first} x {partner_contact_first}"
    lines = [
        f"{rec_first} and {partner_contact_first},",
        "",
        "Connecting you two because the timing made sense.",
        "",
    ]
    if situational:
        lines.append(situational)
        lines.append("")
    lines.append(
        f"{partner_contact_first} is at {partner_name}"
        + (f" — {value}" if value else ".")
    )
    lines.append("")
    lines.append(
        "Took the liberty of patching you in directly. Compare notes when "
        "you have a minute."
    )
    lines.append("")
    lines.append(f"— {operator_first_name}")

    return {
        "subject": subject,
        "body_text": "\n".join(lines),
        "body_html": None,
        "model": None,
        "usage": None,
        "rationale_md": None,
        "backend": "stub",
    }


def _xml_block(tag: str, body: str) -> str:
    return f"<{tag}>\n{body}\n</{tag}>"


def _stringify(d: dict[str, Any]) -> str:
    if not d:
        return "{}"
    items = []
    for k, v in d.items():
        if v is None:
            continue
        items.append(f"{k}: {v}")
    return "\n".join(items) if items else "{}"


def _format_model_emails(model_emails: list[dict[str, Any]] | None) -> str:
    if not model_emails:
        return "(no model emails seeded)"
    out = []
    for i, m in enumerate(model_emails, 1):
        out.append(f"## Model {i} — {m.get('label', '')}")
        out.append(f"Subject: {m.get('subject', '')}")
        out.append("")
        out.append(str(m.get("body", "")))
        notes = m.get("notes")
        if notes:
            out.append("")
            out.append(f"— Notes: {notes}")
        out.append("")
    return "\n".join(out).strip()


_SUBJECT_RE = re.compile(r"<subject>\s*(.*?)\s*</subject>", re.DOTALL)
_BODY_RE = re.compile(r"<body_text>\s*(.*?)\s*</body_text>", re.DOTALL)


def _parse_composer_output(raw: str) -> tuple[str | None, str | None]:
    sm = _SUBJECT_RE.search(raw)
    bm = _BODY_RE.search(raw)
    if sm and bm:
        return sm.group(1).strip(), bm.group(1).strip()
    return None, None


def _first_meaningful_line(text: str | None, max_len: int = 240) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned and not cleaned.startswith("(") and len(cleaned) > 10:
            return cleaned[:max_len]
    return ""


__all__ = ["compose", "ComposerMode"]
