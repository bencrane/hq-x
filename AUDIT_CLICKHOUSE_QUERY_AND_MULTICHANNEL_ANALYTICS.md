> **Superseded by [`DIRECTIVE_HQX_ANALYTICS_REMAINDER.md`](DIRECTIVE_HQX_ANALYTICS_REMAINDER.md).**
>
> ClickHouse is **out of scope** as of 2026-04-29 — the cluster will not
> be provisioned. Every analytics endpoint shipped in PRs #35, #36, #37,
> #39, #40 is Postgres-only and carries `"source": "postgres"`. The
> multi-channel rollups described in this audit are now implemented as
> the campaign-summary (slice 1b) and channel_campaign-summary (slice
> 1f) endpoints, both Postgres-only with the synthetic-step fallback for
> voice/SMS. Post-ship summary:
> [`docs/analytics-buildout-pr-notes.md`](docs/analytics-buildout-pr-notes.md).
>
> The audit content below remains for archaeology only.

---

# Deep Dive: ClickHouse Query Helper & Multi-Channel Analytics — Port vs. Rebuild

**Date:** 2026-04-29
**Source:** `/Users/benjamincrane/outbound-engine-x` (OEX)
**Destination:** `/Users/benjamincrane/hq-x` (worktree: `compassionate-pare-390210`)

Follow-up to `AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`. Investigates the two open items from that audit: (a) the ClickHouse query helper and (b) the missing multi-channel analytics router. Goal: enough context to choose port vs. rebuild vs. defer.

---

## TL;DR

| Item | Recommendation | Effort |
|------|---------------|--------|
| **ClickHouse `ch_query()` helper** | **Port verbatim, ~30 lines.** Trivial. | 1–2 hours |
| **Voice analytics → ClickHouse fallback path** | Port alongside `ch_query()`; mechanical brand-axis renames. | +2–3 hours |
| **Campaign summary / clients rollup analytics** | **Port** with brand-axis adapters. Low risk, high value. | ~1 week |
| **Reliability + message-sync-health analytics** | **Port.** Minimal logic; webhook_events table already exists in hq-x. | ~3–4 days each |
| **Direct-mail analytics** | **Port.** `direct_mail_pieces` table already exists (migration 0011). | ~4–5 days |
| **Multi-channel campaign analytics** | **DEFER or rebuild later.** hq-x has no multi-channel campaign concept; required tables don't exist. | 4–6 weeks if pursued |

The query helper is a near-free win. The analytics router decomposes into 4 independent feature groups; 3 are clean ports, 1 (multi-channel) is blocked on a missing architectural primitive.

---

## Part 1: ClickHouse Query Helper

### What it is in OEX

**File:** `/Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62-96`

Two functions on top of httpx:

```python
ch_query(query: str, params: dict | None = None) -> list[dict]
ch_available() -> bool
```

- **Parameterization:** ClickHouse-native — placeholders like `{org_id:String}` resolved via `param_<name>` URL query params (line 69). No string interpolation, no SQL injection risk.
- **Format:** Query is forced to JSONEachRow, response parsed line-by-line (line 80).
- **Timeout:** 10 s for query, 3 s for `ch_available()` health check.
- **Auth:** Headers `X-ClickHouse-User`, `X-ClickHouse-Key`, `X-ClickHouse-Database` (lines 24–28).
- **Error handling:** Raises `RuntimeError` on HTTP ≥400 (truncates response to 200 chars). `ch_available()` swallows everything and returns False.

Total surface: ~35 lines of code. Zero deps beyond httpx (already in hq-x).

### Where it's used in OEX

All five call sites are inside `src/routers/voice_analytics.py`:

| Endpoint | OEX line | Query target | Aggregation | Filter |
|----------|----------|--------------|-------------|--------|
| `/summary` | 74–78 | `call_events` | `GROUP BY outcome`, counts/durations/costs | `org_id`, date range, optional `company_id` |
| `/by-campaign` | 122–129 | `call_events` | `GROUP BY company_campaign_id, outcome` | `org_id`, date range |
| `/daily-trend` | 169–175 | `call_events` | `toDate(created_at), count()` | `org_id`, date range |
| `/cost-breakdown` | 206–213 | `call_events` | `SUM(cost_*)` per category | `org_id`, date range |
| `/transfer-rate` | 242–250 | `call_events` | `countIf(outcome='transferred')` per campaign | `org_id`, date range |

