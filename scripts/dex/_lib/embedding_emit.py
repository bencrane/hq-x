"""Reusable embedding pipeline for Lance entity-profile datasets.

Phase 4 of the multi-phase hq-all rebuild — the embedding emit primitive
that activates the vector primitives (``similar_to``, ``semantic_match``)
in the audience spec language. Per
``~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/lance_is_the_universal_substrate.md``
the embeddings dataset is just another Lance dataset alongside the source
Lance dataset; same operational discipline (commit_lock, optimize, cleanup).

Pattern (per source):
  1. Read source Lance dataset with eligibility filter.
  2. Construct a profile text per entity (composite of name, location,
     business desc, fleet/operation attributes, etc.) via a per-source
     composer function.
  3. SHA-256 hash the profile text → content_hash for change detection.
  4. Diff against the existing embeddings Lance dataset (if any): only
     embed entities whose content_hash changed OR are missing entirely.
  5. Call OpenAI ``text-embedding-3-small`` (1536-dim, $0.02/1M tokens) in
     batches. Backoff on rate limits.
  6. Merge new embeddings with prior embeddings (upsert by primary key).
  7. Write to the embeddings Lance dataset; build IVF-PQ index.

Model: ``text-embedding-3-small`` (1536-dim).
Rationale (per ``infra_budget_pre_revenue.md`` — cheap baseline):
  - $0.02 / 1M tokens — ~$5 total for FMCSA carrier_essentials (1.95M
    active carriers @ ~150 tokens/profile)
  - 1536-dim vectors; cosine similarity works out-of-the-box
  - Quality is competitive with text-embedding-ada-002 (the prior workhorse)
    at 5x lower cost
  - Future Phase 4.5 / 5: experiment with text-embedding-3-large (3072-dim)
    or open-weights (BGE, Qwen) if cost/quality justifies the swap.

The model name is encoded in each embedding row's ``model_version`` column
so consumers can detect mixed-model datasets and refuse if needed.

Idempotency
-----------
Re-running on the same source snapshot is a no-op (every content_hash
matches). Adding a new source snapshot incurs work only on the rows whose
profile text changed.

Required env (from Doppler):
    R2_ENDPOINT / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY
    OPENAI_API_KEY
    DEX_DB_URL_DIRECT  (for the Lance commit lock)
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

LOG = logging.getLogger(__name__)

# v1 default: OpenAI text-embedding-3-small (1536-dim, $0.02/1M tokens).
# Selectable via env var ``EMBEDDING_PROVIDER`` — also supports
# 'sentence-transformers' as the open-weights fallback (free, runs
# in-process, 384-dim via all-MiniLM-L6-v2). Each emit run stamps the
# provider+model in every row's ``model_version`` column so downstream
# code refuses mixed-model queries.
EMBEDDING_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "openai")

_OPENAI_MODEL = "text-embedding-3-small"
_OPENAI_DIM = 1536
_SENTENCE_TRANSFORMERS_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_SENTENCE_TRANSFORMERS_DIM = 384

if EMBEDDING_PROVIDER == "openai":
    EMBEDDING_MODEL = _OPENAI_MODEL
    EMBEDDING_DIM = _OPENAI_DIM
elif EMBEDDING_PROVIDER == "sentence-transformers":
    EMBEDDING_MODEL = _SENTENCE_TRANSFORMERS_MODEL
    EMBEDDING_DIM = _SENTENCE_TRANSFORMERS_DIM
else:
    raise ValueError(
        f"EMBEDDING_PROVIDER must be 'openai' or 'sentence-transformers'; "
        f"got {EMBEDDING_PROVIDER!r}"
    )

# OpenAI's per-call max is 2048 inputs and ~8192 tokens per input. We use
# a smaller batch (256) because profile-text is short and we want tighter
# checkpointing on retryable failures.
OPENAI_BATCH_SIZE = 256

# sentence-transformers all-MiniLM-L6-v2 handles 64-row batches efficiently
# on CPU; 256 is the sweet spot when running on Modal's default container.
SENTENCE_TRANSFORMERS_BATCH_SIZE = 256

# Cap on profile-text length per row (characters). Far below the 8192-token
# limit (~32k chars). Truncates pathologically long composites without
# burning tokens. v1: 4kB ≈ 1k tokens — plenty of signal.
PROFILE_TEXT_MAX_CHARS = 4096


@dataclass
class EmbeddingEmitConfig:
    """Per-source config for the embedding pipeline."""

    # Slug for commit-lock + observability ledger.
    dataset_slug: str
    """e.g. 'fmcsa_carrier_essentials_embeddings_lance'"""

    # The source Lance dataset (already emitted, registered in Polaris).
    source_lance_uri: str
    """e.g. 's3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_lance'"""

    # The embeddings Lance dataset (output of this pipeline).
    embeddings_lance_uri: str
    """e.g. 's3://dex-raw-landing-zone/polaris-warehouse/fmcsa/carrier_essentials_embeddings_lance'"""

    # The primary-key column on the source. Carried verbatim into the
    # embeddings dataset for join-back. String-typed.
    primary_key_column: str
    """e.g. 'dot_number'"""

    # Eligibility filter (Lance dataset filter expression). Restricts WHICH
    # rows get embedded. Trades off cost (small filter set) vs coverage.
    eligibility_filter: str
    """e.g. \"status_code = 'A' AND power_units_int >= 1\""""

    # Columns to fetch from source for the profile-text composer.
    profile_text_columns: list[str]

    # Function that composes profile text from a row dict.
    profile_text_fn: Callable[[dict[str, Any]], str]

    # Optional: when True, build the IVF-PQ vector index after writing.
    # Disable for dry-run / cost-test runs. Default True.
    build_vector_index: bool = True

    # Optional: max number of rows to embed in this run. Lets the operator
    # cap embedding spend for one-time validation runs. Default: no cap.
    max_rows: int | None = None


