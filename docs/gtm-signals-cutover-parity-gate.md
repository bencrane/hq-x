# GTM-signals cutover — parity gate, N1 latency SLA, schema-source decision

**Status:** gate live (RED against `main`, by design); blocks PR-4 cutover.
**Owner artifact:** [`apps/hq-x/scripts/gtm_signal_parity_check.py`](../scripts/gtm_signal_parity_check.py)
**Date:** 2026-05-29

This is the executable definition of done for moving GTM-signal dispatch from the
Modal cron (DEX `ops.gtm_signals` → `gtm_signal_cohort.fetch_cohort_rows` →
n8n) to hq-x (`business.gtm_signals` → compiler → DEX `/api/internal/signals/compute`
→ n8n). PR-4 is **not** done until this gate is GREEN.

## Why a gate, not just code review

The cutover's failure mode is *silent* divergence: the new cron ships a
plausible-looking 50,000-row payload to prod n8n that is the **wrong** cohort, with
no error. Measured against the live system on 2026-05-29 for `usaspending_net_new_100k`
(365-day window, 4.6M transactions scanned, 134,908 matched, capped to 50,000):

- **33.4% of the dispatched cohort (16,680 of 50,000 companies) differs** between the
  correct numeric ordering and the lexical ordering the new executor currently emits.
- The 16,680 dropped rows are **the highest-value contracts** — including the single
  largest federal award of the year (**$2.3B**), excluded while a $960M one is kept,
  purely because the text `"2…"` sorts below `"9…"`.
- `matched_count` is **identical** either way (134,908) — so a count-based smoke check
  passes while the membership is one-third wrong.

You cannot fix that by inspection. The legacy path is still live, so the gate diffs
the two paths against identical live data and turns "byte-for-byte parity" into a
measured, gating fact.

## What the gate asserts

Per active signal: legacy `POST /api/v1/gtm/signals/{slug}/preview` vs. new
`compile_criteria(...) → POST /api/internal/signals/compute`, both with a long client
timeout so wide-window compute can be measured past the production budget.

| Dimension | Defect | Green when |
|---|---|---|
| `matched_count` | D1 (dropped SAM join / scan_filter) | pre-cap counts equal |
| `keyset` | D3 (key names) | `uei`/`award_type` aliases restored (not `recipient_uei`/`type_description`) |
| `value_types` | D3 (value types) | `federal_action_obligation` DOUBLE, `action_date` DATE — not raw text |
| `ordering+membership` | D2 (lexical sort) | top-N sequence matches legacy (numeric DESC) |
| `latency<=budget` | N1 | new-leg wall-clock ≤ 30 s |

Gate output against `main` (2026-05-29, `usaspending_net_new_100k`):

```
  [PASS] matched_count        legacy=134908 new=134908
  [FAIL] keyset               only_legacy=['award_type', 'uei'] only_new=['recipient_uei', 'type_description']
  [FAIL] value_types          federal_action_obligation: legacy=number new=string
  [FAIL] ordering+membership  diverges at row 0/500
  [FAIL] latency<=budget      new=40.3s legacy=43.0s budget=30s server_compute_ms=39487
```

`matched_count` GREEN proves the boundary is sound and parity is achievable; each RED
is exactly one PR-4 work item.

### D1 is broader than "forward the compiled fields" — the scan must be column-bounded

Building the gate surfaced a defect even the adversarial review's D1 missed:
`execute_cohort` narrows the **spine column** scan only when `project_columns` is
provided (`lance_cohort_exec.py` — `if project_columns:` … else no `columns=` kwarg →
full-schema scan). `select` does **not** bound it, and the compiler emits `select` but
never `project_columns`. The first gate run (forwarding only the compiled fields)
scanned all ~100 FPDS columns for the 365-day window and **timed out at 240s**; passing
`project_columns` (the 11 spine cols) dropped it to ~40s. PR-4 must therefore either
(a) have callers pass `project_columns`, or (b) default the executor's scan projection
to `select ∪ order_by ∪ scan_filter ∪ join-key` when `project_columns` is absent.
Option (b) is preferred — it makes the safe path the default and removes a silent
full-scan footgun.

