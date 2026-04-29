# Directive — hq-x analytics buildout: RudderStack + ClickHouse query layer + motion analytics

**Target audience:** Implementation agent picking up the analytics workstream after the GTM-motions PR ([#18](https://github.com/bencrane/hq-x/pull/18)) and the audit pair ([#16](https://github.com/bencrane/hq-x/pull/16)).

**Source repos:**
- hq-x (this repo): `/Users/benjamincrane/hq-x` — destination.
- outbound-engine-x (OEX): `/Users/benjamincrane/outbound-engine-x` — read-only reference for the analytics router.

**Required prereading (in order, ~15 min):**
1. [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) — what's already on the floor.
2. [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md) — feature decomposition + brand-axis fit.
3. [`docs/gtm-model.md`](docs/gtm-model.md) — the new two-layer outreach model. **This is the architectural anchor.** Everything in this directive flows from it.
4. [`docs/gtm-model-pr-notes.md`](docs/gtm-model-pr-notes.md) — what shipped in #18, including the `emit_event()` choke point.
5. [`docs/tenancy-model.md`](docs/tenancy-model.md) — `organizations` / `organization_memberships` / two-axis roles. Auth model for new endpoints.
6. [`app/services/analytics.py`](app/services/analytics.py) (85 lines) — current state of the analytics emit path. Read every line.

---

## 1. Why

The audit exit position was: ClickHouse writes work, the query helper is deferred, the multi-channel analytics router from OEX would be a 1,098-line port but most of it is moot because hq-x had no multi-channel campaign primitive.

[#18](https://github.com/bencrane/hq-x/pull/18) just shipped that primitive — `gtm_motions` + channel-typed `campaigns`, with the canonical six-tuple `(organization_id, brand_id, gtm_motion_id, campaign_id, channel, provider)` enforced at every emit site by [`app/services/analytics.py::emit_event`](app/services/analytics.py). That changes the calculus:

- The shape of "multi-channel analytics" is now well-defined (motion → child campaigns by channel/provider) and matches the data model directly.
- The RudderStack write side is a deliberate no-op stub — see [`app/services/analytics.py:9-11`](app/services/analytics.py:9). The choke point exists; only the client is missing.
- ClickHouse writes are already going through `emit_event` → `app.clickhouse.insert_row`. A query helper just adds the read path.

So the work is: (a) wire the RudderStack client into the existing emit choke point, (b) add the ClickHouse query helper, (c) build the analytics router on top of (b) using the GTM model directly, instead of porting the OEX router structure verbatim. **Don't port the OEX router as-is** — its `step_order` / `sequence_step` / `campaign_lead_progress` model doesn't exist here and shouldn't be reintroduced. Use OEX as a behavior reference only.

---

## 2. Scope

### In scope (this directive)

1. **RudderStack write integration** — turn the `emit_event()` shim into a real `analytics.track()` call. Config, client, retry, test seam.
2. **ClickHouse query helper** — port `ch_query()` + `ch_available()` from OEX, adapt to hq-x config conventions. Wire ClickHouse-preferred + Postgres-fallback into `voice_analytics.py`.
3. **Motion analytics router** — new `/api/v1/analytics/*` endpoints scoped to organizations. Three feature groups:
   - **Motion rollup** — given a `gtm_motion_id`, return per-channel and per-campaign breakdowns of events, outcomes, costs.
   - **Campaign analytics** — per-campaign drilldown. Same shape across channels (voice/sms/email/direct_mail), with channel-specific extensions where the data warrants.
   - **Reliability + direct-mail funnels** — port verbatim from OEX with field renames; the underlying `webhook_events` and `direct_mail_pieces` tables already match.
4. **Event schema in ClickHouse** — a single `events` table the `emit_event()` helper writes to (in addition to event-typed tables). Becomes the substrate for cross-channel motion analytics.
5. **Tests** — port + extend OEX's analytics test pattern. Pure-function tests for aggregation logic; service-level tests with the in-memory `get_db_connection` fake; ClickHouse client tests with mocked httpx; RudderStack tests with mocked client.

### Out of scope (defer; flag in PR notes)

- **Per-lead state across channels** — explicitly out of scope per [`docs/gtm-model-pr-notes.md:154-155`](docs/gtm-model-pr-notes.md:154-155). Don't build `campaign_lead_progress` analogues.
- **Cross-motion analytics rollup** ([`docs/gtm-model-pr-notes.md:157`](docs/gtm-model-pr-notes.md:157)) — defer.
- **Frontend** — backend only.
- **Tightening `direct_mail_pieces.campaign_id` / `gtm_motion_id` to NOT NULL** — per [`docs/gtm-model-pr-notes.md:128-132`](docs/gtm-model-pr-notes.md:128-132), wait for orphan-free real-data backfill.
- **Sequence-step analytics** — OEX had `/api/analytics/campaigns/{id}/sequence-steps`. The hq-x model has no sequence-step concept (campaigns are atomic; choreography is via motion `start_offset_days` between sibling campaigns). Don't port. If a future feature wants intra-campaign step granularity, it's a separate design.
- **OEX's `/api/analytics/clients` org-rollup** — replaced by motion rollup in the new model. Don't port.
- **Message sync health endpoint** — depends on email/linkedin providers that aren't wired in hq-x yet; defer until they are.

---

## 3. Architecture decisions you must make on day 0

### 3.1 RudderStack: server-side write key + node SDK or HTTP?

The Python `rudder-sdk-python` client exists and is straightforward; use it. Don't roll an HTTP client unless the SDK is unmaintained — verify on PyPI before starting. Configure via:

- `RUDDERSTACK_WRITE_KEY` (SecretStr, optional)
- `RUDDERSTACK_DATA_PLANE_URL` (str, optional)
- Both unset → silent skip, identical pattern to the ClickHouse client. **The fire-and-forget contract in [`app/services/analytics.py:9-11`](app/services/analytics.py:9) is non-negotiable: an unconfigured analytics layer must not break production paths.**

The client is a singleton; init it lazily on first `track()` call so importing `app.services.analytics` in tests doesn't require the SDK to be configured.

### 3.2 ClickHouse `events` table — one wide table or per-event-type?

OEX did per-event-type (`call_events`). hq-x's `emit_event()` already accepts `clickhouse_table` and writes to whatever you point it at, so per-type is supported.

**Recommended:** Add **one wide `events` table** alongside the existing typed tables. Schema:

```sql
events (
  event_id           UUID,
  event_name         String,
  occurred_at        DateTime64(3),
  organization_id    UUID,
  brand_id           UUID,
  gtm_motion_id      UUID,
  campaign_id        UUID,
  channel            LowCardinality(String),
  provider           LowCardinality(String),
  properties         String  -- JSON blob of event-specific props
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (organization_id, gtm_motion_id, occurred_at, event_id)
```

Reasoning: motion rollup queries are the primary read pattern — `WHERE gtm_motion_id = ? AND occurred_at BETWEEN ?` group by `channel, provider`. A single wide table with that ORDER BY makes those near-instant. Per-event-type tables (e.g. `call_events`) stay for backward compat and for queries that need typed columns; the wide `events` table becomes the cross-channel substrate.

**Don't** ship the `CREATE TABLE` DDL in a Postgres migration — ClickHouse is provisioned out-of-band. Ship it as `docs/clickhouse-schema.md` or `scripts/clickhouse/events.sql`, and document that an operator runs it on the cluster.

### 3.3 Auth model for new endpoints

All new analytics endpoints are organization-scoped. Use `require_org_context` ([`app/auth/roles.py`](app/auth/roles.py)). Read the `active_organization_id` from `UserContext`. Every analytics query must filter by `organization_id` from the auth context — **never accept it as a query param**. Platform operators drive cross-org by setting `X-Organization-Id`, same pattern as `gtm-motions`.

### 3.4 Postgres fallback policy

When ClickHouse is unconfigured or `ch_available()` returns False:

- **Voice analytics endpoints** (already exist) — fall back to Postgres `call_logs`. Already implemented; preserve.
- **Motion rollup** — Postgres can answer the question by joining `call_logs` / `sms_messages` / `direct_mail_pieces` on `campaign_id` for each child campaign of the motion. Implement the fallback. It's slower but correct.
- **Reliability and direct-mail funnels** — these never touched ClickHouse in OEX. Postgres-only, no fallback path needed.

The response payload always includes `"source": "clickhouse"` or `"source": "postgres"` so consumers can see which path served them.

---

## 4. Phased implementation

Ship as **three** PRs, in order. Each is independently reviewable and deployable.

### Phase 1 — ClickHouse query helper + voice analytics fallback (~1 day)

**PR title:** "ClickHouse query helper + voice analytics ClickHouse fallback"

**Files:**
- `app/clickhouse.py` — append `ch_query(query, params=None)` and `ch_available()`. ~40 lines.
- `app/routers/voice_analytics.py` — wrap each of the 5 endpoints with `if ch_available(): try ClickHouse else: Postgres`. ~60 lines added.
- `tests/test_clickhouse_client.py` — new file. Mock httpx; test `insert_row`, `ch_query`, `ch_available` happy path + failure modes. Reference: [`/Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py) (215 lines — adapt patterns, don't copy verbatim).
- `tests/test_voice_analytics_clickhouse.py` — new file. Verify ClickHouse path is preferred when available, Postgres fallback when not. Mock `ch_available()` and `ch_query()`.

**Implementation notes:**
- Reference `ch_query` implementation: [`/Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62-80`](file:///Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62). It's ~20 lines. Use ClickHouse native parameterization (`{name:String}` placeholders + `param_<name>` URL params) — **do not interpolate values into the SQL string.**
- Adapt for hq-x's `SecretStr` config (`settings.CLICKHOUSE_PASSWORD.get_secret_value()`).
- Field renames in queries used by voice_analytics:
  - `org_id` → `brand_id` (we filter by brand here, not org)
  - `company_campaign_id` → `campaign_id`
  - `'transferred'` → `'qualified_transfer'` (outcome enum)
- Keep the Postgres fallback **byte-identical** to current behavior — it must remain correct on its own. ClickHouse is purely an opt-in perf layer.

**Acceptance:**
- `pytest tests/test_clickhouse_client.py tests/test_voice_analytics_clickhouse.py` green.
- Existing voice analytics tests pass unchanged (the new branch is gated by `ch_available()` which returns False without config).
- No new env vars are required to ship. Without `CLICKHOUSE_*` set, every request still goes to Postgres and the existing tests pass.

### Phase 2 — RudderStack write integration (~1–2 days)

**PR title:** "RudderStack: real client behind emit_event"

**Files:**
- `pyproject.toml` — add `rudder-sdk-python` (or whatever the current canonical PyPI package is — verify before adding).
- `app/config.py` — add `RUDDERSTACK_WRITE_KEY: SecretStr | None` and `RUDDERSTACK_DATA_PLANE_URL: str | None`.
- `app/rudderstack.py` — new module. Mirrors `app/clickhouse.py` shape:
  - `_is_configured()` — both env vars set.
  - `_get_client()` — lazy singleton init.
  - `track(event_name, *, anonymous_id, properties)` — fire-and-forget. Never raises.
  - `flush()` — call on app shutdown (FastAPI lifespan).
- `app/services/analytics.py` — replace the no-op shim with a real `rudderstack.track()` call. The six-tuple goes into `properties`; `anonymous_id` is the `organization_id` cast to str (a stable identifier per tenant; users aren't the entity here — orgs are).
- `app/main.py` — register the rudderstack flush in the lifespan shutdown handler.
- `tests/test_rudderstack_client.py` — mock the SDK; verify `track` is called with the expected payload and that an unconfigured client is a no-op.
- `tests/test_analytics_emit.py` — extend to assert RudderStack `track` is called when the env is configured. Use a fake client injected via dependency override.

**Implementation notes:**
- The SDK has a background thread that batches sends. Init it once. The `flush()` on shutdown is important — without it, in-flight events are dropped when the container restarts.
- **Do not** put the RudderStack write before logging — log first, then write. Order matters: if RudderStack init throws (it shouldn't, but…), logs still fire.
- Anonymous-id strategy: use `organization_id`. Identify-call later if/when we wire user-level events; out of scope here.
- `event_name`, `occurred_at`, six-tuple all go in the `properties` dict. RudderStack `track`'s `event` argument is `event_name`.

**Acceptance:**
- Without `RUDDERSTACK_*` env: every existing test passes; `emit_event` still logs + still writes to ClickHouse if configured; RudderStack code is a no-op.
- With env: a `track()` call is made per `emit_event()`. Verified by mock.
- App shutdown flushes the queue. Verified by mock + lifespan test.

### Phase 3 — Motion analytics router (~1–1.5 weeks)

**PR title:** "Motion analytics router + reliability + direct-mail funnels"

This is the largest phase. Build it incrementally — each endpoint is a separate commit if helpful for review.

#### 3.1 Wide `events` table substrate

- `docs/clickhouse-schema.md` — document the wide `events` table DDL (see §3.2 above) and the partitioning/ordering rationale.
- `app/services/analytics.py::emit_event` — when the caller passes `clickhouse_table`, ALSO insert a row into the wide `events` table. The wide row uses the six-tuple, `event_name`, `occurred_at`, and `properties` JSON-serialized into the `properties` column. Both writes are fire-and-forget.

The wide `events` table is what motion-level analytics queries join against. Per-event-type tables (`call_events`, future `direct_mail_piece_events_ch`, etc.) stay for typed-column queries.

#### 3.2 Motion rollup endpoint

`GET /api/v1/analytics/motions/{motion_id}/summary?from=&to=`

Returns:
```
{
  "motion": { "id", "name", "status", "start_date", "brand_id", "organization_id" },
  "campaigns": [
    {
      "campaign_id", "name", "channel", "provider", "status", "scheduled_send_at",
      "events_total", "outcomes": { "succeeded": N, "failed": N, "skipped": N },
      "cost_total_cents": N
    }
  ],
  "by_channel": [
    { "channel", "events_total", "outcomes", "cost_total_cents" }
  ],
  "by_provider": [
    { "provider", "events_total", "outcomes", "cost_total_cents" }
  ],
  "source": "clickhouse" | "postgres"
}
```

Auth: `require_org_context`. Filter by `organization_id` from auth + the requested `motion_id`. Verify the motion belongs to the auth's org before querying — return 404 if not (don't leak existence).

ClickHouse path: one query against the wide `events` table grouping by `(channel, provider, campaign_id)` plus an `outcome` column derived from `event_name`. Use ClickHouse-native parameterization for `motion_id`, `from`, `to`.

Postgres fallback: union over `call_logs`, `sms_messages`, `direct_mail_pieces`, joined to `business.campaigns` on `campaign_id` filtered by `gtm_motion_id`. Aggregate in SQL or in Python (your call — measure first).

#### 3.3 Campaign analytics endpoint

`GET /api/v1/analytics/campaigns/{campaign_id}/summary?from=&to=`

Same shape as motion rollup but scoped to one campaign. Channel-specific extensions:
- voice: include `transfer_rate`, `avg_duration_seconds`, cost breakdown (transport / stt / llm / tts / vapi).
- sms: include `delivery_rate`, `opt_out_count`.
- direct_mail: include the funnel (queued → processed → in_transit → delivered → returned/failed).
- email: scaffold — return zeros for now since emailbison isn't wired ([`docs/gtm-model-pr-notes.md:144-146`](docs/gtm-model-pr-notes.md:144-146)).

Auth: same as motion rollup. Verify the campaign's `organization_id` matches auth.

#### 3.4 Direct-mail funnel endpoint

`GET /api/v1/analytics/direct-mail?brand_id=&campaign_id=&from=&to=`

Port from OEX [`/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) (the `/direct-mail` endpoint, ~350 LOC). Field renames:
- `org_id`/`company_id` → `organization_id`/`brand_id`/`partner_id`
- Filter scope adjusted per §3.3 (auth context, not query param)

Keep OEX's safety gates: max 93-day window, `max_rows=20000` cap, paginated `failure_reason_breakdown` and `daily_trends`.

Tables involved (already present in hq-x): `direct_mail_pieces`, `webhook_events` (provider_slug='lob'). Postgres-only; no ClickHouse path needed.

#### 3.5 Reliability endpoint

`GET /api/v1/analytics/reliability?from=&to=`

Port from OEX. Group `webhook_events` by `provider_slug`, count replays, sum `replay_count`, count errors. Filter by `organization_id` from auth.

Cleanest port in the entire OEX router. ~120 LOC.

#### 3.6 Models

`app/models/analytics.py` (new file). Pydantic response shapes for the four endpoint responses above. Don't try to mirror OEX's models 1:1 — design fresh against the new payload shape.

#### 3.7 Router wiring

`app/routers/analytics.py` (new file). Mount at `/api/v1/analytics` in `app/main.py`. Don't reuse OEX's prefix (`/api/analytics`) — hq-x is on `/api/v1`.

#### 3.8 Tests

- `tests/test_motion_analytics_pure.py` — pure aggregation functions (group/sum/derive). Reference pattern: `tests/test_gtm_motions_pure.py`.
- `tests/test_motion_analytics_db_fake.py` — service-level with `get_db_connection` in-memory fake. Reference pattern: `tests/test_gtm_services_db_fake.py`.
- `tests/test_motion_analytics_clickhouse.py` — ClickHouse path with mocked `ch_query`. Verify the SQL parameters and the result-shape mapping.
- `tests/test_reliability_analytics.py` — Postgres-only.
- `tests/test_direct_mail_analytics.py` — Postgres-only; reuse OEX test patterns where logic is identical.

Test count target: 50+ new tests across the phase. Full suite must remain green.

**Acceptance for Phase 3:**
- All four endpoints work end-to-end against a seeded test DB.
- Motion rollup serves ClickHouse when configured (verified by mock), Postgres otherwise.
- Direct-mail funnel matches OEX behavior on a port-equivalence test (run identical inputs through both, compare outputs — if both repos are accessible during dev).
- Cross-org leakage tests: a request with org A's auth, asking about org B's motion, returns 404. **Add this test for every new endpoint.** This is the single highest-value safety check.
- ruff clean, full suite green (target: 393+ passed).

---

## 5. Hard rules

1. **Six-tuple is sacred.** Every event written via `emit_event()` carries the six-tuple. Don't add a code path that bypasses `emit_event()`. Don't accept `organization_id` as a request body field on any analytics endpoint — it comes from auth context.

2. **Org isolation tested per endpoint.** Every new endpoint gets a "user from org A asking about org B's resource → 404" test. No exceptions.

3. **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, `track()` never raise into the caller. They log on failure. Adding a `raise` to any of these is a bug.

4. **No silent assignment.** If a query needs a `motion_id` and the motion doesn't belong to the caller's org, return 404. Don't fall back to "any motion the user can see." Don't auto-pick.

5. **No stepwise/lead-progress concepts.** The OEX model has `step_order`, `step_status`, `campaign_lead_progress`, `campaign_sequence_steps`. None of these exist in hq-x and none should be reintroduced under a new name. The unit of analytics is the campaign within a motion, not a step within a campaign.

6. **ClickHouse parameterization, not interpolation.** Every `ch_query()` call uses `{name:Type}` placeholders + the `params` dict. SQL injection risk if you string-format values into the query.

7. **Postgres fallback stays correct on its own.** ClickHouse is an opt-in perf layer. The Postgres path must produce the same response shape and same `source` field, just slower. Don't gate features on ClickHouse availability.

8. **Don't port the OEX analytics router structurally.** Use it as a behavior reference for direct-mail and reliability endpoints. Everything else is built fresh against the GTM model.

9. **No frontend, no docs site.** Backend only. Frontend is a separate workstream ([`docs/gtm-model-pr-notes.md:151`](docs/gtm-model-pr-notes.md:151)).

10. **Migration discipline.** ClickHouse DDL is operator-applied; ship as `docs/clickhouse-schema.md` or `scripts/clickhouse/`, NOT as a numbered Postgres migration. hq-x Postgres migrations are sequenced (next is `0022_…`); only add one if you need a Postgres schema change. As of this directive, you don't.

---

## 6. File-by-file expected delta

| Phase | File | Action | Approx LOC |
|-------|------|--------|------------|
| 1 | `app/clickhouse.py` | Append `ch_query`, `ch_available` | +40 |
| 1 | `app/routers/voice_analytics.py` | Add ClickHouse branches to 5 endpoints | +60 |
| 1 | `tests/test_clickhouse_client.py` | New | +200 |
| 1 | `tests/test_voice_analytics_clickhouse.py` | New | +150 |
| 2 | `pyproject.toml` | Add rudder SDK | +1 |
| 2 | `app/config.py` | Add 2 settings | +6 |
| 2 | `app/rudderstack.py` | New | +80 |
| 2 | `app/services/analytics.py` | Wire `track()` call | +15 |
| 2 | `app/main.py` | Lifespan flush | +5 |
| 2 | `tests/test_rudderstack_client.py` | New | +120 |
| 2 | `tests/test_analytics_emit.py` | Extend | +60 |
| 3 | `docs/clickhouse-schema.md` | New | +120 |
| 3 | `app/services/analytics.py` | Wide-events write | +15 |
| 3 | `app/services/motion_analytics.py` | New | +250 |
| 3 | `app/services/direct_mail_analytics.py` | New (port from OEX) | +300 |
| 3 | `app/services/reliability_analytics.py` | New | +100 |
| 3 | `app/models/analytics.py` | New | +120 |
| 3 | `app/routers/analytics.py` | New | +200 |
| 3 | `app/main.py` | Mount router | +2 |
| 3 | `tests/test_motion_analytics_pure.py` | New | +200 |
| 3 | `tests/test_motion_analytics_db_fake.py` | New | +400 |
| 3 | `tests/test_motion_analytics_clickhouse.py` | New | +200 |
| 3 | `tests/test_reliability_analytics.py` | New | +150 |
| 3 | `tests/test_direct_mail_analytics.py` | New | +250 |

**Total: ~3,000 LOC across ~25 files. ~50% tests.**

---

## 7. Reference map (where to read in OEX)

These are reference-only. Don't copy structure; understand the behavior, then build against the GTM model.

| Concept | OEX file | What to take from it |
|---------|----------|---------------------|
| `ch_query` / `ch_available` | [`src/clickhouse.py:62-96`](file:///Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62) | Verbatim port (with hq-x SecretStr adapter) |
| ClickHouse fallback pattern | [`src/routers/voice_analytics.py:74-250`](file:///Users/benjamincrane/outbound-engine-x/src/routers/voice_analytics.py:74) | The "if available → CH else PG" idiom |
| Direct-mail funnel logic | [`src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) (search for `/direct-mail`) | Funnel mapping, daily bucket prefill, payload extraction |
| Reliability rollup | same file (`/reliability`) | Group-by + replay/error count logic |
| Channel→provider mapping | [`src/models/multi_channel.py`](file:///Users/benjamincrane/outbound-engine-x/src/models/multi_channel.py) `CHANNEL_BY_PROVIDER_SLUG` | Reference for normalization, but our `provider` is already first-class in `business.campaigns` so we don't need this map |
| Tests | [`tests/test_clickhouse_client.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py), [`tests/test_voice_analytics.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_voice_analytics.py) | Mock patterns for httpx + DB fakes |

**Do NOT take from OEX:**
- `_resolve_company_scope` — replaced by `require_org_context`.
- `campaign_sequence_steps`, `campaign_events`, `campaign_lead_progress` — concepts don't exist here.
- `/clients` org-rollup endpoint — replaced by motion rollup.
- `/sequence-steps` endpoint — concept doesn't exist here.
- `/multi-channel/{campaign_id}` endpoint — concept reframes onto motion rollup.
- `org_id` / `company_id` / `company_campaign_id` field names — use `organization_id` / `partner_id` / `campaign_id`.
- The 1,098-line monolithic router — split per feature group as described.

---

## 8. Risk register

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| RudderStack SDK pins on an old Python or has security issues | Medium | Verify on PyPI before adding. Pin a known-good version. If the canonical SDK is dead, fall back to a thin httpx HTTP client against the data plane. |
| ClickHouse `events` table not provisioned in prod when code ships | High if not coordinated | Ship the DDL doc with the PR. Coordinate with whoever runs the cluster (ops/Ben) before merging Phase 3. Until provisioned, motion rollup falls back to Postgres — should still work, just slower. |
| Cross-org leakage via the new endpoints | Medium | Hard-rule §5.2 — write the negative test first for each endpoint. |
| Postgres motion-rollup query is slow on real data | Medium | Add `EXPLAIN ANALYZE` to PR description for the largest seed dataset available. If >2s on realistic volume, add a Postgres index in a follow-up migration. ClickHouse availability solves it permanently. |
| Per-event-type tables (`call_events`) drift out of sync with wide `events` table | Low–Medium | The wide table is the single source of truth for motion analytics; per-type tables are queried only by their direct consumers (e.g., voice-specific cost breakdowns). Document this in `docs/clickhouse-schema.md`. |
| Six-tuple gets bypassed by some new emit site | Medium (over time) | Code review enforces it. Consider a lint rule (grep gate in CI) that flags direct `clickhouse.insert_row` calls outside `app/services/analytics.py`. |

---

## 9. PR template (copy into each PR description)

```markdown
## Summary
- Phase X of DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md.
- [bullet what shipped]
- [bullet what's deferred]

## Six-tuple integrity
- [ ] No new emit sites bypass `app/services/analytics.py::emit_event`.
- [ ] No new endpoint accepts `organization_id` from request body or query string.

## Cross-org leakage
- [ ] Negative test added for every new endpoint: org A auth + org B resource id → 404.

## Fallback behavior
- [ ] Without RUDDERSTACK_* env: every test still passes.
- [ ] Without CLICKHOUSE_* env: every test still passes; analytics endpoints serve from Postgres with `"source": "postgres"`.

## Test plan
- [ ] `pytest -q` green (target count: __).
- [ ] `ruff check` clean on every touched file.
- [ ] Manual smoke against a seeded org: motion rollup returns expected counts.
```

---

## 10. Definition of done (whole directive)

- All three phases merged to `main`.
- `pytest -q` green; total test count ≥ 393 (current 343 + ~50 new minimum).
- `ruff check` clean.
- `docs/clickhouse-schema.md` exists; cluster operator has applied the DDL (or has a ticket to do so).
- A short post-ship summary appended to `docs/gtm-model-pr-notes.md` (or its own `docs/analytics-buildout-pr-notes.md`) describing what shipped, what's deferred, and any caveats.
- Out-of-scope items from §2 are explicitly listed in the post-ship summary so the next agent has the inheritance context.

---

## 11. Questions to confirm with Ben before starting

1. **RudderStack SDK choice** — Python SDK from PyPI, or a custom httpx wrapper? (Default: SDK.)
2. **`anonymous_id` strategy** — `organization_id` is a fine default. Confirm we're not user-tracking yet.
3. **ClickHouse cluster ownership** — who runs the cluster, who applies the wide `events` DDL? (Coordinate before Phase 3 merge.)
4. **Direct-mail funnel test data** — is there a seeded test fixture that exercises the full Lob webhook lifecycle? If not, we add one in Phase 3.
5. **Phase ordering** — the directive sequences `Phase 1 → 2 → 3` because it's the lowest-risk path. If you'd rather front-load RudderStack (Phase 2 first), nothing breaks. ClickHouse query helper (Phase 1) must precede motion analytics (Phase 3).

---

**Author:** Drafted 2026-04-29 by the audit agent that produced [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) and [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md). All file:line citations were live at draft time; verify before relying on any specific reference.
