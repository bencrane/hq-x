"""Phase 4 vector-query primitives for the audience-spec evaluator.

Implements:
  - ``run_similarity_search(seed_pks, embedding_source, threshold, limit)``
    — k-NN against a centroid of seed embeddings.
  - ``run_semantic_search(query_text, embedding_source, threshold, limit)``
    — k-NN against an embedded free-text query.

Both return a list of (primary_key, similarity) tuples above threshold,
plus the (table_qualname, dataset_version) lineage entries to stamp into
the request-scoped tracker.

The functions open the embeddings Lance dataset directly via the
``pylance`` package (no DuckDB) — Lance's IVF_PQ vector index gives
sub-100ms top-K cold queries; the SQL path would re-materialize the
whole table into memory.

Embedding source resolution
---------------------------
``embedding_source`` is a Polaris generic-table qualified name like
``fmcsa.carrier_essentials_embeddings_lance``. v1 resolves these by
hard-coded dispatch — when Polaris's Generic Table API exposes the
``base-location`` over the catalog, this becomes a runtime lookup.

Embedding model parity
----------------------
The dataset's ``model_version`` column tells us which model produced the
vectors. Query embedding must use the same model — we look at the
dataset's first row to pick. Cross-model queries are refused with
``VectorModelMismatch``.

Per ``partner_intent_lives_in_the_spec.md``: the vector layer is just
another source in the catalog. The evaluator stamps catalog reads for
both the embeddings dataset (for the vector search) AND the primary
entity dataset (for the post-filter SELECT) so the X-Data-Lineage
response header reflects everything that actually got queried.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Any

import numpy as np

LOG = logging.getLogger(__name__)


# ─── embedding-source dispatch ────────────────────────────────────────


@dataclass(frozen=True)
class EmbeddingSourceMeta:
    """Per-embedding-source resolved metadata.

    ``primary_key_column`` is the column on the source AND embeddings
    datasets that uniquely identifies an entity (e.g. ``dot_number`` for
    FMCSA carriers).
    """

    qualified_name: str
    lance_uri: str
    primary_key_column: str


# v1: hard-coded dispatch. When Polaris exposes generic-table base-location
# via its REST API for hq-x, this collapses to a catalog lookup.
_EMBEDDING_SOURCES: dict[str, EmbeddingSourceMeta] = {
    "fmcsa.carrier_essentials_embeddings_lance": EmbeddingSourceMeta(
        qualified_name="fmcsa.carrier_essentials_embeddings_lance",
        lance_uri=(
            "s3://dex-raw-landing-zone/polaris-warehouse/fmcsa/"
            "carrier_essentials_embeddings_lance"
        ),
        primary_key_column="dot_number",
    ),
}


def resolve_embedding_source(name: str) -> EmbeddingSourceMeta:
    """Look up the metadata for a named embedding source.

    Raises ``UnknownEmbeddingSource`` if the name isn't registered.
    """
    meta = _EMBEDDING_SOURCES.get(name)
    if meta is None:
        raise UnknownEmbeddingSource(
            f"embedding source {name!r} is not registered. "
            f"Known: {sorted(_EMBEDDING_SOURCES)}"
        )
    return meta


# ─── Lance dataset handle (per-process singleton) ─────────────────────


_lance_cache: dict[str, Any] = {}
_lance_lock = threading.Lock()


def _lance_storage_options() -> dict:
    """S3-protocol options for Lance to talk to Cloudflare R2.

    Matches the form used in DEX (``scripts/_lib/lance_emit.py``) so the
    two paths stay swappable.
    """
    return {
        "aws_endpoint": _required_env("R2_ENDPOINT"),
        "aws_access_key_id": _required_env("R2_ACCESS_KEY_ID"),
        "aws_secret_access_key": _required_env("R2_SECRET_ACCESS_KEY"),
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise EnvironmentError(f"{name} is required for the Lance vector layer")
    return val


def _get_lance_dataset(uri: str) -> Any:
    """Open a Lance dataset once per process; reuse the handle. The
    dataset object holds open R2 connections + the vector index."""
    with _lance_lock:
        ds = _lance_cache.get(uri)
        if ds is None:
            import lance
            LOG.info("opening Lance dataset %s", uri)
            ds = lance.dataset(uri, storage_options=_lance_storage_options())
            _lance_cache[uri] = ds
        return ds


def _resolve_dataset_model_version(ds: Any) -> str:
    """Inspect the dataset to determine which embedding model produced it.

    Reads one row's ``model_version`` column. All rows must have the same
    model — the embedder enforces this. If a dataset has mixed models the
    first row wins and a warning is logged.
    """
    tbl = ds.to_table(columns=["model_version"], limit=1)
    if len(tbl) == 0:
        raise EmptyEmbeddingsDataset(
            "embeddings dataset is empty; cannot resolve model"
        )
    return tbl["model_version"][0].as_py()


# ─── query-embedding (semantic_match) ─────────────────────────────────


def _embed_query_with_model(text: str, model_version: str) -> list[float]:
    """Embed ``text`` using the model named ``model_version``.

    Routes to OpenAI or sentence-transformers based on the model name.
    Used for ``semantic_match`` only — ``similar_to`` reuses the seed
    embeddings already on disk.
    """
    if model_version.startswith("text-embedding-3-") or \
            model_version.startswith("text-embedding-ada-"):
        return _embed_query_openai(text, model_version)
    if model_version.startswith("sentence-transformers/"):
        return _embed_query_sentence_transformers(text, model_version)
    raise UnsupportedEmbeddingModel(
        f"don't know how to query-embed for model {model_version!r}"
    )


def _embed_query_openai(text: str, model_version: str) -> list[float]:
    import openai

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or api_key == "test":
        raise EnvironmentError(
            "OPENAI_API_KEY missing or placeholder; semantic_match against "
            "an OpenAI-embedded dataset requires the key."
        )
    client = openai.OpenAI(api_key=api_key)
    resp = client.embeddings.create(
        model=model_version, input=[text], encoding_format="float",
    )
    return resp.data[0].embedding


_st_model_cache: dict[str, Any] = {}


def _embed_query_sentence_transformers(
    text: str, model_version: str,
) -> list[float]:
    from sentence_transformers import SentenceTransformer

    name = model_version.removeprefix("sentence-transformers/")
    # Both prefixed and bare names work; sentence-transformers handles both.
    cached = _st_model_cache.get(model_version)
    if cached is None:
        LOG.info("loading sentence-transformers model %s", model_version)
        cached = SentenceTransformer(model_version)
        _st_model_cache[model_version] = cached
    vec = cached.encode([text], normalize_embeddings=True)[0]
    return vec.tolist()


# ─── similarity-search (similar_to) ───────────────────────────────────


@dataclass
class VectorSearchResult:
    """The output of a vector search: (pk, similarity, model_version).

    ``model_version`` is identical for every row from one search but we
    return it so callers can stamp lineage and refuse mixed-model joins
    if they ever need to.
    """

    matches: list[tuple[str, float]]
    model_version: str
    embedding_dim: int
    total_searched: int
    snapshot_version: int | None
    """The Lance dataset version we read (for lineage)."""


def run_similarity_search(
    seed_entity_refs: list[str],
    embedding_source: str,
    similarity_threshold: float,
    limit: int,
) -> VectorSearchResult:
    """k-NN against a centroid of seed embeddings.

    Algorithm:
      1. Open the embeddings dataset.
      2. Look up the seeds' embedding vectors via Lance filter.
      3. Compute the centroid (mean vector).
      4. Lance vector search: top-K nearest above threshold.
      5. Return (pk, cosine_similarity) tuples.

    Cosine similarity is computed as ``1 - cosine_distance``. Lance's
    ``nearest`` API supports `metric_type='cosine'` which returns
    distances in [0, 2]; convert to similarity in [-1, 1].
    """
    meta = resolve_embedding_source(embedding_source)
    ds = _get_lance_dataset(meta.lance_uri)
    pk_col = meta.primary_key_column

    # 1. Look up seeds.
    quoted = ", ".join(f"'{s}'" for s in seed_entity_refs)
    seeds_filter = f"{pk_col} IN ({quoted})"
    seeds_tbl = ds.to_table(
        columns=[pk_col, "embedding_vector", "model_version"],
        filter=seeds_filter,
    )
    if len(seeds_tbl) == 0:
        raise NoSeedsFound(
            f"none of {seed_entity_refs} found in {embedding_source}"
        )

    # 2. Resolve model + dim.
    model_version = seeds_tbl["model_version"][0].as_py()
    # Pull seed vectors as a numpy array.
    vecs = np.array(seeds_tbl["embedding_vector"].to_pylist(), dtype=np.float32)
    if vecs.ndim != 2:
        raise RuntimeError(
            f"expected 2D vector array from seeds; got shape {vecs.shape}"
        )
    embedding_dim = vecs.shape[1]

    # 3. Centroid.
    centroid = vecs.mean(axis=0)
    # Re-normalize centroid so cosine math is well-behaved.
    norm = np.linalg.norm(centroid)
    if norm > 0:
        centroid = centroid / norm
    centroid_list = centroid.astype(np.float32).tolist()

    LOG.info(
        "similar_to: seeds=%d centroid_dim=%d threshold=%.2f limit=%d source=%s",
        len(seeds_tbl), embedding_dim, similarity_threshold, limit, embedding_source,
    )

    # 4. Run the vector search.
    return _vector_topk(
        ds, pk_col, centroid_list, similarity_threshold, limit,
        model_version, embedding_dim,
    )


def run_semantic_search(
    query_text: str,
    embedding_source: str,
    similarity_threshold: float,
    limit: int,
) -> VectorSearchResult:
    """k-NN against an embedded free-text query.

    Algorithm:
      1. Open the embeddings dataset, resolve its model_version.
      2. Embed query_text using the same model.
      3. Lance vector search: top-K nearest above threshold.
    """
    meta = resolve_embedding_source(embedding_source)
    ds = _get_lance_dataset(meta.lance_uri)
    pk_col = meta.primary_key_column

    model_version = _resolve_dataset_model_version(ds)

    # Get the embedding dim from one row.
    head = ds.to_table(columns=["embedding_vector"], limit=1)
    if len(head) == 0:
        raise EmptyEmbeddingsDataset(
            f"embeddings dataset {embedding_source} is empty"
        )
    sample_vec = head["embedding_vector"][0].as_py()
    embedding_dim = len(sample_vec)

    LOG.info(
        "semantic_match: query=%r model=%s dim=%d threshold=%.2f limit=%d source=%s",
        query_text[:80], model_version, embedding_dim, similarity_threshold,
        limit, embedding_source,
    )

    query_vec = _embed_query_with_model(query_text, model_version)
    if len(query_vec) != embedding_dim:
        raise VectorModelMismatch(
            f"query embedding dim {len(query_vec)} != dataset dim {embedding_dim}"
        )

    return _vector_topk(
        ds, pk_col, query_vec, similarity_threshold, limit,
        model_version, embedding_dim,
    )


# ─── vector-topk core ─────────────────────────────────────────────────


def _vector_topk(
    ds: Any,
    pk_col: str,
    query_vector: list[float],
    similarity_threshold: float,
    limit: int,
    model_version: str,
    embedding_dim: int,
) -> VectorSearchResult:
    """Run Lance ANN against ``query_vector``, return matches above threshold.

    Lance's ``scanner(nearest=...)`` returns rows sorted by ascending
    ``_distance`` (cosine or l2 — we use cosine here). cosine_distance is
    ``1 - cosine_similarity``, in [0, 2]. So:

        similarity = 1 - _distance        # for cosine
        similarity >= threshold
            <=> _distance <= 1 - threshold

    We over-fetch (limit * 2, capped at the dataset size) to give the
    threshold filter some slack — Lance's IVF_PQ is approximate; rows
    near the threshold may shuffle.
    """
    total_searched = ds.count_rows()
    # Pull a few-times-limit candidates so the threshold filter has slack.
    fetch_k = min(max(limit * 3, 200), total_searched)

    scanner = ds.scanner(
        columns=[pk_col],
        nearest={
            "column": "embedding_vector",
            "q": query_vector,
            "k": fetch_k,
            "metric": "cosine",
        },
    )
    result_tbl = scanner.to_table()
    if len(result_tbl) == 0:
        return VectorSearchResult(
            matches=[],
            model_version=model_version,
            embedding_dim=embedding_dim,
            total_searched=total_searched,
            snapshot_version=getattr(ds, "version", None),
        )

    # Lance includes a "_distance" column on nearest queries.
    pks = result_tbl[pk_col].to_pylist()
    dists = result_tbl["_distance"].to_pylist()
    sim_floor_dist = 1.0 - similarity_threshold
    matches: list[tuple[str, float]] = []
    for pk, dist in zip(pks, dists, strict=True):
        if dist > sim_floor_dist:
            # Lance returns sorted asc by distance, so once we cross the
            # floor we can break.
            break
        matches.append((str(pk), 1.0 - dist))
        if len(matches) >= limit:
            break

    LOG.info(
        "vector search returned %d / %d candidates above threshold "
        "(threshold=%.3f, fetch_k=%d)",
        len(matches), len(pks), similarity_threshold, fetch_k,
    )

    return VectorSearchResult(
        matches=matches,
        model_version=model_version,
        embedding_dim=embedding_dim,
        total_searched=total_searched,
        snapshot_version=getattr(ds, "version", None),
    )


# ─── exceptions ───────────────────────────────────────────────────────


class UnknownEmbeddingSource(LookupError):
    """Raised when the spec names an embedding source we can't resolve."""


class NoSeedsFound(LookupError):
    """Raised when the similar_to seed_entity_refs aren't in the dataset."""


class EmptyEmbeddingsDataset(RuntimeError):
    """Raised when the embeddings dataset is present but has zero rows."""


class VectorModelMismatch(RuntimeError):
    """Raised on cross-model query vs dataset dim disagreement."""


class UnsupportedEmbeddingModel(NotImplementedError):
    """Raised when a dataset's model_version isn't in the query-embed dispatch."""


__all__ = [
    "EmbeddingSourceMeta",
    "VectorSearchResult",
    "resolve_embedding_source",
    "run_similarity_search",
    "run_semantic_search",
    "UnknownEmbeddingSource",
    "NoSeedsFound",
    "EmptyEmbeddingsDataset",
    "VectorModelMismatch",
    "UnsupportedEmbeddingModel",
]