## N1 latency SLA (the blocker neither prior doc found)

The cron and the rewired `/run-agent` both route compute through
`dex_client.compute_signal_cohort` → `_request("POST", …)`, which hardcodes
`_DEFAULT_TIMEOUT = 30.0` and does **not** retry POST. The gate measured live
`usaspending_net_new_100k` compute at **~40 s** (server-side 39.5 s) — so the
production client times out, the per-signal `try/except` swallows it, and the day logs
green with **zero dispatch**. The same gate run clocked the legacy `/preview` at
**~43 s**, confirming the *current* `/run-agent` path is already over the 30 s budget
for this signal — N1 is not introduced by the cutover, it is unmasked by it.

**SLA:** new-path compute must complete within the hq-x→DEX client budget. The gate's
`latency<=budget` dimension enforces it. Acceptable fixes (PR-4):

1. **Async compute** (preferred) — mirror the fire path's spawn + poll
   (`fire_endpoint` → `fire/status/{call_id}`), so the heavy scan is decoupled from
   any single HTTP request. This is the design the team already adopted when it hit
   this exact 30 s wall on fire.
2. **Bounded synchronous** — give `compute_signal_cohort` an explicit generous timeout
   (≥240 s) AND confirm the Trigger→hq-x→Railway edge tolerates a multi-minute request.
   Weaker; fragile for the widest windows.

## Schema-source decision (D4) — cron must not depend on a live gtm-mcp fetch

The compiler **requires** `allowed_columns` (raises `CompileError` on any unknown
identifier). The plan had the cron fetch the schema from gtm-mcp per run and
"skip-and-log on failure." Because the dependency is **shared**, a gtm-mcp/R2 outage
at 09:00 UTC fails the fetch for *every* signal → every signal skipped → silent
zero-fire day. The legacy Modal cron has no such dependency (Postgres + R2 only), so
this would be a brand-new single point of failure that is strictly worse than today.

**Decision:** the cron compiles with a `validate_identifiers=False` mode (PR-4 adds it
to `compile_criteria` — keep the strict regex, skip only the `in allowed` membership
check) and trusts DEX execute-time re-validation. DEX already re-validates every
identifier against the freshly-opened Lance schema
(`lance_cohort_exec._validate`) and returns 422 on a genuine mismatch — that 422 is a
real per-signal skip (the column truly doesn't exist), not an outage artifact. Net:

- gtm-mcp is **removed** from the cron's hard path (not merely softened) → no shared
  SPOF, no silent zero-fire.
- A real schema drift fails **loudly per-signal** at execute time.
- Interactive authoring (operator UI `/preview`, `/run-agent`) **keeps** the live
  gtm-mcp schema fetch for autocomplete + early validation, where a human benefits —
  via the bundled `fastmcp`/`mcp` SDK client, parsing the `get_polaris_schema` text
  block and its `ERROR …`-at-HTTP-200 sentinel (D9). Hand-rolling the streamable-HTTP
  handshake is unnecessary.
- The Trigger task **throws** if *all* signals skip in a run, so a total-skip day pages
  instead of going green.

This gate opens Lance directly for `allowed_columns` — equivalent to what DEX
re-validates against — so it is agnostic to the cron's eventual schema source.

## Cutover ordering correction (verified live)

The plan's "prod `business.gtm_signals` is empty / DEV-only" premise is **stale**:
prod (`db.imfwppinnfbptqdyraod.supabase.co`) already holds both signals with the
generalized criteria. There is no empty-table 404 window. The real ordering gate is:

1. Land the parity + N1 fixes so this gate is GREEN.
2. **Re-backfill** prod `business.gtm_signals` — the existing rows were translated by
   the pre-fix backfill, so they lack the D2 `order_by` cast and the D3 `select`
   aliases. Re-run after the compiler/contract changes; re-run this gate.
3. Dual-run with both crons on the **test** sink (both signals are already
   `webhook_target='test'`), confirm parity, mute Modal by **`is_active=false` / `modal
   app stop`** (not `webhook_target`), then flip hq-x to `prod` as the sole dispatcher.
