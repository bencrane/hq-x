# Audience-spec vector primitives (Phase 4)

> **Status:** active. `similar_to` and `semantic_match` are evaluable.

Per `~/.claude/projects/-Users-benjamincrane-hq-all/memory/project/partner_intent_lives_in_the_spec.md`:
the partner's signed spec captures **minimal intent**, the platform's
intermediation job is to fill in everything else from the data lake. The
vector primitives are how the spec language captures "find more like
these" and "match this description" without forcing the partner to
enumerate every attribute they care about.

Both primitives query a registered embeddings Lance dataset (per
`apps/data-engine-x/docs/embeddings-pipeline.md`). The evaluator's
vector layer dispatches to Lance's IVF_PQ index — sub-100ms top-K
cold-start once the dataset is open in process.

## `similar_to` — k-NN against partner-supplied seeds

```python
AudienceSpec(
    sources=[CatalogRef(namespace='fmcsa', table='carrier_essentials_lance')],
    similar_to=SimilarityClause(
        seed_entity_refs=['1234567', '7654321', '...'],  # 5+ seed DOTs
        embedding_source='fmcsa.carrier_essentials_embeddings_lance',
        similarity_threshold=0.7,  # cosine similarity floor
        limit=1000,
    ),
)
```

**How it works at compile time:**

1. Look up the 5 seeds' embeddings in the dataset (filter by primary key).
2. Compute the centroid (mean vector) and L2-normalize it.
3. Run Lance's `nearest` query against the centroid with metric='cosine'.
4. Convert `_distance` (cosine_distance, in [0, 2]) to similarity
   (`1 - distance`, in [-1, 1]).
5. Filter to results above `similarity_threshold`; cap at `limit`.
6. Return the matched primary keys.

The evaluator then composes SQL:

```sql
SELECT * FROM fmcsa_carrier_essentials_lance
WHERE "dot_number" IN (?, ?, ?, ...)  -- matched DOTs from vector search
  AND <scalar filters from spec.filters>
```

**Partner usage example:**

> "Find me more trucking carriers like the 50 I've already underwritten
> SBA loans for."

Partner provides the 50 DOT numbers (their existing book). The evaluator
finds carriers semantically similar to that cohort — same operating
shape, fleet size, sector signals. Threshold 0.7 + limit 500 typically
yields 100-500 high-quality candidates.

## `semantic_match` — match a free-text description

```python
AudienceSpec(
    sources=[CatalogRef(namespace='fmcsa', table='carrier_essentials_lance')],
    semantic_match=SemanticPredicate(
        query_text='trucking carriers that haul oversized loads in the Mountain West',
        embedding_source='fmcsa.carrier_essentials_embeddings_lance',
        similarity_threshold=0.5,  # looser default than similar_to
        limit=1000,
    ),
)
```

**How it works at compile time:**

1. Resolve the dataset's `model_version` (from one row's stamp).
2. Embed `query_text` via the same model. Routes by model name prefix:
   - `text-embedding-3-*` / `text-embedding-ada-*` → OpenAI API call.
   - `sentence-transformers/*` → local model load + encode.
3. Lance `nearest` query → matches.
4. Filter by threshold; cap at limit.

The evaluator composes SQL the same way as `similar_to`.

**Partner usage example:**

> "Carriers specializing in cold-chain refrigerated transport in
> California."

The partner doesn't have to know FMCSA's column names or codes — the
spec captures the intent in plain English. The evaluator finds carriers
whose profile-text semantically resembles the query.

## Threshold tuning

The two primitives have different default thresholds because the
similarity distributions differ:

- `similar_to`: seeds → centroid → ANN. The centroid is well-defined
  and stable, so high-similarity matches are common. Default 0.7 is
  "strict" — matches are visibly the same kind of carrier.
- `semantic_match`: free-text → embedded query → ANN. The query is one
  side of a much wider semantic gap; matches will mostly be in the
  0.4-0.7 range. Default 0.5 is the right floor.