**Pattern:** Every endpoint calls `ch_available()` first. If True → ClickHouse aggregation, return with `"source": "clickhouse"`. If False → fetch raw rows from Supabase `call_logs`, aggregate in Python, return with `"source": "supabase"`. Same response shape either way.

### Why it matters

ClickHouse is doing what ClickHouse is for: columnar group-by on a high-volume event stream. Postgres can answer the same questions but pays a full-scan cost that scales linearly with call volume. For voice dashboards covering 30–90 day windows, this is the difference between sub-second and multi-second responses.

The `ch_available()` precheck means the perf upgrade is **opt-in by environment**: deploys without ClickHouse configured fall through to Postgres without code changes.

### Schema: `call_events` table (inferred from inserts)

Columns populated by `record_call_completion()` (`src/services/call_analytics.py:37-57`):

```
event_id, org_id, company_id, company_campaign_id,
call_sid, direction, amd_strategy, amd_result,
outcome, duration_seconds,
cost_transport, cost_stt, cost_llm, cost_tts, cost_vapi, cost_total,
vapi_call_id, ended_reason, success_evaluation,
created_at
```

No DDL is checked in (no `CREATE TABLE` found in OEX) — the table is provisioned out-of-band on the ClickHouse cluster.

### Brand-axis fit (hq-x)

hq-x already writes to `call_events` via `app/services/call_analytics.py`, but with renamed fields:

- `org_id` → `brand_id`
- `company_id` → `partner_id`
- `company_campaign_id` → `campaign_id`

**Implication for the query port:** The query strings need the same renames. The `ch_query()` function itself is schema-agnostic — port verbatim.

Outcome enum also drifted: OEX uses `outcome='transferred'`; hq-x uses `outcome='qualified_transfer'` (per `app/routers/voice_analytics.py:190`). One-line change per query.

### Port plan

1. **Append `ch_query()` and `ch_available()` to `app/clickhouse.py`** (~40 lines). Re-use existing `_is_configured()` and `_headers()` helpers — just unwrap `SecretStr` per hq-x's pattern.
2. **Wrap each endpoint in `app/routers/voice_analytics.py`** with an `if ch_available(): try ClickHouse … else: Postgres`. Postgres path stays as the fallback (already written and working).
3. **Adjust query strings:**
   - `org_id` → `brand_id`
   - `company_campaign_id` → `campaign_id`
   - `'transferred'` → `'qualified_transfer'`
4. **Confirm the production `call_events` schema** matches the renamed columns (writes are already going to it, so this should be true, but verify).

**LOC delta:** ~100 lines added, ~15 removed (deferred-comment cleanup). **Effort:** 1–2 hours.

**Risk:** Very low. The fallback path is preserved, so a misconfigured or down ClickHouse degrades gracefully to current behavior.

### Recommendation
**Port. Do this first; it's nearly free and unblocks the rest.**

---

## Part 2: Multi-Channel Analytics Router (`src/routers/analytics.py`)

The 1,098-line OEX router is not a single feature — it's seven endpoints across four mostly-independent feature groups. Treating it as one chunk is what makes it look daunting; split apart, most of it is straightforwardly portable.

### Endpoint inventory

| Method | Path | Feature group | Auth |
|--------|------|---------------|------|
| GET | `/api/analytics/campaigns` | Campaign summary | `analytics.read` + scope |
| GET | `/api/analytics/clients` | Campaign summary (org rollup) | `analytics.read` + scope |
| GET | `/api/analytics/campaigns/{id}/sequence-steps` | Campaign summary (per-step) | `analytics.read` + campaign auth |
| GET | `/api/analytics/reliability` | Reliability/sync | `analytics.read` + scope |
| GET | `/api/analytics/message-sync-health` | Reliability/sync | `analytics.read` + scope |
| GET | `/api/analytics/direct-mail` | Direct mail | `analytics.read` + scope; max 93-day window |
| GET | `/api/analytics/campaigns/{id}/multi-channel` | Multi-channel | `analytics.read` + campaign auth |

Common query params: `company_id`, `from_ts`, `to_ts`, `mine_only`, `limit`, `offset`. Auth helpers: `_require_analytics_read()`, `_resolve_company_scope()`, `_get_campaign_for_auth()`.

Models live in `src/models/analytics.py` (campaign/client/reliability/direct-mail) and `src/models/multi_channel.py` (multi-channel rollups).

### Feature group 1: Campaign summary (~180 LOC)

**Endpoints:** `/campaigns`, `/clients`, `/campaigns/{id}/sequence-steps`

