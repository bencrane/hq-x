# Recipe: leaf_swap (v2 stub — surface to human)

This recipe requires a creative SQL rewrite of the MV definition. It is NOT auto-applicable in v2.

## When

- Candidate has `strategy_key = leaf_deterministic` or `leaf_time_dependent`.
- `index_only` recipe was tried and the EXPLAIN gate did not clear ≥ 30%.
- Or: the dominant bottleneck is the MV definition itself (e.g., an expensive subquery that can be rewritten).

## What leaf_swap involves

1. Rewrite the MV's `CREATE MATERIALIZED VIEW` definition to eliminate the bottleneck.
2. Create a new MV under a temporary name.
3. Swap via `RENAME`.
4. Drop the old MV.

## v2 behavior: surface to human

The executor emits a report entry:

```
candidate: {schema}.{mv}
verdict: surface-to-human
recipe: leaf_swap
reason: MV definition rewrite required; cannot auto-apply
suggested_action: File a follow-up directive with the proposed new MV definition for human review.
```

No PR is opened. No DDL is applied.
