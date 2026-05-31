# `scripts/dex/_archived_gen2/` — frozen Gen-2 extraction scripts (read-only reference)

**Frozen 2026-05-31. Execution suspended pending Gen-3 rebuilds.**

This directory holds the **391 Gen-2 DEX scripts** moved verbatim out of
`scripts/dex/` root when the Gen-2 data infrastructure was officially frozen:

| Pattern | Count | Role |
|---|---|---|
| `run_*.py` | 230 | per-source extraction (`*_to_r2` / `*_r2_ingest`) + DuckDB→Lance compute (`*_lance_emit`) |
| `build_bridge_*.py` | 120 | cross-source identity bridges (Pattern B) |
| `emit_*.py` | 41 | derived Lance emits |

They are preserved **as reference material only** — the API URLs, column
mappings, and extraction/transform logic the managed-agent fleet reads when
rebuilding each feed on the Gen-3 Universal Dispatcher substrate.

## Rules

- **Do not run, import from, or wire anything under this path.** Frozen.
  Relative `from _lib import …` references resolve to `scripts/dex/_lib/`
  (which stayed put); nothing here is meant to execute.
- `scripts/dex/_lib/` and `scripts/dex/dev-tools/` were intentionally left in
  place and remain live.
- Other Gen-2-adjacent dex scripts outside the three frozen patterns above
  (e.g. `build_cohort_*`, `build_*_spine_*`, `augment_*`, `init_*`,
  `verify_*`, `seed_*`) were **not** part of this freeze and remain at
  `scripts/dex/` root.
