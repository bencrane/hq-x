# Recipe: subtree_drop_recreate

For `has_deps_deterministic` MVs that need a definition change. Drop+recreate the entire dependency subtree atomically inside a single transaction. Last resort — heavy because every MV in the subtree has to be refreshed.

## When

- Strategy is `has_deps_deterministic`.
- `index_only` was tried and could not deliver ≥30% improvement.
- The improvement requires changing the MV definition (not just adding an index).

## Why this works where RENAME-swap doesn't

PG dependent-MV references are OID-bound (see `inventory/mv-optimization.md` §Structural facts §1). RENAME-swap orphans dependents to the renamed `_old` object because their `pg_rewrite` entries still reference the OLD oid.

Drop+recreate inside one transaction works because:
1. Inside a single `BEGIN/COMMIT`, `DROP MATERIALIZED VIEW {dep}` removes all dependents in reverse-dependency order.
2. `DROP MATERIALIZED VIEW {target}` removes the original.
3. `CREATE MATERIALIZED VIEW {target} AS ...` creates a fresh OID.
4. `CREATE MATERIALIZED VIEW {dep} AS ...` for each dependent uses its original definition; PG resolves the reference to `{target}` against the freshly-created OID. No orphan.
5. `COMMIT` makes all of it visible atomically.

## Pre-flight

The validator must produce a **subtree manifest** for this candidate before the recipe runs:

```
target_mv: {schema}.{mv}
subtree (dependents in dep-order, deepest first):
  - {dep_name_1}: full CREATE MATERIALIZED VIEW statement, indexes, original size
  - {dep_name_2}: ...
  - ...
total_subtree_size_mb: {sum}
estimated_refresh_minutes: {sum / 5MB/s × 2}  -- ×2 for original + each dep
```

The executor uses this manifest to assemble the transaction and to estimate cost.

**Hard cost gate:** if estimated_refresh_minutes > (remaining_wall_budget × 0.5), skip with `subtree-too-expensive`. Subtree rebuild is heavy; it should never crowd out other candidates.

## Steps

1. **Capture baseline.** 5× canonical against the target. Record `baseline_p50`.

2. **Capture all dependent definitions.**
   ```sql
   -- For each dependent in the manifest:
   SELECT pg_get_viewdef('{dep_schema}.{dep_name}'::regclass, true) AS def;
   -- And its indexes:
   SELECT indexdef FROM pg_indexes WHERE schemaname = '{dep_schema}' AND tablename = '{dep_name}';
   -- Cache for the recreate step.
   ```

3. **Build the OPTIMIZED target in `__autoresearch__` and validate.**
   This stage proves the rewrite is equivalent BEFORE touching anything in `{schema}`.
   ```sql
   CREATE MATERIALIZED VIEW __autoresearch__.{mv}_v2 AS
     {optimized_definition};
   CREATE UNIQUE INDEX {mv}_v2_pkey ON __autoresearch__.{mv}_v2 ({unique_columns});
   REFRESH MATERIALIZED VIEW CONCURRENTLY __autoresearch__.{mv}_v2;
   ```
   Run equality gate against `{schema}.{mv}`:
   ```sql
   SELECT count(*), sum(hashtext(t::text)) FROM {schema}.{mv} t;
   SELECT count(*), sum(hashtext(t::text)) FROM __autoresearch__.{mv}_v2 t;
   -- Both tuples must be identical.
   ```
   Run latency gate (5× canonical pointed at `__autoresearch__.{mv}_v2`). Must be ≥30% faster.

   **If either gate fails, drop the shadow and abort. Subtree rebuild is too expensive to attempt without strong evidence the rewrite is equivalent and faster.**

4. **Write the swap migration.**

   The migration is one transaction with this shape:
   ```sql
   BEGIN;
   -- Drop dependents in reverse-dep order (deepest first):
   DROP MATERIALIZED VIEW {schema}.{dep_name_N};
   DROP MATERIALIZED VIEW {schema}.{dep_name_N-1};
   -- ...
   DROP MATERIALIZED VIEW {schema}.{dep_name_1};
   -- Drop the target:
   DROP MATERIALIZED VIEW {schema}.{mv};

   -- Recreate the target with the optimized def:
   CREATE MATERIALIZED VIEW {schema}.{mv} AS
     {optimized_definition};
   CREATE UNIQUE INDEX {mv}_pkey ON {schema}.{mv} ({unique_columns});
   -- Recreate any non-unique indexes that existed on the original.

   -- Recreate dependents (forward-dep order, shallowest first) using captured defs:
   CREATE MATERIALIZED VIEW {schema}.{dep_name_1} AS
     {captured_def_1};
   CREATE UNIQUE INDEX {dep_name_1}_pkey ON {schema}.{dep_name_1} ({unique_columns});
   -- ...

   COMMIT;
   ```

