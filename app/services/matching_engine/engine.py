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
import os
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

# Per-source primary-key columns used to build a stable `entity_ref` string.
# Add a row here when registering a new Lance dataset that the matching
# engine should resolve. Unknown sources return zero candidates (gracefully).
ENTITY_REF_COLUMNS: dict[tuple[str, str], list[str]] = {
    ("sba", "borrowers_lance"): ["legal_name_normalized", "borrstate", "borrzip"],
    ("sba", "loans_lance"): ["loan_id"],
    ("sba", "lenders_lance"): ["bankname_normalized", "lender_type"],
    ("fmcsa", "carrier_essentials_lance"): ["dot_number"],
    ("sam_gov", "entities_lance"): ["unique_entity_id"],
    ("pdl", "free_companies_lance"): ["pdl_id"],
    ("bridges", "pdl_sba_borrower_lance"): ["sba_name_normalized", "sba_state", "pdl_id"],
    ("bridges", "sam_sba_borrower_lance"): ["sba_name_normalized", "sba_state", "unique_entity_id"],
    ("bridges", "usaspending_sba_borrower_lance"): ["sba_name_normalized", "sba_state", "recipient_uei"],
    # ─── ucc-gleif-identity-spine cycle additions ────────────────────────
    # GLEIF derives (2)
    ("gleif", "relationship_records_lance"): ["relationship_id"],
    ("gleif", "lei_with_parent_lance"): ["lei"],
    # New bridges (6)
    ("bridges", "ucc_gleif_lance"): ["secured_party_name_normalized", "secured_party_state", "lei"],
    ("bridges", "ucc_pdl_lance"): ["secured_party_name_normalized", "secured_party_state", "match_path", "pdl_id"],
    ("bridges", "ucc_sba_lender_lance"): ["lender_name_normalized", "bankname_normalized"],
    ("bridges", "ucc_sba_borrower_lance"): ["debtor_name_normalized", "state", "legal_name_normalized"],
    ("bridges", "sba_lender_gleif_lance"): ["bankname_normalized", "bankstate", "lei"],
    ("bridges", "sba_borrower_gleif_lance"): ["legal_name_normalized", "state", "lei"],
    # UCC base tables (3 — for s15 smoke and operator-direct UCC specs)
    ("ucc_ca", "lenders_lance"): ["lender_name_normalized"],
    ("ucc_ca", "debtors_lance"): ["UCC1_NUM", "ORG_NAME", "STATE"],
    ("ucc_ca", "secured_parties_lance"): ["UCC1_NUM", "ORG_NAME", "STATE"],
}

# Per-source columns surfaced into `candidate.scalar_attrs` for the scorer
# to evaluate the spec's filter template against. Columns absent here are
# still applied as SQL filters but won't show up as `scalar_hits` in the
# match's reasons. Append-only.
SCALAR_ATTR_COLUMNS: dict[tuple[str, str], list[str]] = {
    ("sba", "borrowers_lance"): [
        "borrstate", "latest_loanstatus", "has_pending_commit",
        "total_loans", "total_gross_approval",
    ],
    ("sba", "loans_lance"): ["borrstate", "loanstatus", "franchisename"],
    ("fmcsa", "carrier_essentials_lance"): ["phy_state", "safety_rating"],
}

# Filter ops → DuckDB SQL templates. The `{col}` and `{val}` placeholders
# get the already-quoted identifier and literal respectively (see _quote_*).
_FILTER_OPS: dict[str, str] = {
    "eq":  "{col} = {val}",
    "ne":  "{col} <> {val}",
    "gt":  "{col} > {val}",
    "lt":  "{col} < {val}",
    "gte": "{col} >= {val}",
    "lte": "{col} <= {val}",
    "cardinality_gt": "len({col}) > {val}",
}

# Cap candidate volume per (relationship, intent) evaluation. Match-queue is
# operator-paced; spamming 100K matches per relationship dilutes the queue.
_CANDIDATE_LIMIT = 500


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
    """Load the intent's spec content, resolve its first source to a Lance
    dataset under `polaris-warehouse/`, apply the spec's scalar filters via
    DuckDB-over-Arrow, and return the candidate set.

    Source resolution: `s3://dex-raw-landing-zone/polaris-warehouse/{namespace}/{table}/`.
    Lance read via `pylance` + Arrow bridge to DuckDB (matches the canonical
    pattern in apps/data-engine-x/scripts/build_bridge_*.py — lance-duckdb
    extension is unstable on osx_arm64 per the Lance canary cycle report).

    Returns an empty candidate set (gracefully) when:
      - the spec_id can't be loaded
      - the spec has no sources declared
      - the (namespace, table) isn't registered in `ENTITY_REF_COLUMNS`
      - the Lance read raises any exception

    Returned shape (consumed by `_score_candidate`):
        {"candidates": [
            {"entity_ref": str, "scalar_attrs": dict,
             "embedding": list[float] | None,
             "last_updated_at": datetime | None,
             "source": str},
            ...
        ],
         "scalar_filter_template": dict,
         "query_centroid": np.ndarray | None}
    """
    spec_id = intent.get("spec_id")
    if spec_id is None:
        return _empty_target_query()

    spec_content = await _load_spec_content(spec_id)
    if spec_content is None:
        return _empty_target_query()

    sources = spec_content.get("sources") or []
    if not sources:
        LOG.warning("spec %s has no sources declared — no candidates", spec_id)
        return _empty_target_query()

    src = sources[0]
    namespace = src.get("namespace")
    table = src.get("table")
    if not namespace or not table:
        LOG.warning("spec %s source is malformed (%r) — no candidates", spec_id, src)
        return _empty_target_query()

    key = (namespace, table)
    if key not in ENTITY_REF_COLUMNS:
        LOG.warning(
            "spec %s targets %s.%s but it has no entity-ref mapping registered; "
            "add an entry to ENTITY_REF_COLUMNS in engine.py — returning empty",
            spec_id, namespace, table,
        )
        return _empty_target_query()

    filters = spec_content.get("filters") or []
    try:
        candidates = _read_lance_with_filters(namespace, table, filters)
    except Exception as exc:
        LOG.error(
            "Lance read failed for %s.%s — returning empty (spec=%s): %s",
            namespace, table, spec_id, exc, exc_info=True,
        )
        return _empty_target_query()

    # Build scalar_filter_template from the spec's eq-filters only. This is
    # what the scorer's scalar_term loop checks against each candidate.
    scalar_filter_template = {
        f["column"]: f["value"]
        for f in filters
        if f.get("op") == "eq" and f.get("column") is not None
    }

    return {
        "candidates": candidates,
        "scalar_filter_template": scalar_filter_template,
        # Embedding centroid: not yet wired (operator decision on embedding
        # model pending; see project memory). Scorer's vector_term stays 0.
        "query_centroid": None,
    }


