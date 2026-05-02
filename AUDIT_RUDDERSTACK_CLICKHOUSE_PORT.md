> **Superseded by [`DIRECTIVE_HQX_ANALYTICS_REMAINDER.md`](DIRECTIVE_HQX_ANALYTICS_REMAINDER.md).**
>
> ClickHouse is **out of scope** as of 2026-04-29 — the cluster will not be
> provisioned and every analytics endpoint is Postgres-only. The
> RudderStack write fan-out shipped in PR #41; the Postgres analytics
> endpoints shipped in PRs #35, #36, #37, #39, #40 (slices 1b–1f).
> Post-ship summary: [`docs/analytics-buildout-pr-notes.md`](docs/analytics-buildout-pr-notes.md).
>
> The original audit content below remains for archaeology only — the
> ClickHouse-fallback architecture and `ch_query` / `ch_available`
> patterns it describes were not adopted.

---

# RudderStack & ClickHouse Port Audit: outbound-engine-x → hq-x

**Audit Date:** 2026-04-29
**Source:** `/Users/benjamincrane/outbound-engine-x`
**Destination:** `/Users/benjamincrane/hq-x` (worktree: `compassionate-pare-390210`)

---

## Executive Summary

**RudderStack:** Not present in either codebase. Zero references in source, config, dependencies, or env vars across both repos. There is nothing to port — RudderStack was never integrated into outbound-engine-x.

**ClickHouse:** Substantially but **incompletely ported** (~40% feature completeness for the broader analytics work; ~75% for the voice-specific path).

- ✅ **Write path ported:** `clickhouse.py` write client and `call_analytics.py` dual-write service exist in hq-x and are wired into the Vapi webhook completion handler.
- ⚠️ **Query path deferred:** hq-x's ClickHouse client is write-only (`insert_row` only); `ch_query()` and `ch_available()` were intentionally dropped. `voice_analytics.py` falls back to Postgres directly.
- ❌ **Multi-channel analytics router missing:** OEX's 1,098-line `src/routers/analytics.py` (campaign summaries, multi-channel, reliability, direct-mail) was not ported. Associated Pydantic models also missing.
- ❌ **Test coverage gap:** OEX has 6 test files specifically for ClickHouse / call_analytics / voice_analytics / multi-channel / authorization. hq-x has zero equivalent tests.

---

## RudderStack

### Source Inventory (outbound-engine-x)
- **Files:** 0
- **References:** No `rudder*` matches in `*.py`, `*.toml`, or any config.
- **Dependencies:** Not in `requirements.txt` or `pyproject.toml`.
- **Env vars:** None.

### Destination Inventory (hq-x)
- **Files:** 0
- **References:** None.
- **Config:** Not in `app/config.py`.

### Port Status
**N/A — nothing to port.** RudderStack appears to be out of scope entirely. If you expected RudderStack instrumentation to exist somewhere, it does not — neither repo has ever integrated it.

### Concerns
None, unless RudderStack was *expected* to be a target system. If the question came from an external roadmap mentioning RudderStack, that work would need to be designed and built fresh — there's no migration path because there's no source.

---

## ClickHouse

### Source Inventory (outbound-engine-x)

#### Core infrastructure
| File | Lines | Purpose |
|------|-------|---------|
| `src/clickhouse.py` | 96 | Hand-rolled HTTP client. Functions: `ch_insert()`, `ch_query()`, `ch_available()`. Auth via `X-ClickHouse-User/Key/Database` headers. |
| `src/config.py` (L71-74) | 4 | Pydantic settings: `clickhouse_url`, `clickhouse_user`, `clickhouse_password`, `clickhouse_database`. |
| `src/services/call_analytics.py` | 67 | `record_call_completion()` → fire-and-forget dual-write to `call_events` table. Calls `ch_available()` first, then `ch_insert()`. |
| `src/routers/voice_analytics.py` | 279 | Dashboard endpoints: `/api/analytics/voice/summary`, `/funnel`, etc. Prefers ClickHouse via `ch_query()`, falls back to Supabase `call_logs`. |
| `src/routers/analytics.py` | **1,098** | Multi-channel campaign analytics. Endpoints for campaigns, multi-channel rollups, reliability, direct-mail funnels, provider breakdowns. **Supabase-only — does not actually use ClickHouse.** |
| `src/models/analytics.py` | ~50+ | Pydantic response shapes (`CampaignAnalyticsSummaryResponse`, `MultiChannelAnalyticsResponse`, `ReliabilityAnalyticsResponse`, `DirectMailAnalyticsResponse`, etc.). |