5. **Refresh sequence (outside the txn).**
   `REFRESH MATERIALIZED VIEW CONCURRENTLY` cannot run inside a transaction with `CREATE`. Refresh after commit:
   ```sql
   REFRESH MATERIALIZED VIEW {schema}.{mv};               -- non-CONCURRENTLY on first refresh of fresh MV
   REFRESH MATERIALIZED VIEW {schema}.{dep_name_1};       -- in dep order
   -- ...
   ```
   For large subtrees, refresh in dep order (shallowest first) so each dep's refresh sees its parents already populated.

6. **Post-rebuild equality gates (per MV).**
   For each MV in the subtree, compute hashtext_sum before and after. Compare.

   ```sql
   -- Captured before the swap migration:
   -- {schema}.{dep_name_1}: count=..., hashtext_sum=...
   -- (validator must do this in step 2 alongside capturing defs)

   -- After the swap and refresh:
   SELECT count(*), sum(hashtext(t::text)) FROM {schema}.{dep_name_1} t;
   -- Must equal the captured before value.
   ```

   If any subtree gate fails: the migration cannot be reverted (drops are committed). Forward-only fix: investigate the divergence, ship a corrective migration, alert.

7. **Drop the validation shadow.** `DROP MATERIALIZED VIEW __autoresearch__.{mv}_v2;`

## Equality gate

`count(*) + sum(hashtext(t::text))` for the target AND every MV in the subtree. The validation in step 3 is the prerequisite; the per-subtree-MV check in step 6 is the audit.

## Latency gate

≥30% on the target's canonical (measured in step 3 against the shadow).

Subtree dependents may show secondary improvements OR slight regressions; this recipe does not gate on dependent latency. Document any dependent-side slowdown in the report.

## Migration template

```sql
-- supabase/migrations/{YYYYMMDDHHMMSS}_optimize_{mv_name}_subtree.sql
-- Subtree drop+recreate. Replays cleanly via CREATE MATERIALIZED VIEW IF NOT EXISTS guards.

BEGIN;

-- ... drops in reverse-dep order ...
-- ... creates in forward-dep order ...

COMMIT;

-- Refreshes (non-transactional):
REFRESH MATERIALIZED VIEW {schema}.{mv};
-- ... per dependent in dep order ...
```

For idempotency, wrap each `DROP/CREATE` block in a `DO $$ ... $$;` block with `pg_matviews` guards. The migration must be safe to re-run.

## Rollback

The transaction is atomic at swap time, but post-commit there is no automatic rollback (drops are real). Forward-only fix: write a corrective migration that recreates the prior state (using the captured defs from step 2 plus the prior optimized-target def).

For this reason, **never run subtree_drop_recreate without first having validated the optimized target against the original via the shadow build in step 3.** The shadow build is the only safety net.

## When this recipe fails

- Step 3 equality fails: rewrite is not equivalent. Skip; do not proceed.
- Step 3 latency fails: rewrite isn't 30% faster. Skip.
- Step 6 audit fails (post-commit): incident. Investigate, write a corrective migration, alert. The harness should never reach this state if step 3 was honored.
- Cost gate fails (subtree too large): record `subtree-too-expensive` and skip. Consider deferring to a follow-up batch with an extended wall budget.

## Notes

- This recipe has not yet been used in production. The 2026-05-02 batch hit 4 candidates that would route here, all of which were skipped because the directive only knew RENAME-swap. Treat the first production use as a higher-risk run; consider a longer wall budget and/or a quiet maintenance window.
- For very deep subtrees (depth > 3 or total > 10 GB), consider whether the target is the right candidate at all. Often the bottleneck pattern (missing index, COUNT(*) OVER(), `($1 IS NULL OR col = $1)`) can be fixed at the consumer side rather than via MV def change.
