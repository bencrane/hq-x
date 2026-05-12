#!/usr/bin/env python3
"""End-to-end smoke test for the Phase 5 matching engine scaffold.

Verifies:

  1. The 3 `business.matching_*` tables exist with the expected schema.
  2. The seeded relationship row `demand_side_fulfillment_paid_spec_v1` is present.
  3. `evaluate_relationship(seed_relationship_id, synthetic_intent)` runs end-to-end:
     a. Reads the relationship config.
     b. Compiles the (stub) target query.
     c. Scores 5 synthetic candidates.
     d. Persists ≥1 match into `business.matches`.
     e. Persists ≥1 `business.match_surfacings` row with channel='portal'.
  4. The `transition_match` status-graph guard rejects an invalid transition
     and accepts a valid one.
  5. The 5 persisted matches are plausibly scored (score > 0 for each).

Idempotent: uses a synthetic source_intent_id per run, then deletes the rows
it created. Does NOT depend on Phase 2 `business.audience_spec_signings`
existing in prod — the synthetic intent bypasses the DB intent loader.

Run:
    doppler --project hq-all --config prd run -- python scripts/smoke_matching_engine.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.db import close_pool, get_db_connection, init_pool
from app.services.matching_engine import engine as engine_mod
from app.services.matching_engine import persistence as persist_mod


SEED_NAME = "demand_side_fulfillment_paid_spec_v1"


def _print_pass(msg: str) -> None:
    print(f"[PASS] {msg}")


def _print_fail(msg: str) -> None:
    print(f"[FAIL] {msg}", file=sys.stderr)


async def _verify_schema() -> None:
    """Gate 1: 3 business.matching_* tables exist."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'business'
                  AND table_name IN
                      ('matching_relationships','matches','match_surfacings')
                ORDER BY table_name
                """
            )
            rows = await cur.fetchall()
    found = {r[0] for r in rows}
    expected = {"matching_relationships", "matches", "match_surfacings"}
    missing = expected - found
    if missing:
        raise SystemExit(f"[FAIL] missing tables: {sorted(missing)}")
    _print_pass(f"3 business.matching_* tables present ({sorted(found)})")


async def _verify_seed() -> UUID:
    """Gate 2: seed row present, returns its id."""
    relationship = await engine_mod.load_relationship_by_name(SEED_NAME)
    _print_pass(f"seed relationship present: name={SEED_NAME} id={relationship.relationship_id}")
    return relationship.relationship_id


async def _evaluate_and_persist(seed_id: UUID) -> tuple[UUID, list]:
    """Gate 3: evaluate_relationship persists ≥1 match + surfacing."""
    synthetic_intent_id = uuid4()
    synthetic_intent = {
        "intent_id": synthetic_intent_id,
        "intent_kind": "paid_spec",
        "spec_id": uuid4(),
        "count_at_signing": 5,
        "catalog_snapshot_ts": datetime.now(timezone.utc),
        "signed_at": datetime.now(timezone.utc),
        "expires_at": None,
        "cohort_manifest_uri": "s3://smoke-test/synthetic-manifest.parquet",
    }
    matches = await engine_mod.evaluate_relationship(
        seed_id, synthetic_intent=synthetic_intent,
    )
    if not matches:
        raise SystemExit("[FAIL] evaluate_relationship returned no matches")
    _print_pass(f"evaluate_relationship persisted {len(matches)} matches")
    # Inspect each for plausibility.
    for i, m in enumerate(matches):
        if m.score <= 0:
            raise SystemExit(f"[FAIL] match[{i}] has score {m.score} ≤ 0")
        if not m.target_entity_ref.startswith("DOT-"):
            raise SystemExit(f"[FAIL] match[{i}] target_entity_ref looks wrong: {m.target_entity_ref}")
    _print_pass(f"all {len(matches)} matches have score > 0 and a DOT-style entity ref")
    return synthetic_intent_id, matches


async def _verify_portal_surfacings(matches: list) -> None:
    """Gate 4: ≥1 portal surfacing was persisted per match."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT match_id, channel, COUNT(*)
                FROM business.match_surfacings
                WHERE match_id = ANY(%s)
                GROUP BY match_id, channel
                """,
                ([str(m.match_id) for m in matches],),
            )
            rows = await cur.fetchall()
    by_match_channel = {(r[0], r[1]): r[2] for r in rows}
    portal_count = sum(c for (_, ch), c in by_match_channel.items() if ch == "portal")
    if portal_count < 1:
        raise SystemExit(f"[FAIL] expected ≥1 portal surfacing; got {portal_count}")
    _print_pass(f"{portal_count} portal surfacings persisted across {len(matches)} matches")


async def _verify_transition_guard(matches: list) -> None:
    """Gate 5: status-graph guard rejects invalid, accepts valid."""
    m = matches[0]
    # m.status is already 'surfaced' (engine flipped after surfacing dispatched).
    # 'surfaced' → 'identified' is INVALID per the graph.
    try:
        await persist_mod.transition_match(m.match_id, "identified")
    except persist_mod.InvalidTransition:
        _print_pass("transition_match correctly rejected invalid surfaced→identified")
    else:
        raise SystemExit("[FAIL] transition_match accepted invalid surfaced→identified")
    # 'surfaced' → 'viewed' IS valid.
    await persist_mod.transition_match(m.match_id, "viewed")
    _print_pass("transition_match accepted valid surfaced→viewed")


async def _print_sample(matches: list) -> None:
    """Print 5 sample matches for operator review."""
    print("\n[SAMPLE] First 5 persisted matches:")
    for i, m in enumerate(matches[:5]):
        print(
            f"  {i+1}. match_id={m.match_id} target={m.target_entity_ref} "
            f"score={m.score:.4f} reasons={m.match_reasons.model_dump_json()}"
        )


async def _cleanup(synthetic_intent_id: UUID) -> None:
    """Delete the rows the smoke created. Surfacings cascade via FK."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM business.matches WHERE source_intent_id = %s",
                (str(synthetic_intent_id),),
            )
    _print_pass("cleanup: deleted smoke-test matches + cascaded surfacings")


async def main() -> int:
    print("==> Phase 5 matching-engine smoke test starting")
    await init_pool()
    try:
        await _verify_schema()
        seed_id = await _verify_seed()
        intent_id, matches = await _evaluate_and_persist(seed_id)
        await _verify_portal_surfacings(matches)
        await _verify_transition_guard(matches)
        await _print_sample(matches)
        await _cleanup(intent_id)
        print("\n==> SUMMARY: 5/5 PASS")
        return 0
    except SystemExit as exc:
        print(str(exc))
        return 1
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
