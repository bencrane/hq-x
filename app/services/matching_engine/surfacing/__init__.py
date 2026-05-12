"""Surfacing channel handlers.

Each handler accepts (Match, SurfacingRule, intent_dict) and returns a
Surfacing object (already persisted, with surfacing_id set). The engine's
`_apply_surfacing_rule` dispatches by `SurfacingRule.channels`.

cold_email_handoff is a STUB in v1: builds the payload, logs it, returns a
Surfacing with outcome='pending'. The real emailbison webhook call is a
follow-up.
"""

from app.services.matching_engine.surfacing import (
    cold_email_handoff,
    operator_queue,
    portal,
)

__all__ = ["portal", "operator_queue", "cold_email_handoff"]