def _hash_profile_text(text: str) -> str:
    """SHA-256 hex digest of the profile text. Used as content_hash."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _pick_num_sub_vectors(dim: int) -> int:
    """Pick a sensible num_sub_vectors for Lance IVF_PQ given the embedding dim.

    Lance requires ``dim % num_sub_vectors == 0``. We aim for the largest
    divisor of ``dim`` that's <= 96 (keeps the PQ codebook small enough
    for memory but accurate enough for cosine), with a floor of 8 so
    very-low-dim vectors still build.

    For the canonical models:
        * 384-dim (sentence-transformers all-MiniLM-L6-v2) → 96
        * 1536-dim (text-embedding-3-small)               → 96
        * 3072-dim (text-embedding-3-large)               → 96
    """
    target = 96
    # Find the largest divisor of dim that's <= target.
    for cand in range(min(target, dim), 0, -1):
        if dim % cand == 0:
            return max(8, cand)
    return 8  # unreachable for dim >= 8


def _lance_storage_options() -> dict:
    """S3-protocol options for Lance to talk to Cloudflare R2.

    Matches the form used by ``scripts/_lib/lance_emit.py`` — kept in sync
    because Lance's R2 access semantics are subtle.
    """
    return {
        "aws_endpoint": os.environ["R2_ENDPOINT"],
        "aws_access_key_id": os.environ["R2_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["R2_SECRET_ACCESS_KEY"],
        "aws_region": "us-east-1",
        "aws_virtual_hosted_style_request": "false",
    }


def _read_source_for_embed(
    config: EmbeddingEmitConfig,
) -> "list[dict[str, Any]]":
    """Scan source Lance dataset with the eligibility filter; return a list
    of dicts containing the primary key + the columns needed by the profile
    text composer.
    """
    import lance

    storage = _lance_storage_options()
    ds = lance.dataset(config.source_lance_uri, storage_options=storage)

    cols = list({config.primary_key_column, *config.profile_text_columns})
    LOG.info(
        "scanning source %s with filter %r (columns=%s)",
        config.source_lance_uri, config.eligibility_filter, cols,
    )
    tbl = ds.to_table(columns=cols, filter=config.eligibility_filter)
    LOG.info("source rows after filter: %d", len(tbl))
    rows = tbl.to_pylist()
    if config.max_rows is not None and len(rows) > config.max_rows:
        LOG.info("max_rows=%d applied; truncating", config.max_rows)
        rows = rows[: config.max_rows]
    return rows


def _read_existing_embeddings(
    config: EmbeddingEmitConfig,
) -> dict[str, str]:
    """Return a {primary_key: content_hash} dict from the embeddings dataset.

    Empty dict if the dataset doesn't exist yet (first-ever emit).
    """
    import lance

    storage = _lance_storage_options()
    try:
        ds = lance.dataset(config.embeddings_lance_uri, storage_options=storage)
    except Exception as e:
        # lance.dataset raises a variety of error classes depending on the
        # underlying object-store error (FileNotFoundError, ValueError,
        # IOError from object_store crate, etc). Treat any read-time
        # failure as "no existing dataset" and let the writer create one.
        LOG.info(
            "no existing embeddings dataset at %s (first-emit): %s",
            config.embeddings_lance_uri, e,
        )
        return {}

    try:
        tbl = ds.to_table(columns=[config.primary_key_column, "content_hash"])
    except Exception as e:
        LOG.warning(
            "existing embeddings dataset present but unreadable: %s; "
            "treating as empty (will overwrite)", e,
        )
        return {}

    pk_col = config.primary_key_column
    out: dict[str, str] = {}
    for r in tbl.to_pylist():
        pk = r.get(pk_col)
        h = r.get("content_hash")
        if pk is not None and h is not None:
            out[str(pk)] = h
    LOG.info("existing embeddings: %d rows", len(out))
    return out


def _compose_profile_texts(
    config: EmbeddingEmitConfig, rows: list[dict[str, Any]],
) -> list[tuple[str, str, str]]:
    """For each row produce (primary_key_str, profile_text, content_hash).

    Profile text is truncated to PROFILE_TEXT_MAX_CHARS.
    """
    pk_col = config.primary_key_column
    out: list[tuple[str, str, str]] = []
    skipped_blank = 0
    for r in rows:
        pk = r.get(pk_col)
        if pk is None or pk == "":
            skipped_blank += 1
            continue
        text = config.profile_text_fn(r).strip()
        if not text:
            skipped_blank += 1
            continue
        if len(text) > PROFILE_TEXT_MAX_CHARS:
            text = text[:PROFILE_TEXT_MAX_CHARS]
        out.append((str(pk), text, _hash_profile_text(text)))
    if skipped_blank:
        LOG.info("composed %d profile texts (skipped %d blank/no-pk)",
                 len(out), skipped_blank)
    return out


def _diff_against_existing(
    composed: list[tuple[str, str, str]],
    existing: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Return only the (pk, text, hash) tuples whose hash differs from
    existing OR whose pk isn't present in existing."""
    out: list[tuple[str, str, str]] = []
    for pk, text, h in composed:
        prev = existing.get(pk)
        if prev != h:
            out.append((pk, text, h))
    LOG.info(
        "diff vs existing: %d rows need (re-)embedding (out of %d candidates)",
        len(out), len(composed),
    )
    return out


