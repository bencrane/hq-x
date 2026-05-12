"""Pydantic spec language for partner-authored audience specs.

The composed ``AudienceSpec`` is the partner's minimal declaration of intent
(per partner_intent_lives_in_the_spec.md) — the platform's intermediation
job is to fill in the rest from the data lake. The spec evaluator compiles
this AST to SQL via DuckDB-over-Iceberg.

Forward-compat: Phase 4 vector primitives (``similar_to``, ``semantic_match``)
get TYPED PLACEHOLDERS in this scaffold — the evaluator raises
``NotImplementedError`` if used until Phase 4 ships. Spec authors can
declare them today; queries that use them will fail loudly.
"""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ─── identifier safety ───────────────────────────────────────────────

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NAMESPACE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


def _is_safe_ident(value: str) -> bool:
    return bool(_IDENT_RE.match(value))


def _is_safe_namespace(value: str) -> bool:
    """Allow dotted namespaces (e.g. ``partners.<pid>``) in addition to
    bare identifiers. Each dot-separated segment must be a SQL identifier.
    """
    return all(_is_safe_ident(s) for s in value.split("."))


# ─── source references ───────────────────────────────────────────────


class CatalogRef(BaseModel):
    """A source the spec reads from. Names a catalog table.

    Examples:
        - ``CatalogRef(namespace='fmcsa', table='carrier_latest')``
        - ``CatalogRef(namespace='partners.acme', table='past_wins')``

    Per vertical_network_platform_frame.md, namespaces are
    ``sources.{source}`` (universal) or ``partners.{partner_id}.{spec_id}``
    (per-partner private). NO ``verticals.*`` tier.
    """

    namespace: str = Field(..., min_length=1)
    table: str = Field(..., min_length=1)

    model_config = ConfigDict(extra="forbid")

    @field_validator("namespace")
    @classmethod
    def _validate_namespace(cls, v: str) -> str:
        if not _is_safe_namespace(v):
            raise ValueError(f"namespace must be a SQL identifier (or dotted); got {v!r}")
        return v

    @field_validator("table")
    @classmethod
    def _validate_table(cls, v: str) -> str:
        if not _is_safe_ident(v):
            raise ValueError(f"table must be a SQL identifier; got {v!r}")
        return v

    def qualified(self) -> str:
        """Return the dotted ``namespace.table`` string used by the
        Iceberg catalog and the DuckDB view name."""
        return f"{self.namespace}.{self.table}"


# ─── scalar predicates ───────────────────────────────────────────────


_OP_TYPES = Literal[
    "eq", "ne", "in", "nin", "gt", "gte", "lt", "lte",
    "like", "ilike", "between", "is_null", "is_not_null",
]
_OPS_NULL_ONLY: set[str] = {"is_null", "is_not_null"}
_OPS_LIST_VALUE: set[str] = {"in", "nin"}
_OPS_RANGE_VALUE: set[str] = {"between"}


class ScalarPredicate(BaseModel):
    """One filter on a scalar attribute. AND-of-OR composable at the
    spec level (top-level filters are AND-conjoined; future ``or_``
    grouping is a forward-compat extension).
    """

    column: str = Field(..., min_length=1)
    op: _OP_TYPES
    value: Any = Field(default=None)

    model_config = ConfigDict(extra="forbid")

    @field_validator("column")
    @classmethod
    def _validate_column(cls, v: str) -> str:
        if not _is_safe_ident(v):
            raise ValueError(f"column must be a SQL identifier; got {v!r}")
        return v

    def model_post_init(self, _: Any) -> None:
        if self.op in _OPS_NULL_ONLY and self.value is not None:
            raise ValueError(f"op {self.op!r} must not carry a value")
        if self.op in _OPS_LIST_VALUE:
            if not isinstance(self.value, list) or not self.value:
                raise ValueError(f"op {self.op!r} requires a non-empty list value")
        if self.op in _OPS_RANGE_VALUE:
            if not isinstance(self.value, list) or len(self.value) != 2:
                raise ValueError(f"op {self.op!r} requires [low, high] list value")
        if self.op not in _OPS_NULL_ONLY \
                and self.op not in _OPS_LIST_VALUE \
                and self.op not in _OPS_RANGE_VALUE \
                and self.value is None:
            raise ValueError(f"op {self.op!r} requires a non-null value")


# ─── freshness SLAs (the partner-declared contract on data freshness) ───