#### Wiring
- `src/main.py:124` — `app.include_router(analytics.router)`
- `src/main.py:141` — `app.include_router(voice_analytics.router)`

#### Tests (6 files)
- `tests/test_clickhouse_client.py` (215 lines) — `ch_insert`/`ch_query`/`ch_available` unit tests with mocked httpx.
- `tests/test_call_analytics.py` (106 lines) — success path, unavailable, insert failure, missing cost fields, None duration.
- `tests/test_voice_analytics.py`
- `tests/test_multi_channel_analytics.py`
- `tests/test_analytics_endpoint.py`
- `tests/test_analytics_authorization_matrix.py`

### Destination Inventory (hq-x)

#### Core infrastructure
| File | Lines | Notes |
|------|-------|-------|
| `app/clickhouse.py` | 54 | **Write-only.** Single function: `insert_row(table, row, *, timeout_seconds=5.0) -> bool`. Returns False on missing config or error; never raises. Header comment explicitly states: _"No query helper."_ |
| `app/config.py` (L51-56) | 6 | Settings (UPPER_SNAKE_CASE): `CLICKHOUSE_URL`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD: SecretStr`, `CLICKHOUSE_DATABASE="default"`. Docstring: _"All optional — analytics is fire-and-forget and skips when unconfigured."_ |
| `app/services/call_analytics.py` | 54 | Same shape as OEX, refactored for brand-axis: `org_id → brand_id`, `company_id → partner_id`, `company_campaign_id → campaign_id`. Skips the `ch_available()` precheck (always attempts insert; `insert_row` handles unconfigured gracefully). |
| `app/routers/voice_analytics.py` | 209 | **Postgres-only.** Same endpoint shape (`/api/brands/{brand_id}/analytics/voice/summary`, `/funnel`). Header docstring acknowledges: _"hq-x's clickhouse.py only exposes a fire-and-forget insert_row writer (no query helper), so this router queries Postgres directly. A ClickHouse query path can be layered on later."_ |
| `app/routers/vapi_analytics.py` | 52 | **New, not from OEX.** Passthrough to Vapi's analytics API. |

#### Wiring
- `app/main.py:217` — `app.include_router(vapi_analytics_router.router)`
- `app/main.py:228` — `app.include_router(voice_analytics_router.router)`
- **Missing:** No equivalent of OEX `analytics.router` (the 1,098-line multi-channel router).

Call-completion dual-write is wired in: `app/routers/vapi_webhooks.py:605` calls `record_call_completion(...)`.

#### Tests
- **None** for ClickHouse client, `call_analytics`, or analytics routers in hq-x.

#### Env / docs
- No `.env.example` in worktree documenting the `CLICKHOUSE_*` vars.

---

### Port Status — Component by Component

| Component | OEX | HQ-X | Completeness |
|-----------|-----|------|--------------|
| ClickHouse HTTP client | 96 lines (insert/query/available) | 54 lines (insert only) | **~55%** — write works, query intentionally deferred |
| Config / env vars | ✅ 4 fields (lowercase) | ✅ 4 fields (UPPER_SNAKE_CASE) | **100%** |
| `record_call_completion` service | ✅ 67 lines | ✅ 54 lines (brand-axis schema) | **100%** functionally |
| Voice analytics router | 279 lines, CH preferred + SB fallback | 209 lines, Postgres direct | **~75%** — endpoints exist, but no ClickHouse query path |
| Multi-channel analytics router | 1,098 lines, 14 endpoints | ❌ Absent | **0%** |
| Analytics Pydantic models | ✅ ~10 response shapes | ❌ Absent | **0%** |
| Test coverage (CH/analytics) | 6 files | 0 files | **0%** |
| `main.py` router count | 44 | 37 | analytics router missing |

---

### Working? — Runtime assessment

**What appears to be working in hq-x:**
- ClickHouse writes are wired and reachable. `record_call_completion()` is invoked on Vapi call completion (`app/routers/vapi_webhooks.py:605`). If `CLICKHOUSE_URL/USER/PASSWORD` are set in the environment, rows should land in `call_events`. If not set, `_is_configured()` short-circuits and the write is silently skipped — by design.
- Voice analytics endpoints work against Postgres `call_logs`. Functional regardless of ClickHouse state.
- Vapi analytics passthrough router works (independent of ClickHouse).

**What is not working / not present:**
- ClickHouse query layer — there is no path from hq-x to *read* from ClickHouse. Anything dashboard-ish that wants ClickHouse aggregations will not get them.
- Multi-channel campaign analytics endpoints — entirely missing. Any frontend or downstream caller that depended on `/api/analytics/campaigns/...`, `/api/analytics/multi-channel/...`, `/api/analytics/reliability`, `/api/analytics/direct-mail/...` will get 404s.
- No tests — regressions in the write path or service refactor will go undetected by CI.

---

### Concerns / Things to Investigate

1. **Was the multi-channel analytics router deliberately dropped, or is it pending?**
   OEX's `src/routers/analytics.py` is 1,098 lines covering campaign summaries, client rollups, multi-channel progress, reliability, direct-mail funnels, and provider breakdowns. Nothing equivalent exists in hq-x. Need to determine: (a) was this scope-cut intentionally for the brand-axis migration, (b) replaced by something else, or (c) still on the to-do list? The OEX router is **Supabase-only** (it does not use ClickHouse at all), so the absence is unrelated to the ClickHouse query gap — it's a separate missing feature.

2. **`ch_available()` precheck removed.**
   OEX called `ch_available()` (a `SELECT 1` against ClickHouse) before each `ch_insert()`. hq-x dropped this — now every call-completion attempts an insert, relying on `insert_row` to handle errors silently. Effect: if ClickHouse is degraded, hq-x will log an error per call rather than skipping cleanly. Low severity (still fire-and-forget), but worth knowing.

3. **No `.env.example` documenting `CLICKHOUSE_*` vars.**
   Production runners need to know these knobs exist. Add them to whatever the worktree's canonical env doc is.

4. **Pydantic field-name shift (lowercase → UPPER_SNAKE_CASE).**
   OEX: `clickhouse_url`. hq-x: `CLICKHOUSE_URL`. Pydantic's env-var resolution makes this transparent at runtime, but any code that referenced `settings.clickhouse_url` in OEX needs to be `settings.CLICKHOUSE_URL` in hq-x. Worth grepping for stale lowercase references during the port.

5. **Schema rename in `call_events` payload.**
   `org_id → brand_id`, `company_id → partner_id`, `company_campaign_id → campaign_id`. If a single ClickHouse cluster receives writes from both OEX and hq-x simultaneously (e.g., during cutover), the table schema needs to accommodate both shapes — or one writer needs to be turned off. Verify the cutover plan.

6. **Test parity.**
   OEX has comprehensive ClickHouse / analytics tests; hq-x has none. At minimum, port `test_clickhouse_client.py` and `test_call_analytics.py` to lock in the write contract.

7. **Verify production writes are actually landing.**
   Confirm `CLICKHOUSE_*` env vars are set in hq-x's deploy environment and that `call_events` rows are appearing for live calls. Because the path is silent-on-failure, an unconfigured deploy would look identical to a working one from the application's perspective.

---

## Recommended Next Steps

1. **Decide multi-channel analytics fate.** Either port `src/routers/analytics.py` + `models/analytics.py` to hq-x (sizable: ~1,150 lines + tests + brand-axis refactor), or formally deprecate the endpoints and update any downstream consumers.
2. **Add `ch_query()` to `app/clickhouse.py`.** The hq-x source comments already flag this as "to be layered on later." Once present, retrofit `voice_analytics.py` to prefer ClickHouse with Postgres fallback (matching OEX behavior).
3. **Port the ClickHouse and call_analytics tests.** These are small, mock-based, and high-leverage.
4. **Verify production wiring.** Check Doppler / Railway env for `CLICKHOUSE_*` vars in the hq-x deploy. Spot-check `call_events` for recent rows.
5. **Document the env vars** in `.env.example` (or hq-x equivalent) so future operators know they exist and that absence = silent skip.
6. **Resolve dual-write coexistence** if both OEX and hq-x are writing to the same ClickHouse table during cutover — confirm schema compatibility for the renamed fields.

---

## Evidence Index (file paths)

**OEX (source):**
- `/Users/benjamincrane/outbound-engine-x/src/clickhouse.py`
- `/Users/benjamincrane/outbound-engine-x/src/config.py:71-74`
- `/Users/benjamincrane/outbound-engine-x/src/services/call_analytics.py`
- `/Users/benjamincrane/outbound-engine-x/src/routers/voice_analytics.py`
- `/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py`
- `/Users/benjamincrane/outbound-engine-x/src/models/analytics.py`
- `/Users/benjamincrane/outbound-engine-x/src/main.py:124,141`
- `/Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py`
- `/Users/benjamincrane/outbound-engine-x/tests/test_call_analytics.py`

**HQ-X (destination):**
- `app/clickhouse.py`
- `app/config.py:51-56`
- `app/services/call_analytics.py`
- `app/routers/voice_analytics.py`
- `app/routers/vapi_analytics.py`
- `app/routers/vapi_webhooks.py:605` (dual-write call site)
- `app/main.py:217,228`
