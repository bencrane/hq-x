# MV Optimization Toolkit

Reusable harness for batch-optimizing slow materialized views. Replaces single-contract directives that fail on common cases (deps, time-dep) with per-candidate strategy routing.

**Master doc:** `~/Desktop/hq/inventory/mv-optimization.md` — read this first.

## Quick start

```bash
# Invoke from a doppler-configured project worktree (e.g. ~/data-engine-x)
cd ~/data-engine-x
bash apps/data-engine-x/scripts/mv-optimization/run.sh --limit 30

# Output: /tmp/mv-opt-manifest.md (Stage A + Stage C+D filled in;
# Stage B requires per-candidate canonical analysis from the validator)
```

## Files

| File | Stage | Purpose |
|---|---|---|
| `00_select_candidates.sql` | A | Top-N by pg_stat_statements with structural filters (unique idx, real SELECT canonical) |
| `01_classify_candidate.sql` | C+D | Per-candidate deps walk + time-dep regex → strategy_key + recipe_chain |
| `02_stability_check.sh` | E | Optional empirical stability check (refresh × 2, compare hashes) |
| `run.sh` | orchestrator | Chains A + C + D, emits markdown manifest |
| `directive-template.md` | — | Per-run directive skeleton; copy to `directives/{date}-mv-optimization-batch.md` |
| `recipes/index_only.md` | — | Universal first-try recipe (CREATE INDEX, no swap) |
| `recipes/leaf_swap.md` | — | Leaf-MV swap (deterministic + time-aligned variants) |
| `recipes/subtree_drop_recreate.md` | — | Has-deps def-change recipe (drop+recreate full subtree in one txn) |

## Strategy routing

Output of Stage C+D classifies each candidate into one cell of:

| deps | time-dep | strategy_key | recipe chain |
|---|---|---|---|
| 0 | no | `leaf_deterministic` | `index_only -> leaf_swap` |
| 0 | yes | `leaf_time_dependent` | `index_only -> leaf_swap (time-aligned)` |
| ≥1 | no | `has_deps_deterministic` | `index_only -> subtree_drop_recreate` |
| ≥1 | yes | `has_deps_time_dependent` | `index_only` (no fallback) |

**Always-true:** try `index_only` first regardless of cell. Only escalate when no index can deliver the latency gate.

## Smoke tests

The classification was verified against the 7 candidates from the 2026-05-02 batch:

| candidate | classification | matches prior-batch finding |
|---|---|---|
| mv_fmcsa_authority_grants | has_deps_deterministic (3 deps) | ✓ (3 deps confirmed) |
| mv_fmcsa_carrier_targeting | leaf_time_dependent (current_date) | ✓ (would have skipped equality gate) |
| mv_pdl_companies_normalized | has_deps_deterministic (7 deps) | ✓ (7 deps confirmed) |
| mv_sam_gov_entities_typed | has_deps_deterministic (7 deps) | ✓ (prior reported 5 — recursive walk now finds full set) |
| mv_fmcsa_latest_insurance_policies | has_deps_deterministic (2 deps) | ✓ |
| mv_usaspending_first_contracts | leaf_deterministic | (Stage B self-time check would still skip per prior batch) |
| mv_dealbridge_lender_naics_state | leaf_deterministic | (Stage B self-time check would still skip per prior batch) |

The 4 candidates that errored on RENAME-vs-deps in the prior batch all route to `index_only` first, which is the path that actually shipped the only win (PR #156 on mv_fmcsa_authority_grants).
