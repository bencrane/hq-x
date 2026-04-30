# Directive — hq-x analytics buildout: RudderStack + ClickHouse query layer + campaign analytics

**Target audience:** Implementation agent picking up the analytics workstream after the campaign-model PR ([#18](https://github.com/bencrane/hq-x/pull/18)), the rename PR ([#22](https://github.com/bencrane/hq-x/pull/22)), and the audit pair ([#16](https://github.com/bencrane/hq-x/pull/16)).

**Source repos:**
- hq-x (this repo): `/Users/benjamincrane/hq-x` — destination.
- outbound-engine-x (OEX): `/Users/benjamincrane/outbound-engine-x` — read-only reference for the analytics router.

**Required prereading (in order):**
1. [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) — what's already on the floor.
2. [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md) — feature decomposition + brand-axis fit. *Note: written before the rename; mentally substitute "motion → campaign" and "campaign → channel_campaign" when reading.*
3. [`docs/campaign-model.md`](docs/campaign-model.md) — the two-layer outreach model (current names). **Architectural anchor.** Note: this doc predates the `channel_campaign_steps` + provider-adapter work described in §1.1 below; the hierarchy in this directive is the post-steps state.
4. [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) — what the rename changed (#22). Read this if you've previously worked off the `gtm_motions` names; otherwise skim.
5. [`docs/campaign-model-pr-notes.md`](docs/campaign-model-pr-notes.md) — original two-layer model post-ship notes (now flagged outdated; useful for the migration story).
6. [`docs/tenancy-model.md`](docs/tenancy-model.md) — `organizations` / `organization_memberships` / two-axis roles. Auth model for new endpoints.
7. [`app/services/analytics.py`](app/services/analytics.py) — current state of the analytics emit path. Read every line.
8. [`app/services/channel_campaigns.py`](app/services/channel_campaigns.py) — `get_channel_campaign_context()` is what `emit_event` calls to resolve the analytics tuple. Skim.
9. **(When it lands)** the post-ship notes for `channel_campaign_steps` + the Lob adapter consolidation. Phase 3 of this directive is gated on that work shipping first; Phases 1 and 2 can run concurrently with it. See §1.1.

---

## 1. Why

The audit exit position was: ClickHouse writes work, the query helper is deferred, the OEX multi-channel analytics router would be a 1,098-line port but most of it is moot because hq-x had no multi-channel campaign primitive.

[#18](https://github.com/bencrane/hq-x/pull/18) shipped the primitive (originally `gtm_motions` + channel-typed `campaigns`), and [#22](https://github.com/bencrane/hq-x/pull/22) renamed it to its current shape: `business.campaigns` (umbrella) + `business.channel_campaigns` (channel-typed execution unit). A concurrent workstream (§1.1) is now adding `business.channel_campaign_steps` as a third layer plus a provider-adapter pattern. The canonical seven-tuple `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider)` will be enforced at every emit site by [`app/services/analytics.py::emit_event`](app/services/analytics.py) once the steps work lands.

That changes the calculus:

- The shape of "multi-channel analytics" is well-defined (campaign → child channel_campaigns → ordered steps, each step 1:1 with an external provider object) and matches the data model directly.
- The RudderStack write side is a deliberate no-op stub — see the docstring of [`app/services/analytics.py`](app/services/analytics.py). The choke point exists; only the client is missing.
- ClickHouse writes are already going through `emit_event` → `app.clickhouse.insert_row`. A query helper just adds the read path.

Work to do: (a) wire the RudderStack client into the existing emit choke point, (b) add the ClickHouse query helper, (c) build the analytics router on top of (b) using the campaign model directly, instead of porting the OEX router structure verbatim. **Don't port the OEX router as-is** — its `step_order` / `sequence_step` / `campaign_lead_progress` model doesn't exist here in the same shape and shouldn't be reintroduced. Use OEX as a behavior reference only.

### 1.1 Concurrent workstream: `channel_campaign_steps` + provider adapters

A separate workstream (not this directive) is introducing:

- **`business.channel_campaign_steps`** — a first-class child of `channel_campaigns`. Every channel_campaign has 1..N ordered steps. Each step is 1:1 with an external provider object (e.g., one Lob campaign per direct-mail step; one EmailBison sequence step per email step; one Vapi call batch per voice step).
- **Per-provider adapters** under `app/providers/<provider>/adapter.py`. The first to land is `app/providers/lob/adapter.py`. The adapter is the single entry point for activating a step against the provider's API and tagging the provider-side object with the full hq-x hierarchy metadata (organization_id / campaign_id / channel_campaign_id / channel_campaign_step_id).
- **Webhook projector rebuild** — the Lob webhook handler is rebuilt to project events against the new schema, routing each inbound webhook to its `(organization_id, campaign_id, channel_campaign_id, channel_campaign_step_id, piece)` and writing `direct_mail_pieces` rows linked back to the step.
- **Pattern repeated** by EmailBison, Vapi, and other future providers.

**Net effect for analytics:** every direct-mail piece and every webhook event is traceable up the full hierarchy. The seven-tuple becomes resolvable from any leaf event. The provider adapter is the *only* call site for `emit_event()` in the direct-mail path — webhook handlers and routers don't emit events directly; the adapter does.

**Sequencing:**

- Phase 1 (ClickHouse query helper) — independent of steps. Can ship now.
- Phase 2 (RudderStack) — depends on the final `emit_event()` signature. If steps work has landed, write Phase 2 against the step-aware signature directly. If not, it's still safe to ship; `emit_event()` accepts an optional `channel_campaign_step_id` and Phase 2 just propagates it through. **Do not ship a Phase 2 that hardcodes the pre-steps signature** — that creates churn when steps lands.
- Phase 3 (campaign analytics) — **gated on the steps + Lob adapter work landing first.** The wide ClickHouse `events` table includes a `channel_campaign_step_id` column, the rollup endpoints expose step-level breakdowns, and the direct-mail funnel reads pieces via `direct_mail_pieces.channel_campaign_step_id` not just the channel_campaign id. Don't start Phase 3 before that schema exists.

---

## 2. Scope

### In scope (this directive)

1. **RudderStack write integration** — turn the `emit_event()` shim into a real `analytics.track()` call. Config, client, retry, test seam.
2. **ClickHouse query helper** — port `ch_query()` + `ch_available()` from OEX, adapt to hq-x config conventions. Wire ClickHouse-preferred + Postgres-fallback into `voice_analytics.py`.
3. **Campaign analytics router** — new `/api/v1/analytics/*` endpoints scoped to organizations. Four feature groups:
   - **Campaign rollup** — given a `campaign_id` (umbrella), return per-channel, per-channel_campaign, and per-step breakdowns of events, outcomes, costs.
   - **Channel-campaign analytics** — per-channel_campaign drilldown including a per-step breakdown. Same shape across channels (voice/sms/email/direct_mail), with channel-specific extensions where the data warrants.
   - **Step analytics** — per-step drilldown. Each step is 1:1 with an external provider object, so step-level analytics has clean equivalence to "this Lob campaign", "this EmailBison sequence step", etc.
   - **Reliability + direct-mail funnels** — port verbatim from OEX with field renames; the underlying `webhook_events` and `direct_mail_pieces` tables match (with `direct_mail_pieces` now linking to `channel_campaign_step_id` rather than directly to `channel_campaign_id`).
4. **Event schema in ClickHouse** — a single `events` table the `emit_event()` helper writes to (in addition to event-typed tables). Becomes the substrate for cross-channel campaign analytics. Carries the full seven-tuple including `channel_campaign_step_id`.
5. **Tests** — port + extend OEX's analytics test pattern. Pure-function tests for aggregation logic; service-level tests with the in-memory `get_db_connection` fake; ClickHouse client tests with mocked httpx; RudderStack tests with mocked client.

### Out of scope (defer; flag in PR notes)

- **Building `channel_campaign_steps` or the provider adapters.** This directive consumes them; it does not build them. See §1.1.
- **Per-lead state across channels** — explicitly out of scope per [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md). Don't build `lead_progress` analogues. Steps are an execution-batch concept, not a per-lead concept.
- **Cross-campaign analytics rollup** (multiple umbrella campaigns at once) — defer.
- **Frontend** — backend only.
- **Tightening `direct_mail_pieces.channel_campaign_step_id` / `channel_campaign_id` / `campaign_id` to NOT NULL** — wait for an orphan-free real-data backfill; out of this directive's scope.
- **OEX's per-lead `/api/analytics/campaigns/{id}/sequence-steps` analytics** — different concept from hq-x steps (OEX's was a per-lead funnel through email sequence steps; hq-x's steps are execution batches each tied to an external provider object). Don't port. The step-analytics endpoint described in §4 Phase 3 is a fresh design over hq-x's `channel_campaign_steps` table.
- **OEX's `/api/analytics/clients` org-rollup** — replaced by campaign rollup in the new model. Don't port.
- **Message sync health endpoint** — depends on email/linkedin providers that aren't wired in hq-x yet; defer until they are.

---

## 3. Architecture decisions you must make on day 0

### 3.1 RudderStack: server-side write key + Python SDK or HTTP?

The Python SDK exists and is straightforward; use it. Don't roll an HTTP client unless the SDK is unmaintained — verify on PyPI before starting. Configure via:

- `RUDDERSTACK_WRITE_KEY` (SecretStr, optional)
- `RUDDERSTACK_DATA_PLANE_URL` (str, optional)
- Both unset → silent skip, identical pattern to the ClickHouse client. **The fire-and-forget contract in [`app/services/analytics.py`](app/services/analytics.py) is non-negotiable: an unconfigured analytics layer must not break production paths.**

The client is a singleton; init it lazily on first `track()` call so importing `app.services.analytics` in tests doesn't require the SDK to be configured.

### 3.2 ClickHouse `events` table — one wide table or per-event-type?

OEX did per-event-type (`call_events`). hq-x's `emit_event()` already accepts `clickhouse_table` and writes to whatever you point it at, so per-type is supported.

**Recommended:** Add **one wide `events` table** alongside the existing typed tables. Schema:

```sql
events (
  event_id                   UUID,
  event_name                 String,
  occurred_at                DateTime64(3),
  organization_id            UUID,
  brand_id                   UUID,
  campaign_id                UUID,        -- umbrella
  channel_campaign_id        UUID,        -- channel-typed execution unit
  channel_campaign_step_id   Nullable(UUID),  -- ordered step under a channel_campaign; null only for events that legitimately have no step (e.g. campaign-level lifecycle events)
  channel                    LowCardinality(String),
  provider                   LowCardinality(String),
  properties                 String       -- JSON blob of event-specific props
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (organization_id, campaign_id, channel_campaign_id, channel_campaign_step_id, occurred_at, event_id)
```

Reasoning: campaign rollup queries are the primary read pattern — `WHERE campaign_id = ? AND occurred_at BETWEEN ?` group by `channel, provider, channel_campaign_id, channel_campaign_step_id`. A single wide table with that ORDER BY makes those near-instant at every granularity (campaign / channel_campaign / step). Per-event-type tables (e.g. `call_events`) stay for backward compat and for queries that need typed columns; the wide `events` table becomes the cross-channel substrate.

`channel_campaign_step_id` is `Nullable(UUID)` because some events legitimately have no step — e.g., a `campaign.activated` lifecycle event sits at the umbrella level. Step-bound events (every direct-mail piece event, every call, every SMS, every email send) MUST carry the step id; this is enforced at the application layer in `emit_event()`.

**Don't** ship the `CREATE TABLE` DDL in a Postgres migration — ClickHouse is provisioned out-of-band. Ship it as `docs/clickhouse-schema.md` or `scripts/clickhouse/events.sql`, and document that an operator runs it on the cluster.

### 3.3 Auth model for new endpoints

All new analytics endpoints are organization-scoped. Use `require_org_context` ([`app/auth/roles.py`](app/auth/roles.py)). Read the `active_organization_id` from `UserContext`. Every analytics query must filter by `organization_id` from the auth context — **never accept it as a query param**. Platform operators drive cross-org by setting `X-Organization-Id`, same pattern as `/api/v1/campaigns`.

### 3.4 Postgres fallback policy

When ClickHouse is unconfigured or `ch_available()` returns False:

- **Voice analytics endpoints** (already exist) — fall back to Postgres `call_logs`. Already implemented; preserve.
- **Campaign rollup** — Postgres can answer the question by joining `call_logs` / `sms_messages` / `direct_mail_pieces` through the FK chain to roll up under the umbrella campaign. Implement the fallback. It's slower but correct.
- **Step rollup** — same join chain, terminating at the step granularity.
- **Reliability and direct-mail funnels** — these never touched ClickHouse in OEX. Postgres-only, no fallback path needed.

The response payload always includes `"source": "clickhouse"` or `"source": "postgres"` so consumers can see which path served them.

**FK-chain reference for Postgres queries (post-steps schema):**

| Source row | Path to step | Path to channel_campaign | Path to campaign (umbrella) |
|---|---|---|---|
| `direct_mail_pieces` | `direct_mail_pieces.channel_campaign_step_id` (direct) | via `channel_campaign_steps` (or denormalized `channel_campaign_id`, if kept) | via `channel_campaigns.campaign_id` |
| `call_logs` | (via `channel_campaign_steps`, once voice steps are wired — see workstream §1.1) | `call_logs.channel_campaign_id` (direct) | via `channel_campaigns.campaign_id` |
| `sms_messages` | (same — TBD when SMS steps are wired) | `sms_messages.channel_campaign_id` (direct) | via `channel_campaigns.campaign_id` |

Verify these column names against the actual `channel_campaign_steps` migration when it lands; the table above reflects the design, not the as-built schema. If voice/SMS rows don't yet carry a `channel_campaign_step_id` at the time you build Phase 3, treat their step granularity as "step unknown" and surface a single synthetic step per channel_campaign in the rollup, with a note in the response and an issue filed for the upstream team to wire it.

---

## 4. Phased implementation

Ship as **three** PRs, in order. Each is independently reviewable and deployable.

### Phase 1 — ClickHouse query helper + voice analytics fallback

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
  - OEX `org_id` → hq-x: filter by `brand_id` (voice analytics is brand-scoped)
  - OEX `company_campaign_id` → hq-x: `channel_campaign_id`
  - Outcome enum: OEX `'transferred'` → hq-x `'qualified_transfer'`
- Keep the Postgres fallback **byte-identical** to current behavior — it must remain correct on its own. ClickHouse is purely an opt-in perf layer.

**Acceptance:**
- `pytest tests/test_clickhouse_client.py tests/test_voice_analytics_clickhouse.py` green.
- Existing voice analytics tests pass unchanged (the new branch is gated by `ch_available()` which returns False without config).
- No new env vars are required to ship. Without `CLICKHOUSE_*` set, every request still goes to Postgres and the existing tests pass.

### Phase 2 — RudderStack write integration

**PR title:** "RudderStack: real client behind emit_event"

**Files:**
- `pyproject.toml` — add the canonical RudderStack Python SDK. Verify on PyPI before adding.
- `app/config.py` — add `RUDDERSTACK_WRITE_KEY: SecretStr | None` and `RUDDERSTACK_DATA_PLANE_URL: str | None`.
- `app/rudderstack.py` — new module. Mirrors `app/clickhouse.py` shape:
  - `_is_configured()` — both env vars set.
  - `_get_client()` — lazy singleton init.
  - `track(event_name, *, anonymous_id, properties)` — fire-and-forget. Never raises.
  - `flush()` — call on app shutdown (FastAPI lifespan).
- `app/services/analytics.py` — replace the no-op shim with a real `rudderstack.track()` call. The seven-tuple goes into `properties`; `anonymous_id` is the `organization_id` cast to str (a stable identifier per tenant; users aren't the entity here — orgs are).
- `app/main.py` — register the rudderstack flush in the lifespan shutdown handler.
- `tests/test_rudderstack_client.py` — mock the SDK; verify `track` is called with the expected payload and that an unconfigured client is a no-op.
- `tests/test_analytics_emit.py` — extend to assert RudderStack `track` is called when the env is configured. Use a fake client injected via dependency override.

**Implementation notes:**
- The SDK has a background thread that batches sends. Init it once. The `flush()` on shutdown is important — without it, in-flight events are dropped when the container restarts.
- **Do not** put the RudderStack write before logging — log first, then write. Order matters: if RudderStack init throws (it shouldn't, but…), logs still fire.
- Anonymous-id strategy: use `organization_id`. Identify-call later if/when we wire user-level events; out of scope here.
- `event_name`, `occurred_at`, seven-tuple all go in the `properties` dict. RudderStack `track`'s `event` argument is `event_name`.
- **Signature handling vs the §1.1 workstream:** if `channel_campaign_steps` has landed by the time you start Phase 2, write `emit_event(channel_campaign_step_id=...)` as the canonical primary; resolve the rest from the step. If it hasn't, accept BOTH `channel_campaign_id` (current) and an optional `channel_campaign_step_id` (forward-compat) so callers can adopt the step id incrementally without a second signature change. Do not write Phase 2 against only the current pre-step signature — that creates churn.

**Acceptance:**
- Without `RUDDERSTACK_*` env: every existing test passes; `emit_event` still logs + still writes to ClickHouse if configured; RudderStack code is a no-op.
- With env: a `track()` call is made per `emit_event()`. Verified by mock.
- App shutdown flushes the queue. Verified by mock + lifespan test.

### Phase 3 — Campaign analytics router

**Gate:** Do not start Phase 3 until the §1.1 workstream has merged: `channel_campaign_steps` table exists, the Lob adapter at `app/providers/lob/adapter.py` is the single emit site for direct-mail piece events, and `direct_mail_pieces` rows carry a `channel_campaign_step_id`. Confirm by reading the post-ship notes for that workstream and verifying the schema with `\d business.channel_campaign_steps`.

**PR title:** "Campaign analytics router + step drilldown + reliability + direct-mail funnels"

This is the largest phase. Build it incrementally — each endpoint is a separate commit if helpful for review.

#### 3.1 Wide `events` table substrate

- `docs/clickhouse-schema.md` — document the wide `events` table DDL (see §3.2 above) and the partitioning/ordering rationale.
- `app/services/analytics.py::emit_event` — when the caller passes `clickhouse_table`, ALSO insert a row into the wide `events` table. The wide row uses the seven-tuple, `event_name`, `occurred_at`, and `properties` JSON-serialized into the `properties` column. Both writes are fire-and-forget.

The wide `events` table is what campaign-level analytics queries join against. Per-event-type tables (`call_events`, future `direct_mail_piece_events_ch`, etc.) stay for typed-column queries.

`emit_event()` enforces that step-bound event names (the direct-mail piece events, call events, sms events, email send events) carry a non-null `channel_campaign_step_id`. Maintain a small allowlist of "step-optional" event names (e.g. `campaign.activated`, `channel_campaign.archived`) and reject step-bound events without a step id.

#### 3.2 Campaign rollup endpoint

`GET /api/v1/analytics/campaigns/{campaign_id}/summary?from=&to=`

Note: `{campaign_id}` here is the **umbrella** campaign (`business.campaigns`), not a channel_campaign.

Returns:
```
{
  "campaign": { "id", "name", "status", "start_date", "brand_id", "organization_id" },
  "channel_campaigns": [
    {
      "channel_campaign_id", "name", "channel", "provider", "status", "scheduled_send_at",
      "events_total", "outcomes": { "succeeded": N, "failed": N, "skipped": N },
      "cost_total_cents": N,
      "steps": [
        {
          "channel_campaign_step_id", "step_order", "name",
          "events_total", "outcomes": { "succeeded": N, "failed": N, "skipped": N },
          "cost_total_cents": N
        }
      ]
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

Auth: `require_org_context`. Filter by `organization_id` from auth + the requested `campaign_id`. Verify the campaign belongs to the auth's org before querying — return 404 if not (don't leak existence).

ClickHouse path: one query against the wide `events` table grouping by `(channel, provider, channel_campaign_id, channel_campaign_step_id)` plus an `outcome` column derived from `event_name`. Use ClickHouse-native parameterization for `campaign_id`, `from`, `to`. Roll up step rows into their parent channel_campaign in application code; emit both granularities in the response.

Postgres fallback: see top-level §3.4 (fallback policy + FK-chain table). Join chain: `direct_mail_pieces` and (when wired) voice/SMS rows → `channel_campaign_steps` → `channel_campaigns` → `campaigns`. Aggregate in SQL or in Python (your call — measure first).

#### 3.3 Channel-campaign analytics endpoint

`GET /api/v1/analytics/channel-campaigns/{channel_campaign_id}/summary?from=&to=`

Same shape as campaign rollup but scoped to one channel_campaign. Includes a `steps` array (per-step breakdown). Channel-specific extensions:
- voice: include `transfer_rate`, `avg_duration_seconds`, cost breakdown (transport / stt / llm / tts / vapi).
- sms: include `delivery_rate`, `opt_out_count`.
- direct_mail: include the funnel (queued → processed → in_transit → delivered → returned/failed) at the channel_campaign level AND repeated per-step (each step is 1:1 with a Lob campaign object, so per-step funnels are clean).
- email: scaffold — return zeros for now since emailbison isn't wired ([`docs/campaign-model.md`](docs/campaign-model.md) §"Channel / provider matrix").

Auth: same as campaign rollup. Verify the channel_campaign's `organization_id` matches auth.

#### 3.4 Step analytics endpoint

`GET /api/v1/analytics/channel-campaign-steps/{channel_campaign_step_id}/summary?from=&to=`

Per-step drilldown. Returns the step's metadata (parent channel_campaign, parent campaign, provider, the external provider object id — e.g. the Lob campaign id), plus events / outcomes / costs and the channel-specific funnel (for direct_mail) or detail rows (for voice — top-N call outcomes; for sms — delivery breakdown).

Why this exists as a separate endpoint and not just a sub-query of channel-campaign: each step is 1:1 with an external provider object. Operators reading this analytics likely already have the external object id from the provider's UI; surface it in the response so they can cross-reference Lob's dashboard against ours without ambiguity.

Auth: same. Verify the step's `organization_id` matches auth (resolved through the FK chain).

#### 3.5 Direct-mail funnel endpoint

`GET /api/v1/analytics/direct-mail?brand_id=&channel_campaign_id=&channel_campaign_step_id=&from=&to=`

Port from OEX [`/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) (the `/direct-mail` endpoint, ~350 LOC). Field renames:
- OEX `org_id`/`company_id` → hq-x `organization_id`/`brand_id`/`partner_id`
- OEX `company_campaign_id` (channel-typed) → hq-x `channel_campaign_id`
- New filter: optional `channel_campaign_step_id` — drills the funnel down to a single Lob campaign object's worth of pieces.
- Filter scope: auth-context-derived `organization_id`, never request param. (See top-level §3.3.)

Keep OEX's safety gates: max 93-day window, `max_rows=20000` cap, paginated `failure_reason_breakdown` and `daily_trends`.

Tables involved (already present in hq-x): `direct_mail_pieces`, `webhook_events` (provider_slug='lob'). Postgres-only; no ClickHouse path needed.

#### 3.6 Reliability endpoint

`GET /api/v1/analytics/reliability?from=&to=`

Port from OEX. Group `webhook_events` by `provider_slug`, count replays, sum `replay_count`, count errors. Filter by `organization_id` from auth.

Cleanest port in the entire OEX router. ~120 LOC.

#### 3.7 Models

`app/models/analytics.py` (new file). Pydantic response shapes for the five endpoint responses above. Don't try to mirror OEX's models 1:1 — design fresh against the new payload shape.

#### 3.8 Router wiring

`app/routers/analytics.py` (new file). Mount at `/api/v1/analytics` in `app/main.py`. Don't reuse OEX's prefix (`/api/analytics`) — hq-x is on `/api/v1`.

#### 3.9 Tests

- `tests/test_campaign_analytics_pure.py` — pure aggregation functions (group/sum/derive at all three granularities: campaign / channel_campaign / step). Reference pattern: `tests/test_campaigns_pure.py`.
- `tests/test_campaign_analytics_db_fake.py` — service-level with `get_db_connection` in-memory fake. Reference pattern: `tests/test_campaigns_services_db_fake.py`. Cover the three-level join (`pieces → step → channel_campaign → campaign`).
- `tests/test_campaign_analytics_clickhouse.py` — ClickHouse path with mocked `ch_query`. Verify the SQL parameters (including `channel_campaign_step_id`) and the result-shape mapping.
- `tests/test_step_analytics.py` — step drilldown (both ClickHouse + Postgres paths).
- `tests/test_reliability_analytics.py` — Postgres-only.
- `tests/test_direct_mail_analytics.py` — Postgres-only; reuse OEX test patterns where logic is identical. Cover the new optional `channel_campaign_step_id` filter.

Test count target: substantial new coverage across the phase. Full suite must remain green.

**Acceptance for Phase 3:**
- All five endpoints work end-to-end against a seeded test DB.
- Campaign rollup serves ClickHouse when configured (verified by mock), Postgres otherwise.
- Step rollup numbers reconcile with the parent channel_campaign rollup (sum of step events == channel_campaign total). Add a property test for this.
- Direct-mail funnel matches OEX behavior on a port-equivalence test (run identical inputs through both, compare outputs — if both repos are accessible during dev).
- Cross-org leakage tests: a request with org A's auth, asking about org B's campaign / channel_campaign / step, returns 404. **Add this test for every new endpoint.** This is the single highest-value safety check.
- ruff clean, full suite green.

---

## 5. Hard rules

1. **Seven-tuple is sacred.** Every event written via `emit_event()` carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel, provider)`. Don't add a code path that bypasses `emit_event()`. Don't accept `organization_id` as a request body field on any analytics endpoint — it comes from auth context.

2. **Org isolation tested per endpoint.** Every new endpoint gets a "user from org A asking about org B's resource → 404" test. No exceptions.

3. **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, `track()` never raise into the caller. They log on failure. Adding a `raise` to any of these is a bug.

4. **No silent assignment.** If a query needs a `campaign_id` and the campaign doesn't belong to the caller's org, return 404. Don't fall back to "any campaign the user can see." Don't auto-pick.

5. **OEX's per-lead step model is banned; hq-x's first-class steps are mandatory.** Don't reintroduce OEX's `step_order` / `step_status` / `campaign_lead_progress` / `campaign_sequence_steps` per-lead funnel concept under any name. DO use hq-x's first-class `channel_campaign_steps` (each step = one external provider object, not a per-lead state). The two are different abstractions; conflating them will produce a confused data model.

6. **ClickHouse parameterization, not interpolation.** Every `ch_query()` call uses `{name:Type}` placeholders + the `params` dict. SQL injection risk if you string-format values into the query.

7. **Postgres fallback stays correct on its own.** ClickHouse is an opt-in perf layer. The Postgres path must produce the same response shape and same `source` field, just slower. Don't gate features on ClickHouse availability.

8. **Don't port the OEX analytics router structurally.** Use it as a behavior reference for direct-mail and reliability endpoints. Everything else is built fresh against the campaign model.

9. **Mind the umbrella-vs-execution-vs-step naming.** `campaign_id` always = umbrella (`business.campaigns`). `channel_campaign_id` always = channel-typed execution unit (`business.channel_campaigns`). `channel_campaign_step_id` always = ordered step (`business.channel_campaign_steps`). Mixing any pair of these will silently produce wrong rollups. When in doubt, re-read [`docs/campaign-model.md`](docs/campaign-model.md) §"Hierarchy".

10. **Provider adapters are the emit chokepoint.** For each provider (Lob first, EmailBison/Vapi/etc. to follow the pattern), `emit_event()` is called from the adapter at `app/providers/<provider>/adapter.py` — not from webhook handlers, not from routers, not scattered across services. This is enforced upstream by the §1.1 workstream; the analytics directive's job is to assume that chokepoint exists and not undermine it by adding emit sites elsewhere. If you find yourself needing to emit from outside an adapter (e.g., a campaign lifecycle event like `campaign.activated`), fine — but use the same `emit_event()` helper, with a step-optional event name from the §3.1 allowlist.

11. **No frontend, no docs site.** Backend only. Frontend is a separate workstream.

12. **Migration discipline.** ClickHouse DDL is operator-applied; ship as `docs/clickhouse-schema.md` or `scripts/clickhouse/`, NOT as a numbered Postgres migration. hq-x Postgres migrations are sequenced; only add one if you need a Postgres schema change. As of this directive, you don't.

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
| 3 | `app/services/campaign_analytics.py` | New (campaign + channel_campaign rollups) | +300 |
| 3 | `app/services/step_analytics.py` | New (step drilldown) | +180 |
| 3 | `app/services/direct_mail_analytics.py` | New (port from OEX, with step filter) | +320 |
| 3 | `app/services/reliability_analytics.py` | New | +100 |
| 3 | `app/models/analytics.py` | New | +140 |
| 3 | `app/routers/analytics.py` | New | +240 |
| 3 | `app/main.py` | Mount router | +2 |
| 3 | `tests/test_campaign_analytics_pure.py` | New | +220 |
| 3 | `tests/test_campaign_analytics_db_fake.py` | New | +450 |
| 3 | `tests/test_campaign_analytics_clickhouse.py` | New | +220 |
| 3 | `tests/test_step_analytics.py` | New | +200 |
| 3 | `tests/test_reliability_analytics.py` | New | +150 |
| 3 | `tests/test_direct_mail_analytics.py` | New | +250 |

**Total: ~3,000 LOC across ~25 files. ~50% tests.** These are sizing estimates; actual deltas will vary.

---

## 7. Reference map (where to read in OEX)

These are reference-only. Don't copy structure; understand the behavior, then build against the campaign model.

| Concept | OEX file | What to take from it |
|---------|----------|---------------------|
| `ch_query` / `ch_available` | [`src/clickhouse.py:62-96`](file:///Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62) | Verbatim port (with hq-x SecretStr adapter) |
| ClickHouse fallback pattern | [`src/routers/voice_analytics.py:74-250`](file:///Users/benjamincrane/outbound-engine-x/src/routers/voice_analytics.py:74) | The "if available → CH else PG" idiom |
| Direct-mail funnel logic | [`src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) (search for `/direct-mail`) | Funnel mapping, daily bucket prefill, payload extraction |
| Reliability rollup | same file (`/reliability`) | Group-by + replay/error count logic |
| Channel→provider mapping | [`src/models/multi_channel.py`](file:///Users/benjamincrane/outbound-engine-x/src/models/multi_channel.py) `CHANNEL_BY_PROVIDER_SLUG` | Reference for normalization, but our `provider` is already first-class on `business.channel_campaigns` so we don't need this map |
| Tests | [`tests/test_clickhouse_client.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py), [`tests/test_voice_analytics.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_voice_analytics.py) | Mock patterns for httpx + DB fakes |

**Do NOT take from OEX:**
- `_resolve_company_scope` — replaced by `require_org_context`.
- `campaign_sequence_steps`, `campaign_events`, `campaign_lead_progress` — OEX's per-lead step model. Different concept from hq-x's `channel_campaign_steps` (execution-batch with 1:1 external-object mapping). Don't conflate.
- `/clients` org-rollup endpoint — replaced by campaign rollup.
- `/sequence-steps` endpoint — different concept; the hq-x step-analytics endpoint at `/api/v1/analytics/channel-campaign-steps/{id}/summary` is a fresh design.
- `/multi-channel/{campaign_id}` endpoint — concept reframes onto campaign rollup.
- `org_id` / `company_id` / `company_campaign_id` field names — use `organization_id` / `brand_id` / `partner_id` / `channel_campaign_id` / `channel_campaign_step_id` per the hq-x model.
- The 1,098-line monolithic router — split per feature group as described.

---

## 8. Risk register

| Risk | Mitigation |
|------|-----------|
| RudderStack SDK pins on an old Python or has security issues | Verify on PyPI before adding. Pin a known-good version. If the canonical SDK is dead, fall back to a thin httpx HTTP client against the data plane. |
| ClickHouse `events` table not provisioned in prod when code ships | Ship the DDL doc with the PR. Coordinate with whoever runs the cluster (ops/Ben) before merging Phase 3. Until provisioned, campaign rollup falls back to Postgres — should still work, just slower. |
| Cross-org leakage via the new endpoints | Hard-rule §5.2 — write the negative test first for each endpoint. |
| Postgres campaign-rollup query is slow on real data | Add `EXPLAIN ANALYZE` to PR description for the largest seed dataset available. If too slow on realistic volume, add a Postgres index in a follow-up migration. ClickHouse availability solves it permanently. |
| Per-event-type tables (`call_events`) drift out of sync with wide `events` table | The wide table is the single source of truth for campaign analytics; per-type tables are queried only by their direct consumers (e.g., voice-specific cost breakdowns). Document this in `docs/clickhouse-schema.md`. |
| Seven-tuple gets bypassed by some new emit site | Code review enforces it. Consider a lint rule (grep gate in CI) that flags direct `clickhouse.insert_row` calls outside `app/services/analytics.py` AND direct `emit_event` calls outside `app/providers/*/adapter.py` and `app/services/{campaigns,channel_campaigns}.py` (the lifecycle-event sites). |
| Confusing umbrella `campaign_id` vs execution `channel_campaign_id` vs step `channel_campaign_step_id` | Hard-rule §5.9. When writing SQL, name your parameters explicitly (`umbrella_campaign_id`, `exec_channel_campaign_id`, `step_id`) until the naming is reflexive. Test fixtures should use distinguishable UUIDs at each layer (`00…0c1`, `00…0cc1`, `00…0ccc1`) so a swapped argument fails loudly. |
| §1.1 workstream slips or lands with a different schema than this directive assumes | Phase 3 is gated on the workstream merging. Re-read its post-ship notes before starting Phase 3 and reconcile any column-name or relationship deltas against §3.4's FK-chain table and the wide `events` schema in §3.2. If voice/SMS rows still don't carry `channel_campaign_step_id` at Phase 3 time, use the synthetic-step fallback from §3.4. |
| Step-analytics rollup numbers don't reconcile to channel_campaign totals | Add a property test: `sum(step.events_total for step in cc.steps) == cc.events_total` for every channel_campaign in every test fixture. A drift here means an event was written without the right step id. |

---

## 9. PR template (copy into each PR description)

```markdown
## Summary
- Phase X of DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md.
- [bullet what shipped]
- [bullet what's deferred]

## Seven-tuple integrity
- [ ] No new emit sites bypass `app/services/analytics.py::emit_event`.
- [ ] No new endpoint accepts `organization_id` from request body or query string.
- [ ] `campaign_id` (umbrella), `channel_campaign_id` (execution), and `channel_campaign_step_id` (step) are not confused in any query, response shape, or test fixture.
- [ ] Step-bound event names always carry a non-null `channel_campaign_step_id`; only allowlisted lifecycle event names omit it.

## Cross-org leakage
- [ ] Negative test added for every new endpoint: org A auth + org B resource id → 404.

## Fallback behavior
- [ ] Without RUDDERSTACK_* env: every test still passes.
- [ ] Without CLICKHOUSE_* env: every test still passes; analytics endpoints serve from Postgres with `"source": "postgres"`.

## Test plan
- [ ] `pytest -q` green.
- [ ] `ruff check` clean on every touched file.
- [ ] Manual smoke against a seeded org: campaign rollup returns expected counts.
```

---

## 10. Definition of done (whole directive)

- All three phases merged to `main`.
- `pytest -q` green; substantial new test coverage across all three phases.
- `ruff check` clean.
- `docs/clickhouse-schema.md` exists; cluster operator has applied the DDL (or has a ticket to do so).
- A short post-ship summary at `docs/analytics-buildout-pr-notes.md` describing what shipped, what's deferred, and any caveats.
- Out-of-scope items from §2 are explicitly listed in the post-ship summary so the next agent has the inheritance context.

---

## 11. Questions to confirm with Ben before starting

1. **RudderStack SDK choice** — Python SDK from PyPI, or a custom httpx wrapper? (Default: SDK.)
2. **`anonymous_id` strategy** — `organization_id` is a fine default. Confirm we're not user-tracking yet.
3. **ClickHouse cluster ownership** — who runs the cluster, who applies the wide `events` DDL? (Coordinate before Phase 3 merge.)
4. **Direct-mail funnel test data** — is there a seeded test fixture that exercises the full Lob webhook lifecycle? If not, we add one in Phase 3.
5. **§1.1 workstream timing** — when does `channel_campaign_steps` + the Lob adapter merge? Phase 3 is gated on it; Phases 1 and 2 are not. Confirm the ordering before kickoff.
6. **Voice/SMS step wiring** — at the time Phase 3 starts, will `call_logs` and `sms_messages` carry `channel_campaign_step_id`, or only `direct_mail_pieces`? If only direct-mail, Phase 3 ships with the synthetic-step fallback (§3.4) for voice/SMS rollups, and a follow-up PR upgrades them once the upstream wiring lands.
7. **Phase ordering** — the directive sequences `Phase 1 → 2 → 3` because it's the lowest-risk path. If you'd rather front-load RudderStack (Phase 2 first), nothing breaks. ClickHouse query helper (Phase 1) must precede campaign analytics (Phase 3).

---

**Author:** Drafted by the audit agent that produced [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) and [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md). Updated post-rename ([#22](https://github.com/bencrane/hq-x/pull/22)) to use the current `campaigns` / `channel_campaigns` ontology, and again to incorporate the concurrent `channel_campaign_steps` + provider-adapter workstream described in §1.1. All file:line citations were live at draft time; verify before relying on any specific reference.
