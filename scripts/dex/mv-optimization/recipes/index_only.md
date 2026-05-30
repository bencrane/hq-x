# Recipe: index_only

Universal first-try recipe. No swap. Add an index to the existing MV and re-benchmark.

## When

- Always tried first, regardless of `strategy_key`.
- Required for `has_deps_time_dependent` (no fallback exists).
- Often sufficient when the canonical's bottleneck is a Seq Scan or a sort that an index can drive.

## Pre-flight (assumed already done by validator)

- Candidate has `has_unique_idx = true`.
- Self-time fraction ≥ 30% (otherwise even infinite improvement here can't clear the latency gate).
- Canonical query identified, parameter values for `PREPARE/EXECUTE` selected.

## Steps

1. **Capture baseline.** Run the canonical 5× via `PREPARE qry AS <canonical>; EXECUTE qry(<bound>);`. Record p50.
2. **Read the EXPLAIN.** Look for the operator that dominates execution time on the candidate MV. Common patterns:
   - `Parallel Seq Scan` on filter column → btree index on filter column.
   - Sort node followed by Limit → btree index on sort columns.
   - `Bitmap Heap Scan` with high recheck cost → covering index that matches.
   - Hash Join with seq scan on one side → btree index on the join column.
3. **Pick one index.** Avoid composite indexes unless the EXPLAIN clearly shows the planner needs both columns; single-column indexes are smaller and easier to maintain.
4. **Apply to a shadow first if uncertain.** For high-confidence indexes, apply directly to prod (the index addition is non-blocking with `CONCURRENTLY`). For lower confidence, build the index on a copy in `__autoresearch__` first, swap measure, then apply to prod.
5. **Re-benchmark.** 5× canonical. Median.
6. **Gate:** new_p50 ≤ 0.7 × old_p50 (≥30% improvement).
7. **Ship.** Migration is a single `CREATE INDEX IF NOT EXISTS` statement with timestamp-prefix filename.

## Migration template

```sql
-- supabase/migrations/{YYYYMMDDHHMMSS}_optimize_{mv_name}_index.sql
CREATE INDEX IF NOT EXISTS {idx_name}
  ON {schema}.{mv_name} USING btree ({columns});

-- Drive-by safety check: ensure the index can serve the canonical.
-- DO NOT include in migration; for executor verification only.
-- ANALYZE {schema}.{mv_name};
```

Naming: `idx_{mv_short_name}_{column_short}` truncated to ≤63 chars (PG identifier limit). Example: `idx_mv_fmcsa_ag_final_dec_date`.

## Equality gate

`count(*)` only. The MV definition is unchanged; row count must match (always does — index addition cannot change MV content).

```sql
-- Before:
SELECT count(*) FROM {schema}.{mv_name};
-- After CREATE INDEX:
SELECT count(*) FROM {schema}.{mv_name};
-- Must be identical.
```

## Latency gate

```bash
# Run 5× before, capture p50
doppler run -- bash -c '
  for i in 1 2 3 4 5; do
    psql "$DEX_DB_URL_DIRECT" -X <<EOF
      PREPARE qry AS {canonical};
      EXPLAIN (ANALYZE, TIMING ON) EXECUTE qry({bound_params});
      DEALLOCATE qry;
EOF
  done
' | awk "/Execution Time/ {print \$3}" | sort -n | awk "NR==3{print}"
# Then apply CREATE INDEX, repeat.
# new_p50 / old_p50 must be ≤ 0.7
```

## Rollback

`DROP INDEX IF EXISTS {schema}.{idx_name};`

The migration is forward-only by convention, but the rollback is one statement and safe to apply at any time.

## When this recipe fails

If no single index improves the canonical by ≥30%:

- For `leaf_*` strategies → escalate to `leaf_swap.md` (definition change required).
- For `has_deps_deterministic` → escalate to `subtree_drop_recreate.md`.
- For `has_deps_time_dependent` → record `skipped-no-viable-index` and stop. No recipe can help safely.

## Notes

- 2026-05-02 PR #156 used this recipe (after `leaf_swap` was blocked by deps). 94.1% reduction on `mv_fmcsa_authority_grants` canonical (576.5ms → 34.1ms) via a single btree on `final_authority_decision_date`.
- `CREATE INDEX CONCURRENTLY` is preferred for prod (non-blocking) but cannot run inside a transaction. Standalone migration only.