The partner can override both. The validators enforce `0.0 <= x <= 1.0`.

## Composition with scalar filters

Vector primitives AND-compose with `filters`. Example: "carriers similar
to my book, restricted to TX":

```python
AudienceSpec(
    sources=[CatalogRef(namespace='fmcsa', table='carrier_essentials_lance')],
    similar_to=SimilarityClause(...),
    filters=[
        ScalarPredicate(column='phy_state', op='eq', value='TX'),
    ],
)
```

The compiled SQL:

```sql
SELECT * FROM fmcsa_carrier_essentials_lance
WHERE "dot_number" IN (?, ?, ...)
  AND "phy_state" = ?
```

The scalar filter operates on the **primary-source** Lance dataset, not
the embeddings dataset. This is by design: the embeddings dataset stores
only the per-entity vector + profile_text + content_hash, NOT the
typed source columns. The primary source has those columns; the
evaluator joins back via `IN`-list.

## Mutual exclusion (v1)

`similar_to` and `semantic_match` are mutually exclusive in v1 — a spec
can have AT MOST ONE vector primitive. Composing both is undefined.
Future work: support hybrid (find things similar to my seeds AND that
match this description) via either intersection or weighted-centroid.

## Lance source size guard

Lance sources at >100K rows refuse a full-scan materialization. Any spec
whose primary source is `fmcsa.carrier_essentials_lance` (~4.4M rows)
MUST have a vector primitive — there's no other way to bound the read.
Specs against the Iceberg view of the same data (`fmcsa.company_census_file`)
don't have this constraint because DuckDB pushes scalar filters down.

The error class is `LargeLanceScanRefused`. The router maps it to HTTP
413 (Payload Too Large) — the spec is structurally fine, but executing
it would exhaust process memory.

## X-Data-Lineage header

Phase 0b's middleware stamps `X-Data-Lineage` on every response. Vector
queries add an entry for the embeddings dataset alongside the
primary-source entry:

```json
[
  {"table": "fmcsa.carrier_essentials_embeddings_lance",
   "snapshot_id": "10",  // Lance dataset version
   "format": "lance",
   "queried_at": "2026-05-12T07:00:00Z"},
  {"table": "fmcsa.carrier_essentials_lance",
   "snapshot_id": "10",
   "format": "lance",
   "queried_at": "2026-05-12T07:00:01Z"}
]
```

The `snapshot_id` for Lance is the dataset version (monotonic int) that
the read targeted. For cross-source lineage (the spec joins source +
embeddings), both entries appear. The evaluator records both reads.

## Costs at query time

`similar_to` is free — it only reads existing embeddings.

`semantic_match` against OpenAI-embedded datasets costs ~$0.0001 per
query (one embedding call). Against sentence-transformers datasets:
free (in-process embed).

A signed cohort is materialized once (at sign-time), so the cost is
incurred once per signing.

## Smoke test

`apps/hq-x/scripts/smoke_audience_specs_vector.py` exercises the full
lifecycle end-to-end against PROD data. Run:

```bash
doppler --project hq-all --config prd run -- \
    uv run python -m scripts.smoke_audience_specs_vector
```

Verifies the verification gates from the Phase 4 cycle directive:
- `similar_to`: >=10 matches above threshold 0.7.
- `semantic_match`: >=10 matches above threshold 0.5.
- X-Data-Lineage includes the embeddings dataset.
- Sanity: hazmat-themed query returns carriers actually flagged hazmat.

## Future work

- Per-source weighted vector primitives (similar_to with `seed_weights`).
- Vector + scalar pre-filter (push scalar filters into the Lance scan
  before the ANN, not after — saves DuckDB hop).
- Cross-source vector matching (e.g. find SAM.gov opportunities
  semantically similar to a partner's past wins in USAspending) — needs
  cross-dataset embedding model alignment.
- Cohort manifest with vector-search metadata (which query, which
  centroid, what threshold) embedded in the signing row for
  reproducibility.
