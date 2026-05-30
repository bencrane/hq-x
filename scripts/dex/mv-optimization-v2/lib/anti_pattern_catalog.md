# Anti-Pattern Catalog — MV Optimization v2

Four seeded patterns. Detection runs as Postgres-side queries via `execute_sql`. Remediation DDL is a template the executor fills and applies.

No new entries may be added without a follow-up directive (per out-of-scope constraint #9 of the MV optimization harness directive).

---

## Pattern 1: LTRIM-no-idx

**Name:** `ltrim_no_idx`

**What it is:** A view definition applies `LTRIM(col, '0')` in a WHERE or JOIN predicate, but no expression index on `(LTRIM(col, '0'))` exists. The planner cannot use any plain btree index on `col` for this expression, forcing a full scan with per-row function evaluation.

**Detection SQL** (run via `execute_sql` against the candidate's schema.mv):

```sql
WITH view_def AS (
  SELECT pg_get_viewdef(format('%I.%I', :schema, :mv)::regclass) AS def
),
has_ltrim_predicate AS (
  SELECT def ~* '\bltrim\s*\(' AS matches
  FROM view_def
),
has_ltrim_index AS (
  SELECT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = :schema AND tablename = :mv
      AND indexdef ~* 'ltrim\s*\('
  ) AS exists
)
SELECT
  matches AS ltrim_in_viewdef,
  NOT exists AS missing_expression_index,
  matches AND NOT exists AS pattern_hit
FROM has_ltrim_predicate, has_ltrim_index;
```

**Remediation DDL template:**

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_{mv_short}_{col_short}_ltrim
  ON {schema}.{mv} USING btree (LTRIM({col}, '0'));
```

**Verification:** After index, `EXPLAIN (ANALYZE, BUFFERS)` on a query with `WHERE LTRIM({col}, '0') = $1` must show `Index Scan` referencing the new index name. Execution Time must drop ≥ 30% vs. baseline.

---

## Pattern 2: NULL-OR-equals

**Name:** `null_or_equals`

**What it is:** A view or its downstream canonical uses `($N IS NULL OR col = $N)` — the classic "optional filter" pattern. The planner cannot use a plain btree index on `col` for this construct because the NULL branch forces a full scan. A partial index `WHERE col IS NOT NULL` lets the planner use it when `$N IS NOT NULL`, cutting ~50% of the work on typical distributions.

**Detection SQL:**

```sql
WITH canonical_text AS (
  -- Caller supplies the canonical query text from pg_stat_statements
  SELECT :canonical_query AS q
)
SELECT q ~* '\(\$\d+\s+IS\s+NULL\s+OR\s+\w+\s*=\s*\$\d+\)' AS pattern_hit
FROM canonical_text;
```

**Remediation DDL template:**

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_{mv_short}_{col_short}_not_null
  ON {schema}.{mv} USING btree ({col})
  WHERE {col} IS NOT NULL;
```

**Verification:** After index, EXPLAIN on a query binding `$N` to a non-NULL value must show Index Scan on the new index. Execution Time must drop ≥ 30%.

---

## Pattern 3: ORDER-BY-LIMIT-no-idx

**Name:** `order_by_limit_no_idx`

**What it is:** The canonical query has `ORDER BY col [ASC|DESC] LIMIT N` and no btree index on `col` exists. The planner must sort the full table (or use a top-N heapsort over a full scan), which scales with table size. A btree index on `col` lets the planner walk the index in order and stop after N rows — O(N) instead of O(rows).

**Detection SQL:**

```sql
WITH canonical_text AS (
  SELECT :canonical_query AS q
),
sort_col AS (
  SELECT (regexp_match(q, 'ORDER\s+BY\s+(\w+)', 'i'))[1] AS col
  FROM canonical_text
),
has_limit AS (
  SELECT q ~* '\bLIMIT\b' AS has_it
  FROM canonical_text
),
has_index AS (
  SELECT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = :schema AND tablename = :mv
      AND indexdef ~* ('btree \(' || sc.col || '\)')
  ) AS exists
  FROM sort_col sc
)
SELECT
  has_it AND sc.col IS NOT NULL AND NOT hi.exists AS pattern_hit,
  sc.col AS sort_column
FROM has_limit, sort_col sc, has_index hi;
```

**Remediation DDL template:**

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_{mv_short}_{sort_col}
  ON {schema}.{mv} USING btree ({sort_col});
```

**Verification:** After index, EXPLAIN on the canonical with a small LIMIT (≤ 200) must show `Index Scan` on the new index. For queries that also have `COUNT(*) OVER()` (window function reading all rows), the index only helps if the query can be restructured to separate the count from the sort — if not, note this explicitly and record the gate as "partial: index exists but planner cannot use for window+sort together."

**Note on COUNT(*) OVER() interaction:** When the canonical is `SELECT *, COUNT(*) OVER() FROM mv ORDER BY col LIMIT N`, the window function forces a full table scan (all rows must be counted). The planner cannot satisfy both the count and the index-ordered sort with a single index scan. The index still provides value if the UI can issue separate count and data queries, but the combined-query form will not see improvement below 30% on typical row counts > 500K.

---

## Pattern 4: COUNT-OVER-paged

**Name:** `count_over_paged`

**What it is:** `COUNT(*) OVER()` inside a paged query (`ORDER BY ... LIMIT N OFFSET M`) reads every row on every page turn. This is a SQL rewrite problem, not an index problem — no index can help a window function that must read all rows. Detection flags it for surfacing to human; no auto-remediation.

**Detection SQL:**

```sql
WITH canonical_text AS (
  SELECT :canonical_query AS q
)
SELECT
  q ~* 'COUNT\s*\(\s*\*\s*\)\s+OVER\s*\(\s*\)' AS has_count_over,
  q ~* '\bLIMIT\b' AS has_limit,
  q ~* 'COUNT\s*\(\s*\*\s*\)\s+OVER\s*\(\s*\)' AND q ~* '\bLIMIT\b' AS pattern_hit
FROM canonical_text;
```

**Remediation:** Manual SQL rewrite required. Split into two queries: `SELECT COUNT(*) FROM {mv} WHERE <filters>` and `SELECT * FROM {mv} WHERE <filters> ORDER BY col LIMIT N OFFSET M`. No auto-apply. Surface to human with both queries suggested.

---

## Detection order

**Pattern 4 short-circuits patterns 1–3.** Run pattern 4 *first*. If it matches, the candidate is routed to manual rewrite via `recipes/split_count.md`; do not propose any pattern 1–3 remediation. Reasoning: an index from pattern 1/2/3 is structurally correct but capped at the COUNT-OVER full-scan cost, so the EXPLAIN gate will reject it (verified 2026-05-02 on `mv_sam_gov_entities_typed`: pattern 3 matched on `legal_business_name`, index built, but EXPLAIN cost diff was 8.8% because COUNT(*) OVER() forces full index walk).

If pattern 4 does **not** match, run patterns 1 → 2 → 3 in order. A candidate may match multiple of 1–3; propose remediation for the first match.

```
detection(candidate):
  if pattern_4_hit(candidate):
    route → manual (split_count recipe)
    skip patterns 1, 2, 3
    return "manual_review_needed"
  for p in [1, 2, 3]:
    if pattern_p_hit(candidate):
      propose pattern_p remediation
      return "auto_apply_proposed"
  return "no_pattern_matched"
```

**Gate change to existing index_only recipe:** before any CREATE INDEX is built, the executor must re-check pattern 4 detection on the candidate's canonical. If pattern 4 hits, abort the index build and route to manual. This prevents the v1 → v2 race where pattern 3 was already queued and pattern 4 was discovered too late (the case that produced PR #157, closed 2026-05-02).