**What it does:**
- Per campaign: count leads by status, count messages by direction, compute `reply_rate = inbound / outbound`, find `max(activity_at)`.
- `/clients` rolls up to company level.
- `/sequence-steps` walks `company_campaign_messages` per campaign, attributes inbound replies to the lead's last outbound step number, aggregates per step.

**OEX tables:** `company_campaigns`, `company_campaign_leads`, `company_campaign_messages`.

**hq-x mapping:**
- `company_campaigns` → `campaigns` (brand_id-scoped)
- "Leads" don't exist as first-class entities for voice; would need to use `call_logs.to_phone` as the recipient identity, or just count calls.
- For non-voice channels (email, etc.) hq-x's data model is less developed — the campaign summary makes most sense if scoped to channels we already have rich data for.

**Risk:** Low–Medium. Logic is mechanical aggregation; the friction is the lead-vs-call semantic mismatch and figuring out which channels we actually want to surface.

**Recommendation: Port** when there's a dashboard consumer ready for it. Don't port speculatively.

### Feature group 2: Reliability / sync health (~120 LOC)

**Endpoints:** `/reliability`, `/message-sync-health`

**What it does:**
- `/reliability`: groups `webhook_events` by `provider_slug`, counts replays (`status='replayed'`), sums `replay_count`, counts errors.
- `/message-sync-health`: per-campaign provider sync status with error tracking.

**OEX tables:** `webhook_events`, plus campaign tables.

**hq-x mapping:**
- `webhook_events` already exists (migration `0010_webhook_events.sql`) with the same shape and `provider_slug` filter.
- Just need to add a `brand_id` filter.

**Risk:** Low. This is the cleanest port in the entire router.

**Recommendation: Port.** Useful for ops/SRE visibility; trivial effort.

### Feature group 3: Direct-mail analytics (~350 LOC)

**Endpoint:** `/direct-mail`

**What it does:** Date-range guarded (max 93 days, `max_rows=20000` cap). Computes:
- Volume by `(piece_type, status)`
- Delivery funnel (queued/processing → created → processed → in_transit → delivered → returned/failed)
- Failure reason breakdown (extracted from `webhook_events` payloads — `dead_letter.reason`, `ingestion.signature_reason`, fallback to `provider_error`)
- Daily trends (pre-populated bucket map across the date range)

**OEX tables:** `company_direct_mail_pieces`, `webhook_events` (Lob).

**hq-x mapping:**
- `direct_mail_pieces` table exists in hq-x (migration `0011_direct_mail_lob.sql`).
- `webhook_events` exists (migration 0010).
- Field renames (`org_id`/`company_id` → `brand_id`/`partner_id`).

**Risk:** Low. The core logic — funnel mapping, daily bucketing, payload extraction — is reusable verbatim. Just adjust column names.

**Recommendation: Port** when direct-mail dashboards are wanted. Largest of the "easy" ports but still well-bounded.

### Feature group 4: Multi-channel campaign analytics (~400 LOC including helpers)

**Endpoint:** `/campaigns/{id}/multi-channel`

**What it does:** This is the actually hard one. It assumes a `campaign_type='multi_channel'` campaign and reads from three tables that don't exist in hq-x:

- `campaign_sequence_steps` — defines step_order → (channel, action_type)
- `campaign_events` — every step/engagement event with channel + step_order + event_type
- `campaign_lead_progress` — per-lead step status (pending/executing/executed/skipped/failed/completed)

It then builds two rollups:

1. **Event rollups** (`_build_multi_channel_event_rollups`): normalizes channel from explicit field or via `CHANNEL_BY_PROVIDER_SLUG` map (smartlead/emailbison/instantly→email, heyreach→linkedin, lob→direct_mail, voicedrop→voicemail). Aggregates by channel and by step_order, computes failure rates, identifies the highest-failure step.
2. **Progress rollups** (`_build_multi_channel_progress_rollup`): counts leads by step status and by current step.

**hq-x reality:** None of the underlying tables exist. hq-x campaigns are currently single-channel (voice/sms/ivr) and brand-scoped. There is no concept of "step N is email, step N+1 is direct mail" in the data model. Porting this analytics router would require building the entire multi-channel campaign primitive first:

1. Add `campaign_type='multi_channel'` to campaigns
2. Create `campaign_sequence_steps` table
3. Create `campaign_events` table (or dual-write to ClickHouse for scale)
4. Create `campaign_lead_progress` table
5. Wire every provider webhook to emit `campaign_events`
6. *Then* port the analytics

