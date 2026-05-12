"""Cold-email handoff surfacing — STUB for the emailbison webhook.

In the production flow (post-scaffold), this handler:
  1. Builds an auto-narrative (LLM-summarized partner intent + matched entity
     profile from the data lake).
  2. POSTs to the configured emailbison webhook with the match metadata +
     narrative + operator-approval flag.
  3. Sets surfacing outcome based on the webhook response.

The scaffold version:
  - Builds a PLACEHOLDER narrative (templated text — no LLM call).
  - Logs the would-be payload at INFO level.
  - Persists the surfacing row with outcome='pending'.

Operator approval flow: when the relationship's `surfacing_rule.operator_approval_required`
is true, the surfacing stays in `outcome='pending'` until the operator hits
`POST /api/v1/operator/match-queue/{surfacing_id}/approve`, which flips
outcome to 'sent' (and in the production version actually fires the webhook).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.services.matching_engine.models import Match, Surfacing, SurfacingRule
from app.services.matching_engine.persistence import persist_surfacing

LOG = logging.getLogger(__name__)


def _placeholder_narrative(match: Match, intent: dict[str, Any]) -> str:
    """Build the auto-narrative payload. Placeholder text — real LLM call is follow-up."""
    return (
        f"Matched entity {match.target_entity_ref} against intent "
        f"{match.source_intent_id} (kind={match.intent_kind}). "
        f"Score={match.score:.4f}. "
        f"Reasons={match.match_reasons.model_dump_json()}"
    )


async def surface_match(
    match: Match,
    rule: SurfacingRule,
    intent: dict[str, Any],
) -> Surfacing:
    """Persist the cold-email-handoff surfacing event (STUB: no webhook call)."""
    narrative = _placeholder_narrative(match, intent)
    payload = {
        "match_id": str(match.match_id),
        "target_entity_ref": match.target_entity_ref,
        "source_intent_id": str(match.source_intent_id),
        "intent_kind": match.intent_kind,
        "relationship_id": str(match.relationship_id),
        "score": match.score,
        "narrative": narrative,
        "operator_approval_required": rule.operator_approval_required,
    }
    LOG.info("cold_email_handoff STUB payload: %s", json.dumps(payload))

    # 'pending' is correct whether operator approval is required or not — the
    # real webhook call is deferred. Operator flips outcome to 'sent' via
    # the approve endpoint when ready (post-scaffold: the approve endpoint
    # also fires the actual webhook).
    surfacing = Surfacing(
        match_id=match.match_id,  # type: ignore[arg-type]
        channel="cold_email_handoff",
        surface_metadata={
            "emailbison_payload": payload,
            "auto_narrative_text": narrative,
            "webhook_called": False,
        },
        outcome="pending",
    )
    surfacing_id = await persist_surfacing(surfacing)
    surfacing.surfacing_id = surfacing_id
    return surfacing
