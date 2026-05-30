# GTM Signals cutover (PR 4 + dual-run): plan to be adversarially assessed

Assess against the live code. Companion to `gtm-signals-to-hqx-plan-of-attack.md`
(§E router, §H run-agent, §J cron, §K cutover). Terminology: slice→cohort; a
cohort is NOT an audience until bound into a sequence (out of scope).

## State (as of main 9c7dda1f)
- SHIPPED: PR1 (`business.gtm_signals` + `gtm_signal_cohorts(_members)` + `gtm_signals.py` + `gtm_cohort_writer.py` + backfill; **applied/backfilled to DEV only**), PR2 (`gtm_signal_compiler.py`, pure, 20 tests), PR3 (DEX `/api/internal/signals/compute` + `lance_cohort_exec.py` + `dex_client.compute_signal_cohort`; verified live vs R2).
- NOT done: **prod `business.gtm_signals` is empty**; live hq-x `gtm_signals_v1.py` endpoints still DEX-proxy; the **Modal cron `gtm_usaspending_trigger_app` is still the authoritative dispatcher**; `gtm_mcp_client` (the compiler's schema source) **not built**.

## Plan

### (a) Prod readiness — DARK
1. Apply migration `20260529T193000_gtm_signals.sql` to PROD hq-x DB (`doppler --project hq-all --config prd` → `HQX_DB_URL_DIRECT` → `scripts/migrate`).
2. Run `scripts/backfill_gtm_signals_from_dex` against PROD (reads live DEX `ops.gtm_signals` over `dex_client` + `DEX_SERVICE_TOKEN`; upserts `business.gtm_signals`).
- Nothing reads `business.gtm_signals` in prod until (b) deploys → reversible (`TRUNCATE business.gtm_signals`).

### (b) PR 4 code — router rewire + cron (cron deployed PAUSED)
1. `gtm_mcp_client.py`: direct-HTTP MCP (streamable-HTTP, `Authorization: Bearer GTM_MCP_AUTH_TOKEN`) → `get_polaris_schema(ns,ds)->set[str]` (compiler `allowed_columns` source) + `execute_read_only(sql)` (preview ≤100). Add `GTM_MCP_AUTH_TOKEN` to hq-x config (value in hq-all/prd).
2. `gtm_signals_v1.py` → hq-x-native: `GET/POST/PATCH/DELETE /api/v1/signals` (CRUD via `gtm_signals` service); `POST /{slug}/fire` (compile → `compute_signal_cohort` → `write_cohort(source=manual)` → webhook POST if url set); `POST /{slug}/preview` (compile → `/compute` count+sample, no persist); `POST /{slug}/run-agent` REWIRE (`gtm_signals.get_signal` + compile(schema-gated) → `compute_signal_cohort` → build the **preview-shaped dict `_format_initial_user_message` expects** → `mint_session`); `GET /{slug}/cohorts` + `GET /cohorts/{id}`.
3. `app/routers/internal/gtm_signals.py` `POST /internal/signals/run-daily` (`verify_trigger_secret`): per active signal → fetch schema (gtm-mcp; per-signal try/except) → compile → `compute_signal_cohort(max_rows=50k)` → `write_cohort(source=cron, trigger_run_id)` → webhook POST (byte-for-byte the legacy Modal payload) → record `dispatch`. Per-signal isolation (one failure ≠ abort batch).
4. `gtm-signals-daily.ts` `schedules.task` cron `0 9 * * *`, deployed **PAUSED**.
- Cron schema dependency: on gtm-mcp schema-fetch failure, **skip-and-log that signal** (don't abort); DEX re-validates identifiers at execute time regardless.
- Reversible: revert PR; DEX-proxy router + Modal cron still live (untouched until PR 5).

### (c) Dual-run cutover — the ONLY live-n8n-dispatch step
1. Deploy (b) (cron paused). Manually `POST /internal/signals/run-daily` once with webhook target=**test**; diff the resulting cohort vs the DEX preview for the same slug → assert row parity.
2. Enable hq-x cron. For ONE day, PATCH DEX `ops.gtm_signals` `webhook_target=test` (empty test url) so the Modal cron dispatches nowhere-real; only hq-x dispatches to prod n8n. Confirm n8n gets exactly one payload per signal from hq-x.
3. `modal app stop data-engine-x-gtm-usaspending-trigger`. hq-x already firing → dispatch never dark.

### PR 5 — retire (after one clean hq-x cycle)
Delete DEX `gtm_signals_v1` CRUD/preview/fire + `gtm_usaspending_trigger_app` + `gtm_signal_cohort` FPDS body + hq-x `dex_client` signal methods. Drop `ops.gtm_signals` in a trailing migration after a grace cycle.

## Assumptions
- cron stays `0 9 * * *`; one-day dual-run; prod backfill now (dark).
- legacy n8n payload shape `{signal_slug, fired_at, row_count, rows}` preserved byte-for-byte.
- `GTM_MCP_URL` + `GTM_MCP_AUTH_TOKEN` present in hq-x runtime (hq-all/prd) for the schema gate + preview.
- hq-x runtime Doppler = hq-all/prd (has `DEX_SERVICE_TOKEN`, `HQX_DB_URL_*`, `GTM_MCP_*`).

## Known risks (attack these + find more)
- R-cron-schema: cron depends on gtm-mcp for `allowed_columns`. Mitigation: skip-and-log + DEX execute-time validation. Is skip-and-log correct, or should the cron compile WITHOUT a schema gate (trust DEX)? Does the compiler even allow that (it REQUIRES `allowed_columns`)?
- R-parity: hq-x compiled cohort must equal the legacy Modal cohort for the seeds (same WHERE, same SAM INNER JOIN, same dispatch payload). The legacy ordered by `TRY_CAST(...AS DOUBLE)`; PR3 orders lexically → truncated-cohort membership can differ.
- R-double-dispatch: dual-run window. Mitigation order: flip Modal→test sink BEFORE enabling hq-x? or after? Get the sequence exactly right.
- R-run-agent: the rewired `/run-agent` must produce the exact preview-shaped dict the existing `_format_initial_user_message` consumes, else agent authoring breaks.
- R-mcp-protocol: `gtm_mcp_client` hand-rolls MCP streamable-HTTP (session init + tools/call). Easy to get wrong.
- R-prod-deploy-order: PR4 rewires LIVE endpoints to read `business.gtm_signals`; if prod backfill (a) hasn't run when (b) deploys, `/run-agent` reads empty. Ordering is load-bearing.
- R-webhook-target: prod signal rows after backfill — is `webhook_prod_url` populated? The live DEX signals (`usaspending_net_new_100k`, `usaspending_test_permissive`) — do they carry prod webhook urls, or did the backfill map them correctly?
