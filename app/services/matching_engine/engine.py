"""Matching-engine core.

Evaluates a relationship config against an intent source (paid_spec OR
preference, polymorphic via `intent_kind`). Persists ranked matches into
`business.matches` and surfacings into `business.match_surfacings`.

Scaffold-only: the scoring uses placeholder weights from the relationship's
JSONB `scoring_strategy`. Target population resolution is intentionally simple
— for paid_spec intents, the engine treats the signing's cohort manifest as
the candidate set (the parquet on R2 frozen at sign-time). For preferences,
the engine targets the spec's declared filter (placeholder — preferences
substrate is Phase 5.1).

The engine does NOT recompute Phase 2's evaluator at runtime — it consumes
the materialized cohort. This keeps Phase 5 lean and decouples it from the
catalog read path.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import numpy as np

from app.db import get_db_connection
from app.services.matching_engine.models import (
    Match,
    MatchReasons,
    RelationshipConfig,
    ScoringStrategy,
    Surfacing,
    SurfacingRule,
)
from app.services.matching_engine.persistence import persist_match, persist_surfacing

LOG = logging.getLogger(__name__)


# ─── Relationship config loading ─────────────────────────────────────────


async def load_relationship(relationship_id: UUID) -> RelationshipConfig:
    """Fetch a single relationship row by id."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT relationship_id, name, description, intent_source,
                       target_filter, scoring_strategy, surfacing_rule,
                       enabled, created_at, created_by_user_id
                FROM business.matching_relationships
                WHERE relationship_id = %s
                """,
                (str(relationship_id),),
            )
            row = await cur.fetchone()
    if row is None:
        raise UnknownRelationship(f"relationship {relationship_id} not found")
    return _row_to_relationship(row)


async def load_relationship_by_name(name: str) -> RelationshipConfig:
    """Fetch a single relationship row by its unique name."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT relationship_id, name, description, intent_source,
                       target_filter, scoring_strategy, surfacing_rule,
                       enabled, created_at, created_by_user_id
                FROM business.matching_relationships
                WHERE name = %s
                """,
                (name,),
            )
            row = await cur.fetchone()
    if row is None:
        raise UnknownRelationship(f"relationship name={name!r} not found")
    return _row_to_relationship(row)


async def list_active_relationships() -> list[RelationshipConfig]:
    """All enabled rows."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT relationship_id, name, description, intent_source,
                       target_filter, scoring_strategy, surfacing_rule,
                       enabled, created_at, created_by_user_id
                FROM business.matching_relationships
                WHERE enabled = TRUE
                """
            )
            rows = await cur.fetchall()
    return [_row_to_relationship(r) for r in rows]


def _row_to_relationship(row: tuple) -> RelationshipConfig:
    (
        relationship_id, name, description, intent_source,
        target_filter, scoring_strategy_json, surfacing_rule_json,
        enabled, created_at, created_by_user_id,
    ) = row
    return RelationshipConfig(
        relationship_id=relationship_id,
        name=name,
        description=description,
        intent_source=intent_source,
        target_filter=target_filter or {},
        scoring_strategy=ScoringStrategy(**(scoring_strategy_json or {})),
        surfacing_rule=SurfacingRule(**(surfacing_rule_json or {})),
        enabled=enabled,
        created_at=created_at,
        created_by_user_id=created_by_user_id,
    )


# ─── Eligible intent loading ─────────────────────────────────────────────


async def load_eligible_intents(relationship: RelationshipConfig) -> list[dict[str, Any]]:
    """Return the list of (intent_id, intent_kind, ...) the engine should
    evaluate for this relationship.

    For `intent_source = paid_specs`: queries `business.audience_spec_signings`
    that haven't expired. The scaffold returns an empty list if the table
    doesn't exist (prod state pre-Phase-2-recovery) — that's the operator's
    signal to run the smoke test which synthesizes its own signing.

    For `intent_source = preferences`: returns an empty list — preferences
    substrate is Phase 5.1.

    For `intent_source = both`: union.
    """
    intents: list[dict[str, Any]] = []
    if relationship.intent_source in ("paid_specs", "both"):
        intents.extend(await _load_paid_spec_intents())
    # preferences are Phase 5.1; empty list for now.
    return intents


async def _load_paid_spec_intents() -> list[dict[str, Any]]:
    """Load active (un-expired) signed specs from Phase 2 substrate.

    Returns an empty list gracefully if Phase 2 schema is not present (the
    `try/except UndefinedTable` path) — the scaffold is fault-tolerant on
    that gap.
    """
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT signing_id, spec_id, count_at_signing,
                           catalog_snapshot_ts, signed_at, expires_at,
                           cohort_manifest_uri
                    FROM business.audience_spec_signings
                    WHERE (expires_at IS NULL OR expires_at > NOW())
                    """
                )
                rows = await cur.fetchall()
    except Exception as exc:
        LOG.warning(
            "load_paid_spec_intents: business.audience_spec_signings unavailable (%s); "
            "returning empty list",
            exc,
        )
        return []
    return [
        {
            "intent_id": r[0],
            "intent_kind": "paid_spec",
            "spec_id": r[1],
            "count_at_signing": r[2],
            "catalog_snapshot_ts": r[3],
            "signed_at": r[4],
            "expires_at": r[5],
            "cohort_manifest_uri": r[6],
        }
        for r in rows
    ]


