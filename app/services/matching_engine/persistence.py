"""Match + surfacing persistence with idempotency + status guard.

`persist_match` upserts: same (source_intent_id, intent_kind, relationship_id,
target_entity_ref) hit twice within 24h updates the existing row's score +
reasons instead of inserting a duplicate. This keeps re-runs of the daily
cron from inflating the table.

`transition_match` guards the status transition graph:
  identified → surfaced | dismissed | expired
  surfaced   → viewed | dismissed | expired
  viewed     → reserved | dismissed | expired
  reserved   → claimed | dismissed | expired
  claimed, dismissed, expired = terminal (no further transitions)
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from app.db import get_db_connection
from app.services.matching_engine.models import Match, MatchStatus, Surfacing

LOG = logging.getLogger(__name__)


# The status graph. Keys = source state; values = set of permitted destinations.
_ALLOWED_TRANSITIONS: dict[MatchStatus, set[MatchStatus]] = {
    "identified": {"surfaced", "dismissed", "expired"},
    "surfaced":   {"viewed", "dismissed", "expired"},
    "viewed":     {"reserved", "dismissed", "expired"},
    "reserved":   {"claimed", "dismissed", "expired"},
    "claimed":    set(),  # terminal
    "dismissed":  set(),  # terminal
    "expired":    set(),  # terminal
}


class InvalidTransition(Exception):
    """Raised when a status transition violates the lifecycle graph."""


async def persist_match(match: Match) -> UUID:
    """Insert or update the match row. Returns its match_id.

    Idempotency: dedup on (source_intent_id, intent_kind, relationship_id,
    target_entity_ref) within a 24h window. If a row exists in that window,
    its score + match_reasons + source_freshness are updated and the SAME
    match_id is returned (no new row).
    """
    reasons_json = match.match_reasons.model_dump(mode="json")
    freshness_json = match.source_freshness or {}

    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            # Look for an existing row within the dedup window.
            await cur.execute(
                """
                SELECT match_id, status
                FROM business.matches
                WHERE source_intent_id = %s
                  AND intent_kind = %s
                  AND relationship_id = %s
                  AND target_entity_ref = %s
                  AND identified_at > NOW() - INTERVAL '24 hours'
                ORDER BY identified_at DESC
                LIMIT 1
                """,
                (
                    str(match.source_intent_id),
                    match.intent_kind,
                    str(match.relationship_id),
                    match.target_entity_ref,
                ),
            )
            existing = await cur.fetchone()
            if existing is not None:
                match_id, _existing_status = existing
                await cur.execute(
                    """
                    UPDATE business.matches
                    SET score = %s,
                        match_reasons = %s::jsonb,
                        source_freshness = %s::jsonb
                    WHERE match_id = %s
                    """,
                    (
                        match.score,
                        json.dumps(reasons_json),
                        json.dumps(freshness_json),
                        match_id,
                    ),
                )
                return match_id

            await cur.execute(
                """
                INSERT INTO business.matches (
                    source_intent_id, intent_kind, relationship_id,
                    target_entity_ref, target_source_id, score,
                    match_reasons, status, expires_at, source_freshness
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                RETURNING match_id
                """,
                (
                    str(match.source_intent_id),
                    match.intent_kind,
                    str(match.relationship_id),
                    match.target_entity_ref,
                    str(match.target_source_id) if match.target_source_id else None,
                    match.score,
                    json.dumps(reasons_json),
                    match.status,
                    match.expires_at,
                    json.dumps(freshness_json),
                ),
            )
            row = await cur.fetchone()
            return row[0]


async def persist_surfacing(surfacing: Surfacing) -> UUID:
    """Insert a surfacing row. Returns surfacing_id."""
    metadata_json = surfacing.surface_metadata or {}
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO business.match_surfacings (
                    match_id, channel, surface_metadata, outcome
                )
                VALUES (%s, %s, %s::jsonb, %s)
                RETURNING surfacing_id
                """,
                (
                    str(surfacing.match_id),
                    surfacing.channel,
                    json.dumps(metadata_json),
                    surfacing.outcome,
                ),
            )
            row = await cur.fetchone()
            return row[0]


async def transition_match(match_id: UUID, new_status: MatchStatus) -> None:
    """Move match_id to new_status if the transition is allowed.

    Idempotent on same-status: if current == new_status, this is a no-op
    (logged at debug level). Without this, daily-cron re-evaluations raised
    `InvalidTransition 'surfaced' → 'surfaced'` and aborted the relationship's
    iteration before reaching new intents — observed 2026-05-13 cycle 2 smoke.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status FROM business.matches WHERE match_id = %s",
                (str(match_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise InvalidTransition(f"match {match_id} not found")
            current = row[0]
            # Idempotency guard: same-status transition is a no-op. The daily
            # matching-engine cron re-evaluates active intents and reissues
            # surfacings against already-persisted matches; if those matches
            # are already 'surfaced', the call should succeed silently rather
            # than abort the cron with InvalidTransition.
            if current == new_status:
                LOG.debug(
                    "transition_match: match %s already at %r (no-op)",
                    match_id, new_status,
                )
                return
            allowed = _ALLOWED_TRANSITIONS.get(current, set())
            if new_status not in allowed:
                raise InvalidTransition(
                    f"cannot transition match {match_id} from {current!r} to {new_status!r}"
                )
            await cur.execute(
                "UPDATE business.matches SET status = %s WHERE match_id = %s",
                (new_status, str(match_id)),
            )


async def transition_surfacing_outcome(surfacing_id: UUID, new_outcome: str) -> None:
    """Update a surfacing's outcome (operator-approval / partner-action UI uses this).

    No transition graph guard on outcome — operator may toggle pending →
    sent → responded → ... freely. The CHECK constraint at the DB enforces
    the enum.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE business.match_surfacings SET outcome = %s WHERE surfacing_id = %s",
                (new_outcome, str(surfacing_id)),
            )
