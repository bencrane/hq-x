# GTM Signals → hq-x: migration + generalization plan (v0, to be assessed)

## Objective
Move the GTM signal **definition + lifecycle** from data-engine-x into hq-x. Keep warehouse **compute** near the data (do not pull DuckDB/Lance/R2 into hq-x). Generalize criteria beyond USAspending FPDS to any Lance dataset. Output a reusable **cohort**.

## Terminology (locked)
- **slice → cohort.** A signal's criteria *slices* the warehouse → produces a **cohort** (named, reusable set of resolved entities).
- A cohort is **NOT** an "audience." It only becomes an audience when bound into a GTM sequence/initiative. Cohort→audience binding is **out of scope** here.

## Current state (the tangle)
- `ops.gtm_signals` (DEX Postgres). Row: `signal_slug PK, spine_target, criteria jsonb, webhook_target, webhook_prod_url, webhook_test_url, is_active, ts`.
- `apps/data-engine-x/app/services/gtm_signal_cohort.py` does TWO jobs in one file:
  1. criteria → parameterized SQL (pure logic), and
  2. DuckDB/Lance/R2 **execution** (data compute).
  Hardcoded to FPDS: fixed URIs (`usaspending/transaction_fpds_lance`, `spines/sam_entities_lance`), fixed FPDS columns, fixed FPDS↔SAM join on UEI, 4 criteria keys (`time_window_hours`, `min_obligated_usd`, `award_types`, `action_types`).
- Dispatch: Modal cron `apps/data-engine-x/modal/gtm_usaspending_trigger_app.py` (09:00 UTC) + `fire_one_signal`; cohort POSTed to n8n webhook.
- hq-x today only PROXIES: `apps/hq-x/app/routers/gtm_signals_v1.py` + `app/services/dex_client.py::list_gtm_signals`.

## The cut (target architecture)
Separate **"what is a signal" (GTM logic → hq-x)** from **"run a query over the warehouse" (data compute → near the data)**.

### A. Moves INTO hq-x
- **Table** → `business.gtm_signals` (hq-x Supabase `imfwppinnfbptqdyraod`). Config-only, no joins to DEX data → clean move.
- **Criteria compiler** (spec → parameterized SQL string) — pure Python, **no DuckDB**. Generalized (below).
- Registry/CRUD, scheduling via **hq-x Trigger.dev** (replaces Modal cron), **cohort persistence**, agent-authoring entry points.

### B. STAYS near the data (DEX / gtm-mcp) — hq-x must not gain DuckDB/lance/boto3/R2
SQL execution over Lance/R2 via two paths:
- **Authoring + preview (≤100 rows):** `gtm-mcp` `execute_read_only_duckdb_query` (live at `gtm-mcp.up.railway.app/mcp`). gtm-agent authors/validates criteria vs live schema via `gtm.get_polaris_schema`.
- **Bulk cohort materialization (full set):** thin DEX endpoint `POST /internal/signals/compute` wrapping existing `fetch_cohort_rows`. Called from hq-x via existing `dex_client`. **[RECOMMENDED]** over raising the gtm-mcp 100-row cap.

### C. Generalized criteria spec (replaces the 4 FPDS keys)
```
{
  "spine_target": "<namespace.dataset_lance>",
  "predicates": [ {"column": str, "op": "eq|in|gte|lte|between|is_null|not_null|like", "value": ...} ],
  "time_window": {"column": str, "hours": int} | null,
  "join": {"dataset": str, "on": [left,right], "select": [str]} | null,
  "select": [str] | null,
  "order_by": {"column": str, "dir": "desc|asc"} | null
}
```
Columns validated against `gtm.get_polaris_schema(spine_target)`.

### D. Output = cohort
- Persist matched set as a **cohort** in hq-x (`business.gtm_signal_cohorts` or similar): `signal_slug, run_at, criteria_snapshot jsonb, matched_count, member rows or member ref`. Reusable, re-runnable.
- Webhook = one optional sink. Cohort→sequence (audience) is downstream/out of scope.

## Retire
- hq-x→DEX signal proxy (`gtm_signals_v1` proxy + `dex_client.list_gtm_signals`).
- Modal USAspending cron (`gtm_usaspending_trigger_app.py`).
- FPDS-hardcoded compiler in DEX → replaced by generic compiler (hq-x) + generic executor endpoint (DEX).

## Constraints / invariants
- hq-x = platform spine; NO data stack (DuckDB/lance/pyarrow/boto3/R2 creds).
- No cross-DB joins (hq-x Supabase vs DEX). `gtm_signals` is config-only.
- hq-x scheduling = Trigger.dev (existing tasks under `apps/hq-x/src/trigger/`). hq-x↔DEX = `dex_client` (existing). hq-x↔gtm-mcp = managed-agent session OR direct HTTP w/ `GTM_MCP_AUTH_TOKEN`.
- gtm-mcp read tool caps at 100 rows → bulk needs DEX `/compute`.

## Open questions (assessor to decide)
1. Cohort persistence grain: inline member rows (table/jsonb) vs re-runnable spec + on-demand recompute vs R2 snapshot. Size/scale.
2. DEX `/compute` contract: accept compiled SQL string, or accept criteria+spine_target and compile DEX-side? (Lean: hq-x compiles → sends SQL; DEX stays generic executor. Must be injection-safe / parameterized over HTTP.)
3. Column validation at compile-time vs execute-time.
4. Backfill of existing `ops.gtm_signals` rows (2 seeds + any live) into `business.gtm_signals`.
5. Auth for the new DEX `/compute` endpoint (service token); rate/size limits.
6. Cutover sequencing so dispatch is never dark (dual-run vs hard switch).