This is a 4–6 week epic, not an analytics task.

**Risk:** High. This isn't a port question — it's a product-architecture question.

**Recommendation: DEFER.** Don't port the analytics until/unless the underlying multi-channel primitive is built in hq-x. When that day comes, the OEX implementation is a useful reference (especially the channel normalization map and the failure-rate ranking heuristic), but it should be re-implemented against whatever schema hq-x ends up with — not lifted directly.

If multi-channel campaigns are not on the hq-x roadmap, **delete this from the porting backlog entirely.**

---

## Consumers (who calls these endpoints)

The OEX frontend (`/Users/benjamincrane/outbound-engine-x-frontend`) consumes 6 of the 7 endpoints:

- `src/features/analytics/api.ts` → `/campaigns`, `/clients`, `/reliability`, `/message-sync-health`
- `src/features/campaigns/api.ts` → `/campaigns/{id}/sequence-steps`
- `src/features/direct-mail/api.ts` → `/direct-mail`

The multi-channel endpoint is in OEX code but **not exported in `openapi.json`** — likely an internal/experimental surface, which further weakens the case for porting it.

No consumers outside the frontend.

---

## hq-x's current analytics surface

For comparison, hq-x today exposes:

- `app/routers/voice_analytics.py` — brand-scoped voice dashboards (summary, by-campaign, daily-trend, cost-breakdown), Postgres-backed
- `app/routers/vapi_analytics.py` — Vapi passthrough proxy

That's it. No campaign-summary / client-rollup / reliability / direct-mail / multi-channel surfaces yet. Whatever gets ported is greenfield.

---

## Auth / scope translation

OEX's analytics endpoints use a three-level scope: `org_id → company_id → company_campaign_id`. The helper `_resolve_company_scope(auth, company_id)` returns the auth-bound company for user-scoped tokens, or validates a requested company_id for org admins.

hq-x's `require_flexible_auth()` returns a brand-scoped context. There's no org/company hierarchy. Translation is a simplification, not a complication: drop the `_resolve_company_scope` indirection, replace with `brand_id` from auth context. This is a few-line change per endpoint.

---

## Suggested roadmap

If we decide to port the easy stuff:

**Phase 0 (1–2 hrs, do anytime):** Port `ch_query()` + add ClickHouse fallback to existing `voice_analytics.py` endpoints. Pure win.

**Phase 1 (~1 week):** Campaign summary analytics (`/campaigns`, `/clients`, `/campaigns/{id}/sequence-steps`) — only if there's a dashboard consumer ready. Don't port speculatively.

**Phase 2 (~3–4 days):** Reliability + message-sync-health. Useful for ops; minimal cost.

**Phase 3 (~4–5 days):** Direct-mail analytics. Tables already exist; clean port.

**Phase 4: Multi-channel.** Don't. Revisit only if/when hq-x grows multi-channel campaign primitives. At that point, treat the OEX code as reference, not as a port target.

**Total for Phases 0–3: ~2.5 weeks.** Phase 4 is a separate product decision.

---

## Open questions for you

1. **Is multi-channel campaign support on the hq-x roadmap?** This is the big fork in the road. If no, half of the OEX analytics router becomes irrelevant.
2. **Is there a frontend consumer queued up** for any of the easy ports? Reliability/direct-mail/campaign-summary all become much more compelling if a dashboard is waiting on them, vs. speculative.
3. **What's the actual production volume on `call_events`?** If it's small (<1M rows/month), the ClickHouse query path is a nice-to-have. If it's large, it's a real perf unlock and Phase 0 should jump the queue.

---

## File references

**OEX:**
- `src/clickhouse.py:62-96` — `ch_query`, `ch_available`
- `src/routers/voice_analytics.py:74-250` — query call sites
- `src/routers/analytics.py` (1,098 lines) — multi-channel router
- `src/models/analytics.py`, `src/models/multi_channel.py` — response models
- `src/services/call_analytics.py:37-57` — `call_events` schema (inferred)

**hq-x:**
- `app/clickhouse.py` — write-only client
- `app/routers/voice_analytics.py:190` — `qualified_transfer` outcome enum
- `app/services/call_analytics.py` — brand-axis writer
- `migrations/0010_webhook_events.sql` — table for reliability port
- `migrations/0011_direct_mail_lob.sql` — table for direct-mail port

**OEX frontend (consumer evidence):**
- `src/features/analytics/api.ts`
- `src/features/campaigns/api.ts`
- `src/features/direct-mail/api.ts`