# ─── Target query compilation ────────────────────────────────────────────


async def _compile_target_query(
    relationship: RelationshipConfig,
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Stub for the target-population resolver.

    For the scaffold, returns a small synthetic candidate set that exercises
    the scoring path. In production tuning, this is replaced with a DuckDB-
    over-R2 read of the spec's cohort manifest parquet, optionally re-filtered
    by `relationship.target_filter`.

    Returns:
        {"candidates": [
            {"entity_ref": str, "scalar_attrs": dict, "embedding": list[float] | None,
             "last_updated_at": datetime | None},
            ...
        ]}
    """
    # Scaffold synthetic candidates. Operator wires the real R2-manifest read
    # in a follow-up. Each candidate has scalar attrs that exercise the
    # scoring path's "scalar hit" loop + a placeholder embedding.
    return {
        "candidates": [
            {
                "entity_ref": f"DOT-{i:06d}",
                "scalar_attrs": {
                    "state": "TX" if i % 3 == 0 else "CA",
                    "safety_rating": "satisfactory",
                    "power_units": 10 * (i + 1),
                },
                "embedding": np.array([0.1 * (i + 1)] * 8, dtype=np.float32),
                "last_updated_at": datetime.now(timezone.utc),
                "source": "fmcsa.carrier_essentials_latest",
            }
            for i in range(5)
        ],
        "scalar_filter_template": {
            "state": "TX",
            "safety_rating": "satisfactory",
        },
        "query_centroid": np.array([0.2] * 8, dtype=np.float32),
    }


# ─── Scoring ─────────────────────────────────────────────────────────────


def _score_candidate(
    candidate: dict[str, Any],
    target_query: dict[str, Any],
    strategy: ScoringStrategy,
) -> tuple[float, MatchReasons]:
    """Apply the placeholder scoring strategy to one candidate.

    The three terms:
      scalar_term  = strategy.scalar_weight × |attributes in candidate that
                     match the scalar_filter_template|
      vector_term  = strategy.vector_weight × cosine(query_centroid, embedding)
      recency_term = strategy.recency_boost_weight × 1/(1 + days_since_update)
    """
    scalar_filter = target_query.get("scalar_filter_template", {})
    scalar_hits: list[dict[str, Any]] = []
    for attr, expected in scalar_filter.items():
        actual = candidate["scalar_attrs"].get(attr)
        matched = actual == expected
        scalar_hits.append({"attribute": attr, "value": actual, "matched": matched})
    scalar_term = strategy.scalar_weight * sum(1 for h in scalar_hits if h["matched"])

    vector_similarity: float | None = None
    vector_term = 0.0
    centroid = target_query.get("query_centroid")
    embedding = candidate.get("embedding")
    if centroid is not None and embedding is not None:
        a = np.asarray(centroid, dtype=np.float32)
        b = np.asarray(embedding, dtype=np.float32)
        denom = (np.linalg.norm(a) * np.linalg.norm(b)) or 1.0
        vector_similarity = float(np.dot(a, b) / denom)
        vector_term = strategy.vector_weight * vector_similarity

    recency_score: float | None = None
    recency_term = 0.0
    last_updated = candidate.get("last_updated_at")
    if last_updated is not None:
        delta_days = max((datetime.now(timezone.utc) - last_updated).days, 0)
        recency_score = 1.0 / (1.0 + delta_days)
        recency_term = strategy.recency_boost_weight * recency_score

    score = float(scalar_term + vector_term + recency_term)
    reasons = MatchReasons(
        scalar_hits=scalar_hits,
        vector_similarity=vector_similarity,
        recency_score=recency_score,
    )
    return score, reasons


# ─── Surfacing application ───────────────────────────────────────────────


async def _apply_surfacing_rule(
    match: Match,
    rule: SurfacingRule,
    intent: dict[str, Any],
) -> list[Surfacing]:
    """For each channel in the rule, fire the corresponding handler."""
    from app.services.matching_engine.surfacing import (
        cold_email_handoff,
        operator_queue,
        portal,
    )

    surfacings: list[Surfacing] = []
    handlers = {
        "portal": portal.surface_match,
        "operator_queue": operator_queue.surface_match,
        "cold_email_handoff": cold_email_handoff.surface_match,
    }
    for channel in rule.channels:
        handler = handlers.get(channel)
        if handler is None:
            LOG.warning("unknown surfacing channel %r — skipping", channel)
            continue
        try:
            s = await handler(match, rule, intent)
            if s is not None:
                surfacings.append(s)
        except Exception as exc:  # noqa: BLE001 — scaffold tolerance
            LOG.error("surfacing handler %s failed: %s", channel, exc, exc_info=True)
    return surfacings


# ─── Public API ──────────────────────────────────────────────────────────


async def evaluate_relationship_for_intent(
    relationship: RelationshipConfig,
    intent: dict[str, Any],
) -> list[Match]:
    """Evaluate one (relationship, intent) pair. Persists matches + surfacings."""
    intent_id = intent["intent_id"]
    intent_kind = intent["intent_kind"]

    target_query = await _compile_target_query(relationship, intent)
    persisted: list[Match] = []
    for candidate in target_query["candidates"]:
        score, reasons = _score_candidate(candidate, target_query, relationship.scoring_strategy)
        if score <= 0:
            continue
        match = Match(
            source_intent_id=intent_id,
            intent_kind=intent_kind,
            relationship_id=relationship.relationship_id,
            target_entity_ref=candidate["entity_ref"],
            score=score,
            match_reasons=reasons,
            status="identified",
            source_freshness={
                candidate.get("source", "unknown"): (
                    candidate["last_updated_at"].isoformat()
                    if candidate.get("last_updated_at")
                    else None
                ),
            },
        )
        match_id = await persist_match(match)
        match.match_id = match_id
        # Mark surfacing channels.
        surfacings = await _apply_surfacing_rule(match, relationship.surfacing_rule, intent)
        if surfacings:
            # Bump status to 'surfaced' on successful surfacing dispatch.
            from app.services.matching_engine.persistence import transition_match

            await transition_match(match_id, "surfaced")
            match.status = "surfaced"
        persisted.append(match)
    return persisted


async def evaluate_relationship(
    relationship_id: UUID,
    *,
    synthetic_intent: dict[str, Any] | None = None,
) -> list[Match]:
    """Evaluate one relationship across all eligible intents.

    Pass `synthetic_intent` to bypass the DB intent loader (used by the smoke
    test before the Phase 2 substrate is recovered in prod).
    """
    relationship = await load_relationship(relationship_id)
    if not relationship.enabled:
        LOG.info("relationship %s is disabled — skipping", relationship_id)
        return []
    intents = (
        [synthetic_intent]
        if synthetic_intent is not None
        else await load_eligible_intents(relationship)
    )
    matches: list[Match] = []
    for intent in intents:
        matches.extend(await evaluate_relationship_for_intent(relationship, intent))
    return matches


async def evaluate_all_active_relationships() -> dict[UUID, list[Match]]:
    """Daily orchestrator entry point. Returns {relationship_id: [matches...]}."""
    relationships = await list_active_relationships()
    out: dict[UUID, list[Match]] = {}
    for rel in relationships:
        try:
            out[rel.relationship_id] = await evaluate_relationship(rel.relationship_id)
        except Exception as exc:  # noqa: BLE001 — daily cron tolerance
            LOG.error(
                "evaluate_relationship failed for %s (%s): %s",
                rel.relationship_id, rel.name, exc, exc_info=True,
            )
            out[rel.relationship_id] = []
    return out


# ─── Exceptions ──────────────────────────────────────────────────────────


class UnknownRelationship(Exception):
    """Raised when a relationship_id or name does not exist."""
