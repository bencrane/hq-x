"""Reply classifier — classifies inbound supply-side reply text.

Cluster 3 input gate. Reads the reply body (plus minimal context) and
emits one of the six classifications enumerated on
``business.email_reply_classifications.classification``:

    positive | negative | unsubscribe | question | auto_reply | unclassified

Two backends:

* ``anthropic`` (default) — Claude Haiku via ``anthropic_client.complete``.
  Per-call cost is small enough that we never aggregate; one classifier
  call per inbound reply.
* ``stub`` — deterministic local heuristic for simulation / offline test.
  Triggered when ``settings.ANTHROPIC_API_KEY`` is unset OR when the
  caller passes ``mode='stub'`` explicitly (the simulation harness does).

Output shape is identical across backends so callers don't branch on
mode. The ``evidence`` dict goes verbatim into
``email_reply_classifications.evidence`` for forensic replay.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from app.config import settings
from app.services import anthropic_client

logger = logging.getLogger(__name__)


VALID_CLASSIFICATIONS = (
    "positive",
    "negative",
    "unsubscribe",
    "question",
    "auto_reply",
    "unclassified",
)

ClassifierMode = Literal["anthropic", "stub", "auto"]


_CLASSIFIER_SYSTEM_PROMPT = """\
You classify inbound replies to a B2B cold-email outreach.

The outreach went out as part of a "Cluster 2" supply-side opt-in flow:
we (the operator) emailed a supply-side recipient on behalf of a paying
demand-side partner who reserved access to that audience. The recipient
is replying. You decide what kind of reply this is.

Output exactly one classification, one of:

  positive      — explicit interest, says yes, asks to talk, asks for
                  more info as a buyer would (not as an objection-handler)
  negative      — explicit no, "not interested", "not a fit", "remove me"
                  if the language is annoyed but not unsubscribe-shaped
  unsubscribe   — explicit unsubscribe / opt-out / "stop emailing me"
  question      — non-committal, asks clarification, "what is this", neutral
  auto_reply    — out-of-office, vacation, mail-delivery-failed, bot reply
  unclassified  — anything else, including ambiguous, hostile-but-engaged

Output a JSON object with two fields:

  {"classification": "<one of the above>",
   "rationale": "<short single-sentence justification>"}

