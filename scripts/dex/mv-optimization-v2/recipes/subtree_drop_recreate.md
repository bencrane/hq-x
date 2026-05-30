# Recipe: subtree_drop_recreate (v2 stub — surface to human)

This recipe drops and recreates an entire MV subtree (the candidate + all its dependents). It is NOT auto-applicable in v2.

## When

- Candidate has `strategy_key = has_deps_deterministic`.
- `index_only` recipe did not clear the gate.
- The bottleneck is in the MV definition, not just a missing index.

## What subtree_drop_recreate involves

1. Identify all dependent MVs via `classify.sql` (the `dependent_mvs` JSON column, ordered by depth DESC = drop order).
2. Drop each dependent MV in order (highest depth first).
3. Drop the candidate MV.
4. Recreate the candidate MV with the improved definition.
5. Recreate each dependent MV in reverse order (lowest depth first).

This is a blocking operation on all downstream consumers of the MV. Must be done in a maintenance window.

## v2 behavior: surface to human

The executor emits a report entry:

```
candidate: {schema}.{mv}
verdict: surface-to-human
recipe: subtree_drop_recreate
reason: {deps_count} dependent MVs; subtree recreate required; cannot auto-apply safely
dependents: {list from classify.sql}
suggested_action: File a follow-up directive with maintenance window + full drop-recreate script.
```

No PR is opened. No DDL is applied.
