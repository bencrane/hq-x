# Recipe: split_count (manual)

Manual SQL rewrite recipe for `count_over_paged` candidates (Pattern 4 in `lib/anti_pattern_catalog.md`).

**This recipe is not auto-applied.** The executor surfaces the candidate + the proposed rewrite to a human; the human reviews, runs the rewrite as a leaf_swap-style migration, and ships.

## When this recipe applies

The candidate's canonical query matches Pattern 4: `COUNT(*) OVER()` inside a paged query (`ORDER BY ... LIMIT N OFFSET M`). No index can help — the window function reads every matching row regardless of LIMIT.

Detection short-circuits Patterns 1–3 per the catalog's detection-order rule.

## What the rewrite looks like

The combined query that produced the bottleneck:

```sql
SELECT *, COUNT(*) OVER() AS total_matched
FROM {schema}.{mv}
WHERE {filters}
ORDER BY {sort_col} {ASC|DESC}
LIMIT $1 OFFSET $2
```

splits into two queries the consumer issues separately:

```sql
-- Query A: count, run once per filter set, optionally cached.
SELECT COUNT(*) AS total_matched
FROM {schema}.{mv}
WHERE {filters};

-- Query B: page, run per page turn. Index on {sort_col} now drives this end-to-end.
SELECT *
FROM {schema}.{mv}
WHERE {filters}
ORDER BY {sort_col} {ASC|DESC}
LIMIT $1 OFFSET $2;
```

The MV definition itself does **not** change. The rewrite happens in the consumer (the FastAPI route or the SQL builder that produces the canonical). An index on `{sort_col}` (Pattern 3 remediation) becomes effective once Query B is decoupled from the count.

## Pre-flight checks

Before recommending the rewrite, the executor surfaces:

1. The exact canonical text from `pg_stat_statements`.
2. The consumer location — where the canonical is built. Find via `grep -r "FROM {mv}" {project}/app` to locate the route or query builder.
3. The `{sort_col}` Pattern 3 would have proposed an index on. Even though Pattern 4 short-circuits the index, the human reviewer often wants both: rewrite the consumer AND ship the index, because Query B benefits from it.

## Per-candidate output (executor → human)

```
candidate: {schema}.{mv}
pattern: count_over_paged (Pattern 4)
canonical: <full SQL from pg_stat_statements>
sort_col_for_index_when_query_b_decoupled: {col}

proposed rewrite:
  Query A (count):
    SELECT COUNT(*) AS total_matched FROM {schema}.{mv} WHERE {filters};

  Query B (page):
    SELECT * FROM {schema}.{mv} WHERE {filters}
    ORDER BY {col} {dir} LIMIT $1 OFFSET $2;

consumer location (best guess): {file}:{line}

recommended actions for human:
  1. Rewrite consumer to issue Query A and Query B separately.
  2. Optionally cache Query A's result for repeat page turns with the same filter.
  3. Ship a Pattern-3 index on {col} once the rewrite is in flight (now effective).
  4. Verify post-deploy via pg_stat_statements: combined-canonical row should drop in mean_exec_time; two new rows appear for Query A and Query B.
```

## Why this is manual

The rewrite touches consumer code (FastAPI route, SQL builder, frontend pagination logic) — not just the MV definition or an index. That's a creative refactor, not a structural transformation. The harness surfaces; the human decides scope and ships as a normal feature PR (not an `autoresearch/optimize-*` branch).

## Verification post-merge

After the human ships the rewrite + the optional Pattern-3 index:

1. Wait for one full `pg_stat_statements_reset` cycle (or a few hours of traffic).
2. Confirm:
   - The original combined-canonical row's `mean_exec_time` dropped or the row aged out.
   - Two new rows for Query A and Query B exist; their combined `mean_exec_time × calls` is meaningfully lower than the original.
   - Supabase `get_advisors` no longer flags this MV under "missing index" or "high cost".

## Recorded use

- 2026-05-02: catalog rule added after `mv_sam_gov_entities_typed` shipped a Pattern-3 index (PR #157) that cleared structural check but failed the EXPLAIN cost gate at 8.8% because of pattern 4 interaction. PR closed; candidate queued for this recipe in a follow-up.
