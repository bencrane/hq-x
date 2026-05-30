# Recipe: leaf_swap

For leaf MVs (deps_count = 0) that need a definition change, not just an index. RENAME-based atomic swap. Two variants: deterministic and time-aligned.

## Pre-flight (assumed already done by validator)

- Strategy is `leaf_deterministic` or `leaf_time_dependent`.
- `index_only` was tried and could not deliver ≥30% improvement.
- Candidate has `has_unique_idx = true` (CONCURRENTLY-refreshable).
- Self-time fraction ≥ 30%.
- Canonical query identified, parameter values for `PREPARE/EXECUTE` selected.

## Why RENAME-swap is safe here

This recipe ONLY runs when `deps_count = 0`. PG's OID-based dependency tracking through `pg_rewrite` is what makes RENAME unsafe for non-leaf MVs (verified 2026-05-02; see `inventory/mv-optimization.md` §Structural facts §1). With zero dependents, there's nothing to orphan.

If you find yourself running this recipe on a MV with deps_count > 0, STOP. The validator's classification was wrong; re-run `01_classify_candidate.sql` and use the strategy that comes back.

---

## Variant A: deterministic

Use when `time_dependent = false`.

### Steps

1. **Capture baseline.** 5× canonical via `PREPARE/EXECUTE`. Record `baseline_p50`.
2. **Capture original OID.** `SELECT oid FROM pg_class WHERE relname = '{mv}' AND relnamespace = '{schema}'::regnamespace AND relkind = 'm';` — store as `orig_oid`.
3. **Build shadow.**
   ```sql
   CREATE MATERIALIZED VIEW __autoresearch__.{mv}_v2 AS
     {optimized_definition_with_same_columns_and_types};
   CREATE UNIQUE INDEX {mv}_v2_pkey ON __autoresearch__.{mv}_v2 ({unique_columns});
   ```
4. **Refresh both.**
   ```sql
   REFRESH MATERIALIZED VIEW CONCURRENTLY {schema}.{mv};
   REFRESH MATERIALIZED VIEW CONCURRENTLY __autoresearch__.{mv}_v2;
   ```
5. **Equality gate.**
   ```sql
   SELECT count(*), sum(hashtext(t::text)) FROM {schema}.{mv} t;
   SELECT count(*), sum(hashtext(t::text)) FROM __autoresearch__.{mv}_v2 t;
   -- Both tuples must be identical.
   ```
6. **Latency gate.** 5× canonical against shadow. Median ≤ 0.7 × baseline.
7. **OID-stability check.** Re-read original OID; must equal `orig_oid` (verifies no other process recreated the MV).
8. **Atomic swap.**
   ```sql
   BEGIN;
   ALTER MATERIALIZED VIEW {schema}.{mv} RENAME TO {mv}_old_{ts};
   ALTER MATERIALIZED VIEW __autoresearch__.{mv}_v2 SET SCHEMA {schema};
   ALTER MATERIALIZED VIEW {schema}.{mv}_v2 RENAME TO {mv};
   COMMIT;
   ```
9. **Drop old.** `DROP MATERIALIZED VIEW {schema}.{mv}_old_{ts};`
10. **Migration.** Captures the new MV definition for replay; idempotent via `CREATE MATERIALIZED VIEW IF NOT EXISTS` or matview-existence guard.

### Equality gate (deterministic)

`count(*) + sum(hashtext(t::text))`. Order-independent, linear-time, ~20s on 2.5 GB.

---

## Variant B: time-aligned

Use when `time_dependent = true`. Same RENAME-swap mechanics, but the equality gate is structural (not byte-identical) because `CURRENT_DATE` evaluation between the original's last refresh and the shadow's first refresh produces different content.

### Steps (deltas from Variant A)

After step 4, **realign refreshes**:

   ```sql
   -- Force same-wall-clock evaluation of CURRENT_DATE/NOW():
   REFRESH MATERIALIZED VIEW CONCURRENTLY {schema}.{mv};       -- first
   -- (no sleep; back-to-back)
   REFRESH MATERIALIZED VIEW CONCURRENTLY __autoresearch__.{mv}_v2;  -- second
   ```