No other text. No code fences. Just the JSON.
"""


async def classify(
    *,
    reply_text: str,
    reply_subject: str | None = None,
    reply_from_email: str | None = None,
    mode: ClassifierMode = "auto",
    model: str | None = None,
) -> dict[str, Any]:
    """Classify a reply. Returns ``{classification, classified_by, evidence}``.

    ``classified_by`` is ``'agent'`` for Anthropic-backed runs, ``'rule'``
    for stub runs. Both go straight into the
    ``email_reply_classifications`` row.
    """
    chosen_mode: ClassifierMode = mode
    if chosen_mode == "auto":
        chosen_mode = "anthropic" if settings.ANTHROPIC_API_KEY else "stub"

    if chosen_mode == "stub":
        return _classify_stub(
            reply_text=reply_text,
            reply_subject=reply_subject,
            reply_from_email=reply_from_email,
        )

    return await _classify_anthropic(
        reply_text=reply_text,
        reply_subject=reply_subject,
        reply_from_email=reply_from_email,
        model=model,
    )


async def _classify_anthropic(
    *,
    reply_text: str,
    reply_subject: str | None,
    reply_from_email: str | None,
    model: str | None,
) -> dict[str, Any]:
    user_blob_parts: list[str] = []
    if reply_from_email:
        user_blob_parts.append(f"<from>{reply_from_email}</from>")
    if reply_subject:
        user_blob_parts.append(f"<subject>{reply_subject}</subject>")
    user_blob_parts.append(f"<reply_body>\n{reply_text or ''}\n</reply_body>")

    user_msg = "\n".join(user_blob_parts)

    try:
        result = await anthropic_client.complete(
            system=_CLASSIFIER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
            model=model or "claude-haiku-4-5-20251001",
            max_tokens=256,
        )
    except anthropic_client.AnthropicClientError as exc:
        logger.warning("reply_classifier anthropic failed; stub fallback: %s", exc)
        stub = _classify_stub(
            reply_text=reply_text,
            reply_subject=reply_subject,
            reply_from_email=reply_from_email,
        )
        stub["evidence"]["fallback_reason"] = str(exc)[:200]
        return stub

    raw = (result.get("text") or "").strip()
    parsed: dict[str, Any] | None = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError:
                parsed = None

    classification = "unclassified"
    rationale = "could not parse classifier output"
    if isinstance(parsed, dict):
        candidate = str(parsed.get("classification") or "").strip().lower()
        if candidate in VALID_CLASSIFICATIONS:
            classification = candidate
            rationale = str(parsed.get("rationale") or "")[:500]

    return {
        "classification": classification,
        "classified_by": "agent",
        "evidence": {
            "model": result.get("model"),
            "rationale": rationale,
            "raw_output": raw[:1000],
            "usage": result.get("usage", {}),
        },
    }


_POSITIVE_HINTS = (
    "yes interested",
    "let's talk",
    "lets talk",
    "happy to chat",
    "sure thing",
    "sounds good",
    "open to learning",
    "send a calendar",
    "book a time",
    "schedule a call",
)
_NEGATIVE_HINTS = (
    "not interested",
    "not a fit",
    "no thanks",
    "we already have",
    "not now",
    "pass",
    "we're good",
)
_UNSUB_HINTS = (
    "unsubscribe",
    "remove me",
    "stop emailing",
    "take me off",
    "do not email",
    "opt out",
    "opt-out",
)
_AUTO_HINTS = (
    "out of office",
    "out-of-office",
    "ooo",
    "automatic reply",
    "auto-reply",
    "auto reply",
    "i am currently out",
    "delivery has failed",
    "mail delivery",
    "mailer-daemon",
    "undeliverable",
)
_QUESTION_HINTS = (
    "what is this",
    "who are you",
    "how did you get",
    "where did you get",
    "can you clarify",
    "tell me more",
    "what does this look like",
)


def _classify_stub(
    *,
    reply_text: str,
    reply_subject: str | None,
    reply_from_email: str | None,
) -> dict[str, Any]:
    blob = " ".join(
        filter(
            None,
            [
                (reply_subject or "").lower(),
                (reply_text or "").lower(),
                (reply_from_email or "").lower(),
            ],
        )
    )

    if any(hint in blob for hint in _AUTO_HINTS):
        return {
            "classification": "auto_reply",
            "classified_by": "rule",
            "evidence": {"matched": "auto_hints", "rule": "stub"},
        }
    if any(hint in blob for hint in _UNSUB_HINTS):
        return {
            "classification": "unsubscribe",
            "classified_by": "rule",
            "evidence": {"matched": "unsub_hints", "rule": "stub"},
        }
    if any(hint in blob for hint in _QUESTION_HINTS):
        return {
            "classification": "question",
            "classified_by": "rule",
            "evidence": {"matched": "question_hints", "rule": "stub"},
        }
    if any(hint in blob for hint in _POSITIVE_HINTS):
        return {
            "classification": "positive",
            "classified_by": "rule",
            "evidence": {"matched": "positive_hints", "rule": "stub"},
        }
    if any(hint in blob for hint in _NEGATIVE_HINTS):
        return {
            "classification": "negative",
            "classified_by": "rule",
            "evidence": {"matched": "negative_hints", "rule": "stub"},
        }
    return {
        "classification": "unclassified",
        "classified_by": "rule",
        "evidence": {"matched": "none", "rule": "stub"},
    }


__all__ = ["classify", "VALID_CLASSIFICATIONS", "ClassifierMode"]