class FreshnessRequirement(BaseModel):
    """Spec-declared freshness SLA per source.

    Refused at sign-time if the source isn't fresh enough. Per
    operator_data_anxieties_phase_0.md (concern 1: staleness gap),
    freshness is a load-bearing contract surface — not a soft signal.

    Examples:
        - ``FreshnessRequirement(source='fmcsa.carrier_latest', max_age_seconds=86400)``
          (FMCSA daily snapshots; refuse if last snapshot >24h old)
    """

    source: str = Field(..., min_length=1)
    max_age_seconds: int = Field(..., gt=0)

    model_config = ConfigDict(extra="forbid")


# ─── exclusion rules ────────────────────────────────────────────────


_EXCLUSION_KINDS = Literal[
    "entity_blocklist",   # block specific entity_refs
    "contact_recency",    # exclude entities contacted in last N days (cross-spec)
    "source_age",         # exclude entries whose source row exceeds an age
    "custom",             # operator-defined; evaluator routes by parameters['kind']
]


class ExclusionRule(BaseModel):
    """Opt-outs, prior contacts, partner-blacklist, etc.

    Evaluator dispatches by ``rule_kind``; ``parameters`` is a free-shape
    dict whose schema is per-kind (validated by the evaluator, not here).
    """

    rule_kind: _EXCLUSION_KINDS
    parameters: dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="forbid")


# ─── PHASE 4 VECTOR PRIMITIVES ───────────────────────────────────────
#
# Activated in Phase 4. Partners declare "find more like these / find
# things that semantically match this description" — the platform's
# Lance vector layer (per ``lance_is_the_universal_substrate.md``) does
# the work. Both clauses name an ``embedding_source`` which must point at
# a registered embeddings Lance dataset (e.g. ``fmcsa.carrier_essentials_embeddings_lance``).


class SimilarityClause(BaseModel):
    """k-NN against partner-supplied seed entities.

    A partner says "find more like my top 50 wins" by listing seed entity
    refs (canonical IDs, e.g. DOT numbers) and the embeddings dataset to
    compare against. The evaluator looks up the seeds' embeddings, takes
    their centroid (mean vector), and ANN-searches the dataset for the
    top-K nearest neighbors above ``similarity_threshold``.
    """

    seed_entity_refs: list[str] = Field(..., min_length=1)
    """Canonical IDs of seed entities. For FMCSA carriers, dot_number."""

    embedding_source: str = Field(..., min_length=1)
    """The embeddings dataset to query, e.g. 'fmcsa.carrier_essentials_embeddings_lance'."""

    similarity_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    """Cosine similarity floor (0..1). Candidates below this are excluded."""

    limit: int = Field(default=1000, ge=1, le=100_000)
    """Max candidates returned (also caps the ANN top-K)."""

    model_config = ConfigDict(extra="forbid")


class SemanticPredicate(BaseModel):
    """Match entities whose attributes semantically resemble a free-text description.

    A partner says "trucking carriers that haul hazardous materials in the
    Pacific Northwest" — the evaluator embeds the query text via the same
    model used for the dataset, then ANN-searches the dataset for top-K
    matches above ``similarity_threshold``.
    """

    query_text: str = Field(..., min_length=1)
    """Free-text query, e.g. 'refrigerated produce hauling in California'."""

    embedding_source: str = Field(..., min_length=1)
    """The embeddings dataset to query."""

    similarity_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    """Cosine similarity floor (0..1). Semantic matches are typically
    looser than seed-similarity matches; the default 0.5 reflects that."""

    limit: int = Field(default=1000, ge=1, le=100_000)
    """Max candidates returned (also caps the ANN top-K)."""

    model_config = ConfigDict(extra="forbid")


# ─── the spec ──────────────────────────────────────────────────────


class AudienceSpec(BaseModel):
    """Partner-authored intent declaration. Compiles to SQL via the
    evaluator. Lean by design — the data lake fills in everything else.

    See partner_intent_lives_in_the_spec.md: the spec is the partner's
    minimal declaration of intent, not an exhaustive description.
    """

    sources: list[CatalogRef] = Field(..., min_length=1)
    filters: list[ScalarPredicate] = Field(default_factory=list)

    # PHASE 4 placeholders — typed but not yet evaluable.
    similar_to: SimilarityClause | None = None
    semantic_match: SemanticPredicate | None = None

    exclude: list[ExclusionRule] = Field(default_factory=list)
    enrich_with: list[CatalogRef] = Field(default_factory=list)
    required_freshness: list[FreshnessRequirement] = Field(default_factory=list)

    model_config = ConfigDict(extra="forbid")

    @property
    def primary_source(self) -> CatalogRef:
        """The first source — the spec's base table for FROM."""
        return self.sources[0]