async def _load_spec_content(spec_id: UUID | str) -> dict[str, Any] | None:
    """Load the JSONB content blob from business.audience_specs."""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT content FROM business.audience_specs WHERE spec_id = %s",
                    (str(spec_id),),
                )
                row = await cur.fetchone()
        if row is None:
            return None
        # psycopg returns JSONB as a dict already.
        return row[0]
    except Exception as exc:
        LOG.warning("load_spec_content(%s) failed: %s", spec_id, exc)
        return None


def _empty_target_query() -> dict[str, Any]:
    return {
        "candidates": [],
        "scalar_filter_template": {},
        "query_centroid": None,
    }


def _read_lance_with_filters(
    namespace: str,
    table: str,
    filters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Read a Lance dataset, apply scalar filters, return candidate dicts.

    Uses the Arrow-bridge pattern (pylance → pyarrow.Table → DuckDB.register)
    rather than the lance-duckdb extension (osx_arm64 instability — see Lance
    canary cycle report). Suitable for batch reads up to ~500 candidates.

    Synchronous on purpose: pylance is sync-only, and the daily-cron caller
    can tolerate a sub-second block per intent.
    """
    import duckdb
    import lance

    key = (namespace, table)
    key_cols = ENTITY_REF_COLUMNS[key]
    scalar_cols = SCALAR_ATTR_COLUMNS.get(key, [])
    filter_cols = [f["column"] for f in filters if f.get("column")]
    columns_needed = list({*key_cols, *scalar_cols, *filter_cols})

    lance_uri = f"s3://dex-raw-landing-zone/polaris-warehouse/{namespace}/{table}/"
    storage_options = {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }
    ds = lance.dataset(lance_uri, storage_options=storage_options)
    arrow_table = ds.scanner(columns=columns_needed).to_table()

    con = duckdb.connect()
    try:
        con.register("ds", arrow_table)
        where_sql = _filters_to_where_sql(filters)
        select_sql = ", ".join(_quote_ident(c) for c in columns_needed)
        rows = con.execute(
            f"SELECT {select_sql} FROM ds WHERE {where_sql} LIMIT {_CANDIDATE_LIMIT}"
        ).fetchall()
    finally:
        con.close()

    now = datetime.now(timezone.utc)
    source_ref = f"{namespace}.{table}"
    candidates: list[dict[str, Any]] = []
    for row in rows:
        rowd = dict(zip(columns_needed, row))
        entity_ref = "|".join(
            [source_ref] + [str(rowd.get(c) or "") for c in key_cols]
        )
        scalar_attrs = {c: rowd.get(c) for c in scalar_cols}
        candidates.append({
            "entity_ref": entity_ref,
            "scalar_attrs": scalar_attrs,
            "embedding": None,
            "last_updated_at": now,
            "source": source_ref,
        })
    LOG.info(
        "Lance read %s.%s — filters=%d rows=%d",
        namespace, table, len(filters), len(candidates),
    )
    return candidates


def _filters_to_where_sql(filters: list[dict[str, Any]]) -> str:
    """Compile spec filters into a DuckDB WHERE clause. Unsupported ops are
    silently dropped with a log line — never inject untrusted spec content
    into SQL beyond the whitelisted op templates + quoted ident/value."""
    clauses: list[str] = []
    for f in filters:
        op = f.get("op")
        col = f.get("column")
        val = f.get("value")
        if op not in _FILTER_OPS:
            LOG.info("filter dropped (unsupported op %r on %r)", op, col)
            continue
        if not col:
            continue
        try:
            quoted_col = _quote_ident(col)
            quoted_val = _quote_value(val)
        except ValueError as exc:
            LOG.warning("filter dropped (%s)", exc)
            continue
        clauses.append(_FILTER_OPS[op].format(col=quoted_col, val=quoted_val))
    return " AND ".join(clauses) or "1=1"


def _quote_ident(col: str) -> str:
    """Quote a DuckDB identifier. Rejects anything outside [A-Za-z0-9_] to
    block injection via spec column names."""
    if not col or not col.replace("_", "").isalnum():
        raise ValueError(f"invalid column identifier: {col!r}")
    return f'"{col}"'


def _quote_value(val: Any) -> str:
    """Quote a literal for DuckDB. Strings are single-quoted with embedded
    quotes rejected (rather than escaped) — spec values come from operator-
    authored JSON, not user input, but defense-in-depth applies."""
    if isinstance(val, bool):
        return "TRUE" if val else "FALSE"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        if "'" in val or ";" in val or "--" in val or "\x00" in val:
            raise ValueError(f"invalid string literal: {val!r}")
        return f"'{val}'"
    raise ValueError(f"unsupported literal type: {type(val).__name__}")


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
