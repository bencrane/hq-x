# MV Optimization Harness v2 — MCP-native

Replaces the shell+psql+regex v1 harness with a pure MCP-native implementation. No shell scripts, no doppler invocations, no awk/sed/perl. All DB access goes through `mcp__d9b07b25-21bc-4122-ad98-eb8d43cbb8e4__execute_sql` and `apply_migration`.

## Entry point

From a new session: `/mv-optimize` (after the v1→v2 swap in `/Users/benjamincrane/.claude/commands/mv-optimize.md`).

The command spawns two subagents defined at `~/.claude/agents/`:
- `mv-optimization-validator` — selects candidates, classifies, runs anti-pattern detection, emits verdicts.
- `mv-optimization-executor` — receives `ship` verdicts, builds indexes, verifies, opens PRs.

## Directory layout

```
mv-optimization-v2/
  README.md                          ← this file
  lib/
    candidate_select.sql             ← Stage A: top-N MV selection (pg_stat_statements)
    classify.sql                     ← Stage C+D: deps + time-volatility + strategy assignment
    existing_index_check.sql         ← Guard: existing indexes on a candidate MV
    anti_pattern_catalog.md          ← 4 seeded patterns with detection SQL + remediation templates
  recipes/
    index_only.md                    ← Auto-applicable recipe: detect → propose → apply → verify → PR
    leaf_swap.md                     ← Surface-to-human stub (MV definition rewrite)
    subtree_drop_recreate.md         ← Surface-to-human stub (drop-recreate subtree)
```

## Invocation flow

```
/mv-optimize
  ↓
mv-optimization-validator (opus)
  1. execute_sql: candidate_select.sql → candidate list
  2. For each candidate: execute_sql classify.sql → {deps_count, time_dependent, strategy_key}
  3. For each candidate: execute_sql existing_index_check.sql → existing index list
  4. For each candidate: run anti-pattern detection from anti_pattern_catalog.md
  5. Emit verdict JSON per candidate:
       {candidate, verdict: ship|skip|surface-to-human, reason, pattern_hit, proposed_index}
  ↓
mv-optimization-executor (sonnet) — receives `ship` verdicts only
  1. execute_sql: EXPLAIN baseline (pre-index)
  2. execute_sql: CREATE INDEX CONCURRENTLY IF NOT EXISTS ...
  3. apply_migration: same DDL without CONCURRENTLY (records migration row)
  4. execute_sql: EXPLAIN post-index — gate: ≥30% improvement + Index Scan node
  5. get_advisors: performance sweep (hygiene, non-blocking)
  6. git branch + migration file + gh pr create
  7. If gate passed: gh pr merge --merge --auto --delete-branch
     If gate failed: leave open with status comment
```

## Key design decisions

### No PREPARE/EXECUTE round-trips

v1 pulled canonical queries from `pg_stat_statements` and tried to re-run them via `PREPARE qry AS <canonical>; EXECUTE qry(<params>)`. This failed when the canonical contained typed literals (`interval $N`, `numeric $N`) that PG can't infer parameter types for. v2 detects patterns structurally from the view definition, not by re-executing canonicals.

### execute_sql for CONCURRENTLY, apply_migration for migration row

`apply_migration` wraps SQL in a transaction. `CREATE INDEX CONCURRENTLY` is incompatible with transactions (PG error). Solution: run the actual DDL via `execute_sql` (autocommit), then call `apply_migration` with the same DDL shape but using `IF NOT EXISTS` without `CONCURRENTLY` to record the migration metadata row. This mirrors v1's split between prod-side application and migration file commit.

### EXPLAIN gate, not advisor gate

`get_advisors` has no "missing-index-on-MV-column" advisor type. Gate is: EXPLAIN on the canonical with a realistic LIMIT shows (1) Index Scan node naming the new index, and (2) Execution Time dropped ≥ 30%.

### No branch staging for index_only

Supabase branches replicate schema, not MV data freshness. `CREATE INDEX CONCURRENTLY` is non-blocking and fully reversible via `DROP INDEX CONCURRENTLY`. Direct-to-prod is simpler and safer for this recipe.

## Constraints

These are inviolable (from the directive):
- `CREATE INDEX CONCURRENTLY` for every index DDL, no exceptions.
- Migration filenames: `YYYYMMDDHHMMSS_*` (timestamp-prefixed, per `project_migration_filename_convention` memory).
- No leaf_swap or subtree_drop_recreate auto-apply — surface to human only.
- No new anti-pattern catalog entries beyond the 4 seeded ones without a follow-up directive.
- Max 50 Supabase MCP calls per candidate (catches runaway loops).

## v1 archive

v1 lives at `scripts/mv-optimization/v1-legacy/` after the post-validation swap.
