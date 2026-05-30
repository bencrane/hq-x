"""Generic Modal-app scaffolds — Pattern A/B/C topologies factored out of
the 99-app portfolio per the 2026-05-25 systemic Modal critique (audit §"P1-4").

Per Böckeler's Ashby's Law: a regulator must have at least as much variety as
the system it governs. The 99-app portfolio of mostly-identical Pattern A
Lance-emit crons (each ~150-250 LOC of identical boilerplate wrapping ~30
LOC of derivation logic) violates this principle. These scaffolds reduce
the variety the operator + agents have to reason about: each Pattern A app
is now ~30 LOC of config + a single `build_pattern_a_lance_emit_app(...)`
call.
"""

from .pattern_a_lance_emit import (
    ORCHESTRATOR_SECRETS,
    PatternALanceEmitConfig,
    build_image,
    run_emit,
)

__all__ = [
    "ORCHESTRATOR_SECRETS",
    "PatternALanceEmitConfig",
    "build_image",
    "run_emit",
]