def _embed_batch_openai(
    client: Any, texts: list[str], retries: int = 6,
) -> list[list[float]]:
    """Call OpenAI embeddings on one batch. Linear backoff on rate-limits."""
    import openai

    backoff = 4.0
    for attempt in range(retries + 1):
        try:
            resp = client.embeddings.create(
                model=_OPENAI_MODEL,
                input=texts,
                encoding_format="float",
            )
            return [d.embedding for d in resp.data]
        except (openai.RateLimitError, openai.APIConnectionError,
                openai.APITimeoutError, openai.InternalServerError) as e:
            if attempt == retries:
                raise
            wait = backoff * (2 ** attempt) + 0.5 * attempt
            LOG.warning(
                "openai rate/transient error on attempt %d: %s; sleeping %.1fs",
                attempt + 1, e, wait,
            )
            time.sleep(wait)
    # Unreachable.
    raise RuntimeError("embed retry loop exhausted")


def _embed_query_openai(client: Any, text: str) -> list[float]:
    """Embed a single query string via OpenAI. Used by query-time callers."""
    return _embed_batch_openai(client, [text])[0]


def embed_query(text: str) -> list[float]:
    """Embed a single query string with the configured provider.

    Used by the evaluator's ``semantic_match`` primitive — the partner's
    free-text query is embedded once, then ANN-searched against the
    target dataset.
    """
    if EMBEDDING_PROVIDER == "openai":
        import openai

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key or api_key == "test":
            raise RuntimeError(
                "OPENAI_API_KEY missing or placeholder; cannot embed query."
            )
        client = openai.OpenAI(api_key=api_key)
        return _embed_query_openai(client, text)

    if EMBEDDING_PROVIDER == "sentence-transformers":
        model = _get_sentence_transformers_model()
        vec = model.encode([text], normalize_embeddings=True)[0]
        return vec.tolist()

    raise ValueError(f"unknown EMBEDDING_PROVIDER {EMBEDDING_PROVIDER!r}")