This narrows the time-skew window to the duration of the first refresh + the start of the second. Any remaining drift comes from underlying-table writes during that window — usually negligible for the targeted MVs but documented as a known limitation.

Replace the **equality gate** in step 5 with a structural check:

```sql
-- Row counts identical:
SELECT count(*) FROM {schema}.{mv};
SELECT count(*) FROM __autoresearch__.{mv}_v2;

-- Per-column null fraction within ε=0.5%:
WITH orig AS (
  SELECT
    avg(({col1} IS NULL)::int) AS null_frac_{col1},
    avg(({col2} IS NULL)::int) AS null_frac_{col2}
    -- ... per column the validator chose to track
  FROM {schema}.{mv}
),
shadow AS (
  SELECT
    avg(({col1} IS NULL)::int) AS null_frac_{col1},
    avg(({col2} IS NULL)::int) AS null_frac_{col2}
  FROM __autoresearch__.{mv}_v2
)
SELECT
  abs(orig.null_frac_{col1} - shadow.null_frac_{col1}) AS delta_{col1},
  abs(orig.null_frac_{col2} - shadow.null_frac_{col2}) AS delta_{col2}
FROM orig, shadow;
-- All deltas must be ≤ 0.005.

-- Per-column distinct count within 1%:
SELECT count(DISTINCT {col1}), count(DISTINCT {col2}) FROM {schema}.{mv};
SELECT count(DISTINCT {col1}), count(DISTINCT {col2}) FROM __autoresearch__.{mv}_v2;
-- abs(orig - shadow) / orig ≤ 0.01 per column.
```

### Equality gate (time-aligned)

`count(*) + per-column null/distinct fractions within ε`. Strictly weaker than `hashtext_sum` but tolerant of `CURRENT_DATE` drift across the refresh window.

If the structural gate fails (e.g., null fraction diverges by > 0.5%), the rewrite is changing semantics, not just performance. ROLLBACK and skip; this is a real failure, not a strategy mismatch.

---

## Migration template (both variants)

```sql
-- supabase/migrations/{YYYYMMDDHHMMSS}_optimize_{mv_name}_swap.sql

DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_matviews
    WHERE schemaname = '{schema}' AND matviewname = '{mv_name}'
  ) THEN
    -- Replay: drop and recreate from the optimized definition below.
    -- Idempotent: re-running the migration produces the same end state.
    EXECUTE 'DROP MATERIALIZED VIEW {schema}.{mv_name}';
  END IF;
END$$;

CREATE MATERIALIZED VIEW {schema}.{mv_name} AS
  {optimized_definition};

CREATE UNIQUE INDEX {mv_name}_pkey ON {schema}.{mv_name} ({unique_columns});

-- Rebuild any non-unique indexes that existed on the original (capture from \d in pre-flight).
-- CREATE INDEX {idx_name_1} ON {schema}.{mv_name} ({columns});

REFRESH MATERIALIZED VIEW {schema}.{mv_name};  -- non-CONCURRENTLY on first build (no prior content)
```

## Rollback

If the swap was committed but issues are discovered post-merge:

1. The `_old_{ts}` MV was dropped at step 9; rollback requires rebuilding from the previous migration.
2. `git revert` the migration commit, deploy, refresh.
3. Indexes follow the migration; the previous migration's indexes return automatically.

## When this recipe fails

- Equality gate fails (deterministic): the rewrite is not equivalent. Rewrite the SQL or skip.
- Equality gate fails (time-aligned): possibly underlying tables are receiving writes during the refresh window, OR the rewrite changes semantics. Try `02_stability_check.sh` to confirm; if MV is genuinely time-stable, the rewrite is wrong.
- Latency gate fails: the rewrite isn't 30% faster. Skip; future iteration.
- OID-stability check fails: another process recreated the MV mid-flight. Drop the shadow, retry from step 1.