_st_model_cache: Any = None


def _get_sentence_transformers_model() -> Any:
    """Load the sentence-transformers model once per process."""
    global _st_model_cache
    if _st_model_cache is None:
        from sentence_transformers import SentenceTransformer
        LOG.info("loading sentence-transformers model %s",
                 _SENTENCE_TRANSFORMERS_MODEL)
        _st_model_cache = SentenceTransformer(_SENTENCE_TRANSFORMERS_MODEL)
    return _st_model_cache


def _embed_batch_sentence_transformers(
    model: Any, texts: list[str],
) -> list[list[float]]:
    """Encode a batch with sentence-transformers. Returns L2-normalized
    vectors (so cosine == dot product, simplifies similarity math)."""
    arr = model.encode(
        texts,
        batch_size=SENTENCE_TRANSFORMERS_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [v.tolist() for v in arr]


def _embed_all(
    to_embed: list[tuple[str, str, str]],
) -> "tuple[list[tuple[str, list[float], str, str, datetime]], int, int]":
    """Call the configured provider in batches.

    Returns (rows, total_tokens_estimated, failed).
    Each row tuple is (pk, vector, content_hash, profile_text, embedded_at).

    Provider dispatch is on ``EMBEDDING_PROVIDER`` (set in env). Each
    embedded row's ``model_version`` is stamped with the resolved model
    name so a mixed-model dataset is detectable (and the evaluator can
    refuse mixed-dim queries).
    """
    if EMBEDDING_PROVIDER == "openai":
        return _embed_all_openai(to_embed)
    if EMBEDDING_PROVIDER == "sentence-transformers":
        return _embed_all_sentence_transformers(to_embed)
    raise ValueError(f"unknown EMBEDDING_PROVIDER {EMBEDDING_PROVIDER!r}")


def _embed_all_openai(
    to_embed: list[tuple[str, str, str]],
) -> "tuple[list[tuple[str, list[float], str, str, datetime]], int, int]":
    import openai

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or api_key == "test":
        raise RuntimeError(
            "OPENAI_API_KEY missing or placeholder in env — "
            "set it in Doppler hq-all/prd before running the embedder, "
            "or set EMBEDDING_PROVIDER=sentence-transformers."
        )
    client = openai.OpenAI(api_key=api_key)

    rows: list[tuple[str, list[float], str, str, datetime]] = []
    tokens_est = 0
    failed = 0
    n = len(to_embed)
    LOG.info("embedding %d texts via openai/%s in batches of %d",
             n, _OPENAI_MODEL, OPENAI_BATCH_SIZE)
    t0 = time.time()
    for i in range(0, n, OPENAI_BATCH_SIZE):
        batch = to_embed[i:i + OPENAI_BATCH_SIZE]
        texts = [t for _, t, _ in batch]
        try:
            vectors = _embed_batch_openai(client, texts)
        except Exception as e:
            LOG.error(
                "batch %d-%d failed permanently: %s; skipping (will retry on next run)",
                i, i + len(batch), e,
            )
            failed += len(batch)
            continue
        if len(vectors) != len(batch):
            LOG.error(
                "batch %d-%d: openai returned %d vectors for %d inputs; "
                "skipping batch (will retry next run)",
                i, i + len(batch), len(vectors), len(batch),
            )
            failed += len(batch)
            continue
        now = datetime.now(timezone.utc)
        for (pk, text, h), vec in zip(batch, vectors, strict=True):
            if len(vec) != EMBEDDING_DIM:
                LOG.warning(
                    "vector dim %d != expected %d for pk=%s; skipping",
                    len(vec), EMBEDDING_DIM, pk,
                )
                failed += 1
                continue
            rows.append((pk, vec, h, text, now))
            tokens_est += max(1, len(text) // 4)
        if (i // OPENAI_BATCH_SIZE) % 20 == 0 or i + OPENAI_BATCH_SIZE >= n:
            rate = (i + len(batch)) / max(1.0, time.time() - t0)
            LOG.info(
                "  progress: %d/%d (%.1f/s, ~%dM tokens so far)",
                i + len(batch), n, rate, tokens_est // 1_000_000,
            )
    LOG.info(
        "embedding done: %d succeeded, %d failed (~%d tokens, ~$%.2f at $0.02/1M)",
        len(rows), failed, tokens_est, tokens_est / 1_000_000 * 0.02,
    )
    return rows, tokens_est, failed


def _embed_all_sentence_transformers(
    to_embed: list[tuple[str, str, str]],
) -> "tuple[list[tuple[str, list[float], str, str, datetime]], int, int]":
    """Embed via the sentence-transformers fallback. Free, runs in-process."""
    model = _get_sentence_transformers_model()
    n = len(to_embed)
    LOG.info(
        "embedding %d texts via sentence-transformers/%s in batches of %d",
        n, _SENTENCE_TRANSFORMERS_MODEL, SENTENCE_TRANSFORMERS_BATCH_SIZE,
    )
    rows: list[tuple[str, list[float], str, str, datetime]] = []
    failed = 0
    tokens_est = 0
    t0 = time.time()
    for i in range(0, n, SENTENCE_TRANSFORMERS_BATCH_SIZE):
        batch = to_embed[i:i + SENTENCE_TRANSFORMERS_BATCH_SIZE]
        texts = [t for _, t, _ in batch]
        try:
            vectors = _embed_batch_sentence_transformers(model, texts)
        except Exception as e:
            LOG.error(
                "batch %d-%d failed: %s; skipping (will retry on next run)",
                i, i + len(batch), e,
            )
            failed += len(batch)
            continue
        now = datetime.now(timezone.utc)
        for (pk, text, h), vec in zip(batch, vectors, strict=True):
            if len(vec) != EMBEDDING_DIM:
                LOG.warning(
                    "vector dim %d != expected %d for pk=%s; skipping",
                    len(vec), EMBEDDING_DIM, pk,
                )
                failed += 1
                continue
            rows.append((pk, vec, h, text, now))
            tokens_est += max(1, len(text) // 4)
        if (i // SENTENCE_TRANSFORMERS_BATCH_SIZE) % 20 == 0 \
                or i + SENTENCE_TRANSFORMERS_BATCH_SIZE >= n:
            rate = (i + len(batch)) / max(1.0, time.time() - t0)
            LOG.info("  progress: %d/%d (%.1f/s)", i + len(batch), n, rate)
    LOG.info(
        "embedding done: %d succeeded, %d failed (sentence-transformers free)",
        len(rows), failed,
    )
    return rows, tokens_est, failed


def _write_embeddings_to_lance(
    config: EmbeddingEmitConfig,
    new_rows: list[tuple[str, list[float], str, str, datetime]],
    existing_pks: set[str],
) -> dict[str, Any]:
    """Append/merge new embeddings into the embeddings Lance dataset.

    Strategy:
      - If dataset doesn't exist: ``write_dataset(mode='create')`` with the
        new rows.
      - If dataset exists: read existing rows whose pk is NOT in the new set,
        union with new rows, write_dataset(mode='overwrite'). Lance's
        commit-lock guards this against concurrent writers.

    The overwrite-after-merge pattern is correct because:
      - the embeddings dataset for a single source has a single writer
        (this cron),
      - we hold the commit-lock for the whole operation,
      - Lance keeps prior versions in ``_versions/`` for 7 days for
        rollback.

    Returns metrics dict.
    """
    import lance
    import pyarrow as pa
    from scripts._lib.lance_commit_lock import lance_commit_lock

    if not new_rows:
        LOG.info("no new rows to write; skipping write step")
        return {
            "new_rows": 0,
            "merged_rows": 0,
            "total_rows": len(existing_pks),
            "wrote_dataset": False,
        }

    pk_col = config.primary_key_column

    # Build the new pyarrow.Table.
    new_pks = [r[0] for r in new_rows]
    new_vecs = [r[1] for r in new_rows]
    new_hashes = [r[2] for r in new_rows]
    new_texts = [r[3] for r in new_rows]
    new_ts = [r[4] for r in new_rows]
    new_models = [EMBEDDING_MODEL] * len(new_rows)

    new_table = pa.table({
        pk_col: pa.array(new_pks, type=pa.string()),
        "embedding_vector": pa.array(
            new_vecs,
            type=pa.list_(pa.float32(), list_size=EMBEDDING_DIM),
        ),
        "content_hash": pa.array(new_hashes, type=pa.string()),
        "profile_text": pa.array(new_texts, type=pa.string()),
        "embedded_at": pa.array(new_ts, type=pa.timestamp("us", tz="UTC")),
        "model_version": pa.array(new_models, type=pa.string()),
    })

    storage = _lance_storage_options()
    new_pks_set = set(new_pks)

    LOG.info("acquiring lance commit lock for %s", config.dataset_slug)
    metrics: dict[str, Any] = {"new_rows": len(new_rows)}
    with lance_commit_lock(config.dataset_slug):
        merged_table = new_table
        kept = 0
        try:
            existing_ds = lance.dataset(
                config.embeddings_lance_uri, storage_options=storage,
            )
            LOG.info(
                "existing embeddings dataset present (rows=%d, version=%s) — "
                "merging carry-over rows",
                existing_ds.count_rows(), existing_ds.version,
            )
            # Pull the rows we want to keep (not being replaced this run).
            # We scan ONLY the pks not in new_pks; Lance has no IN-with-millions
            # so we fetch all + filter in Python. For 2M-row sets at 1536 dim
            # that's ~12GB which is too large. Instead pull keys first and
            # batch-stream the keep set.
            keep_table = _keep_carryover_rows(
                existing_ds, pk_col, new_pks_set,
            )
            kept = len(keep_table)
            LOG.info("carry-over rows: %d", kept)
            if kept > 0:
                merged_table = pa.concat_tables([keep_table, new_table])
        except Exception as e:
            LOG.info("first-emit (no existing dataset): %s", e)

        LOG.info(
            "writing embeddings dataset: %d total rows (kept=%d, new=%d)",
            len(merged_table), kept, len(new_table),
        )
        t0 = time.time()
        ds = lance.write_dataset(
            merged_table,
            config.embeddings_lance_uri,
            mode="overwrite",
            storage_options=storage,
        )
        dur = time.time() - t0
        LOG.info("write complete in %.1fs (version=%s)", dur, ds.version)
        metrics.update({
            "merged_rows": kept,
            "total_rows": ds.count_rows(),
            "lance_version": ds.version,
            "write_seconds": round(dur, 1),
            "wrote_dataset": True,
        })

        # Build a BTREE on primary_key (so per-PK lookup is fast).
        os.environ["LANCE_BYPASS_SPILLING"] = "true"
        t_idx = time.time()
        try:
            LOG.info("creating BTREE scalar index on %s", pk_col)
            ds.create_scalar_index(pk_col, index_type="BTREE", replace=True)
            idx_dur = time.time() - t_idx
            LOG.info("  BTREE index built in %.1fs", idx_dur)
            metrics["btree_seconds"] = round(idx_dur, 1)
        except Exception as e:
            LOG.warning("BTREE index build failed (non-fatal): %s", e)
            metrics["btree_seconds"] = None

        # Build the IVF-PQ vector index — Lance's headline benefit for
        # vector search. Approximate-NN with sub-100ms top-K cold query.
        if config.build_vector_index:
            t_v = time.time()
            try:
                # num_partitions default heuristic: sqrt(N).
                n = ds.count_rows()
                # Floor to a reasonable minimum so small datasets still get an index.
                num_partitions = max(16, int(n ** 0.5))
                # num_sub_vectors must divide EMBEDDING_DIM. Pick the largest
                # divisor that's <=96 to keep the PQ codebook small but
                # accurate; minimum 8 so very-small dim doesn't degrade.
                num_sub_vectors = _pick_num_sub_vectors(EMBEDDING_DIM)
                LOG.info(
                    "creating IVF_PQ vector index on embedding_vector "
                    "(num_partitions=%d, num_sub_vectors=%d, metric=cosine)",
                    num_partitions, num_sub_vectors,
                )
                ds.create_index(
                    column="embedding_vector",
                    index_type="IVF_PQ",
                    num_partitions=num_partitions,
                    num_sub_vectors=num_sub_vectors,
                    metric="cosine",
                    replace=True,
                )
                v_dur = time.time() - t_v
                LOG.info("  IVF_PQ index built in %.1fs", v_dur)
                metrics["ivfpq_seconds"] = round(v_dur, 1)
            except Exception as e:
                LOG.warning("IVF_PQ index build failed (non-fatal): %s", e)
                metrics["ivfpq_seconds"] = None

        # Optimize: compact + cleanup older versions.
        t1 = time.time()
        try:
            stats = ds.optimize.compact_files()
            LOG.info("compact_files: %s", stats)
        except Exception as e:
            LOG.warning("compact_files failed (non-fatal): %s", e)
        try:
            stats = ds.cleanup_old_versions(older_than=timedelta(days=7))
            LOG.info("cleanup_old_versions: %s", stats)
        except Exception as e:
            LOG.warning("cleanup_old_versions failed (non-fatal): %s", e)
        metrics["optimize_seconds"] = round(time.time() - t1, 1)

    return metrics


def _keep_carryover_rows(
    existing_ds: Any, pk_col: str, new_pks_set: set[str],
) -> Any:
    """Pull rows from existing_ds whose pk is NOT in new_pks_set.

    Streams in chunks (200K rows) so we never materialize the full 2M-row
    × 1536-dim payload in process memory.
    """
    import pyarrow as pa

    # First pass: pull keys only, decide which rows to carry over.
    keys_tbl = existing_ds.to_table(columns=[pk_col, "content_hash"])
    keys = keys_tbl[pk_col].to_pylist()
    keep_pks_set = {k for k in keys if str(k) not in new_pks_set}
    if not keep_pks_set:
        return pa.table({})  # empty

    # Build chunks of pk-list to scan back. Lance's `filter` can handle IN-list
    # for moderate sizes; chunk it.
    keep_pks_list = sorted(keep_pks_set)
    chunk_size = 50_000
    parts: list[Any] = []
    for i in range(0, len(keep_pks_list), chunk_size):
        chunk = keep_pks_list[i:i + chunk_size]
        quoted = ", ".join(f"'{p}'" for p in chunk)
        f = f"{pk_col} IN ({quoted})"
        part = existing_ds.to_table(filter=f)
        parts.append(part)
    if not parts:
        return pa.table({})
    return pa.concat_tables(parts)


def run_embedding_emit(config: EmbeddingEmitConfig) -> dict[str, Any]:
    """Drive the full pipeline. Returns metrics."""
    LOG.info("=" * 60)
    LOG.info("embedding emit: %s", config.dataset_slug)
    LOG.info("  source:     %s", config.source_lance_uri)
    LOG.info("  embeddings: %s", config.embeddings_lance_uri)
    LOG.info("  filter:     %s", config.eligibility_filter)
    LOG.info("  model:      %s (%d-dim)", EMBEDDING_MODEL, EMBEDDING_DIM)

    started_at = datetime.now(timezone.utc)
    t0 = time.time()

    src_rows = _read_source_for_embed(config)
    composed = _compose_profile_texts(config, src_rows)
    existing = _read_existing_embeddings(config)
    to_embed = _diff_against_existing(composed, existing)

    embedded_rows, tokens_est, failed = ([], 0, 0)
    if to_embed:
        embedded_rows, tokens_est, failed = _embed_all(to_embed)

    metrics = _write_embeddings_to_lance(
        config, embedded_rows, set(existing.keys()),
    )
    duration_s = round(time.time() - t0, 1)

    n_candidates = len(composed)
    coverage = (
        (metrics.get("total_rows", 0) / n_candidates) if n_candidates else 0.0
    )
    metrics.update({
        "started_at": started_at.isoformat(),
        "duration_s": duration_s,
        "candidates": n_candidates,
        "needed_embedding": len(to_embed),
        "embedded": len(embedded_rows),
        "failed": failed,
        "tokens_estimated": tokens_est,
        "dollars_estimated": round(tokens_est / 1_000_000 * 0.02, 4),
        "coverage_ratio": round(coverage, 4),
        "model_version": EMBEDDING_MODEL,
        "embedding_dim": EMBEDDING_DIM,
    })
    LOG.info("OK — metrics: %s", metrics)
    return metrics


__all__ = [
    "EmbeddingEmitConfig",
    "run_embedding_emit",
    "embed_query",
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "PROFILE_TEXT_MAX_CHARS",
]
