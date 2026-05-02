# Directive — hq-x analytics buildout: RudderStack + ClickHouse query layer + campaign analytics

**Target audience:** Implementation agent picking up the analytics workstream after the campaigns hierarchy is fully assembled (#18 → #22 → #28 → #29) and the audit pair ([#16](https://github.com/bencrane/hq-x/pull/16)).

**Source repos:**
- hq-x (this repo): `/Users/benjamincrane/hq-x` — destination.
- outbound-engine-x (OEX): `/Users/benjamincrane/outbound-engine-x` — read-only reference for the analytics router.

**Required prereading (in order):**
1. [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) — **canonical reference** for the campaigns hierarchy as it stands today: orgs → brands → campaigns → channel_campaigns → channel_campaign_steps → step_recipients/artifacts, plus the recipient identity layer. **Read this front-to-back before doing anything else.**
2. [`docs/campaign-model.md`](docs/campaign-model.md) — conceptual model + REST surface companion.
3. [`docs/lob-integration.md`](docs/lob-integration.md) — the direct-mail pipeline in depth (adapter, webhook projector, two-phase lifecycle, membership state machine).
4. [`docs/tenancy-model.md`](docs/tenancy-model.md) — `organizations` / `organization_memberships` / two-axis roles. Auth model for new endpoints.
5. [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) and [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md) — what's already on the floor and how OEX did it. Note: both pre-date the rename; mentally map "motion → campaign", "campaign → channel_campaign", and ignore OEX's per-lead step model entirely (hq-x has its own first-class step concept; see §5.5).
6. [`app/services/analytics.py`](app/services/analytics.py) — current emit path. Read every line.
7. [`app/services/recipients.py`](app/services/recipients.py) and [`app/services/channel_campaign_steps.py`](app/services/channel_campaign_steps.py) — `get_step_context()` is what `emit_event` calls to resolve the six-tuple. Skim.
8. [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py) and [`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py) — the canonical adapter+projector pattern that EmailBison/Vapi will follow. Skim so you know where `emit_event` is called from in production direct-mail flow.

---

## 1. Why

The audit exit position was: ClickHouse writes work, the query helper is deferred, the OEX multi-channel analytics router would be a 1,098-line port but most of it is moot because hq-x had no multi-channel campaign primitive.

Three subsequent PRs filled in the substrate:

- [#22](https://github.com/bencrane/hq-x/pull/22) — `gtm_motions → campaigns`, old `campaigns → channel_campaigns`. Two-layer outreach model.
- [#28](https://github.com/bencrane/hq-x/pull/28) — `channel_campaign_steps` (per-touch ordered execution under a channel_campaign) + Lob retrofit. Each step is 1:1 with an external provider object (e.g., one Lob campaign per direct-mail step).
- [#29](https://github.com/bencrane/hq-x/pull/29) — `recipients` + `channel_campaign_step_recipients`. Channel-agnostic, org-scoped identity layer; per-step audience memberships with their own state machine.

The canonical six-tuple `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel + provider)` is enforced at every emit site by [`app/services/analytics.py::emit_event`](app/services/analytics.py). Per-recipient events also carry `recipient_id`. The [`Lob adapter`](app/providers/lob/adapter.py) is the single API entry point for direct-mail activation; the [`Lob webhook projector`](app/webhooks/lob_processor.py) routes inbound events to the correct hierarchy + recipient and emits analytics. Other providers (EmailBison, Vapi/Twilio) will follow the same adapter+projector pattern.

That changes the calculus completely:

- "Multi-channel analytics" is now well-defined and matches the data model directly: campaign → child channel_campaigns → ordered steps → step_recipients (memberships) + per-recipient artifact rows.
- The RudderStack write side is a deliberate no-op stub. The choke point exists; only the client is missing.
- ClickHouse writes are already going through `emit_event` → `app.clickhouse.insert_row`. A query helper just adds the read path.
- **Recipient identity is now first-class.** Cross-channel rollups by recipient are tractable for the first time. (OEX never had this — its "leads" lived per-channel inside provider tables.)

Work to do: (a) wire the RudderStack client into the existing emit choke point, (b) add the ClickHouse query helper, (c) build the analytics router on top of (b) using the campaign + step + recipient model directly, instead of porting the OEX router structure verbatim. **Don't port the OEX router as-is** — its `step_order` / `sequence_step` / `campaign_lead_progress` per-lead funnel concept is a different abstraction and should not be reintroduced (see §5.5).

---

## 2. Scope

### In scope (this directive)

1. **RudderStack write integration** — turn the `emit_event()` shim into a real `analytics.track()` call. Config, client, retry, test seam.
2. **ClickHouse query helper** — port `ch_query()` + `ch_available()` from OEX, adapt to hq-x config conventions. Wire ClickHouse-preferred + Postgres-fallback into [`app/routers/voice_analytics.py`](app/routers/voice_analytics.py).
3. **Campaign analytics router** — new `/api/v1/analytics/*` endpoints scoped to organizations. Five feature groups:
   - **Campaign rollup** — given a `campaign_id` (umbrella), per-channel + per-channel_campaign + per-step breakdowns of events, outcomes, costs, plus unique-recipient counts per channel.
   - **Channel-campaign analytics** — per-channel_campaign drilldown with per-step breakdown and channel-specific extensions.
   - **Step analytics** — per-step drilldown with the membership funnel (`pending → scheduled → sent/failed/suppressed/cancelled`), exposing `external_provider_id` so operators can cross-reference the provider's UI 1:1.
   - **Reliability + direct-mail funnels** — port from OEX with field renames; the underlying `webhook_events` and `direct_mail_pieces` tables match the design.
   - **Recipient counts** rolled into campaign and step responses (unique recipients touched). NOT a standalone recipient-timeline endpoint — that's deferred (§2 out-of-scope).
4. **Wide `events` table in ClickHouse** — the substrate for cross-channel campaign analytics. Carries the six-tuple plus nullable `recipient_id`. `emit_event()` writes here in addition to event-typed tables.
5. **Tests** — port + extend OEX's analytics test pattern. Pure-function tests for aggregation logic; service-level tests with the in-memory `get_db_connection` fake; ClickHouse client tests with mocked httpx; RudderStack tests with mocked client.

### Out of scope (defer; flag in PR notes)

- **Recipient timeline endpoint** (given a `recipient_id`, show all events across all campaigns/channels) — defer. The substrate (recipient_id on artifact rows + on the wide events table) is built so this is a small future addition.
- **Cross-channel suppression** ("don't call a recipient who unsubscribed via email") — explicitly out per [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) §"Out of scope".
- **Per-lead engagement state on recipients** — out per the same section. `recipient_type` is identity, not workflow state.
- **Cross-organization recipient sharing** — never. Hard rule §5.10.
- **Cross-campaign analytics rollup** (multiple umbrella campaigns at once) — defer.
- **Multi-step scheduler** that activates step N+1 after step N's `delay_days_from_previous` window — explicitly out per the canonical doc.
- **Frontend** — backend only.
- **Tightening `direct_mail_pieces.channel_campaign_step_id` / `recipient_id` / `channel_campaign_id` / `campaign_id` to NOT NULL** — listed under "Cleanup follow-ups" in the canonical doc; out of this directive's scope.
- **OEX's per-lead `/api/analytics/campaigns/{id}/sequence-steps`** — different concept. Don't port. The hq-x step-analytics endpoint at §3 Phase 3 is a fresh design over `channel_campaign_steps` + `channel_campaign_step_recipients`.
- **OEX's `/api/analytics/clients` org-rollup** — replaced by campaign rollup.
- **Message sync health endpoint** — depends on email/linkedin providers that aren't wired yet; defer.

---

## 3. Architecture decisions you must make on day 0

### 3.1 RudderStack: server-side write key + Python SDK or HTTP?

The Python SDK exists and is straightforward; use it. Don't roll an HTTP client unless the SDK is unmaintained — verify on PyPI before starting. Configure via:

- `RUDDERSTACK_WRITE_KEY` (SecretStr, optional)
- `RUDDERSTACK_DATA_PLANE_URL` (str, optional)
- Both unset → silent skip, identical pattern to the ClickHouse client. **The fire-and-forget contract in [`app/services/analytics.py`](app/services/analytics.py) is non-negotiable: an unconfigured analytics layer must not break production paths.**

The client is a singleton; init it lazily on first `track()` call so importing `app.services.analytics` in tests doesn't require the SDK to be configured.

### 3.2 ClickHouse `events` table — one wide table or per-event-type?

Per-event-type tables exist (`call_events`, `direct_mail_piece_events`, etc.). Add **one wide `events` table** alongside them. Schema:

```sql
events (
  event_id                   UUID,
  event_name                 String,
  occurred_at                DateTime64(3),
  organization_id            UUID,
  brand_id                   UUID,
  campaign_id                UUID,             -- umbrella
  channel_campaign_id        UUID,             -- channel-typed execution unit
  channel_campaign_step_id   Nullable(UUID),   -- ordered step; null only for lifecycle events with no step (e.g. `campaign.activated`)
  recipient_id               Nullable(UUID),   -- per-recipient artifact events; null for non-recipient-bound events
  channel                    LowCardinality(String),
  provider                   LowCardinality(String),
  properties                 String            -- JSON blob of event-specific props
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(occurred_at)
ORDER BY (organization_id, campaign_id, channel_campaign_id, channel_campaign_step_id, occurred_at, event_id)
```

Reasoning: campaign rollup is the primary read pattern (`WHERE campaign_id = ? AND occurred_at BETWEEN ?` group by `channel, provider, channel_campaign_id, channel_campaign_step_id`). The ORDER BY makes those near-instant at every granularity. Per-event-type tables stay for typed-column queries (e.g., voice cost breakdown reads `call_events`, not the wide `events`).

`channel_campaign_step_id` and `recipient_id` are `Nullable(UUID)` because some events legitimately have neither — a `campaign.activated` lifecycle event sits at the umbrella level; a step's external-provider activation event has a step but no recipient. **`emit_event()` enforces** that step-bound event names carry a non-null `channel_campaign_step_id`, and that per-recipient event names carry a non-null `recipient_id`. Maintain a small allowlist of step-optional and recipient-optional event names; reject otherwise.

**Don't** ship the `CREATE TABLE` DDL in a Postgres migration — ClickHouse is provisioned out-of-band. Ship it as `docs/clickhouse-schema.md`, and document that an operator runs it on the cluster.

### 3.3 Auth model for new endpoints

All new analytics endpoints are organization-scoped. Use `require_org_context` ([`app/auth/roles.py`](app/auth/roles.py)). Read `active_organization_id` from `UserContext`. Every analytics query must filter by `organization_id` from the auth context — **never accept it as a query param**. Platform operators drive cross-org by setting `X-Organization-Id`, same pattern as `/api/v1/campaigns`.

### 3.4 Postgres fallback policy

When ClickHouse is unconfigured or `ch_available()` returns False:

- **Voice analytics endpoints** (already exist) — fall back to Postgres `call_logs`. Already implemented; preserve.
- **Campaign / channel-campaign / step rollup** — Postgres can answer by joining the artifact tables through the FK chain. Implement the fallback. Slower but correct.
- **Membership funnel** — Postgres-only; reads `channel_campaign_step_recipients` directly. No ClickHouse path needed (memberships are a relational state, not an event stream).
- **Reliability and direct-mail funnels** — these never touched ClickHouse in OEX. Postgres-only.

The response payload always includes `"source": "clickhouse"` or `"source": "postgres"` (or `"source": "postgres+events"` for hybrid views) so consumers can see which path served them.

**FK-chain reference (post-#28/#29 schema):**

| Source row | Step | Channel campaign | Campaign | Recipient |
|---|---|---|---|---|
| `direct_mail_pieces` | `channel_campaign_step_id` (direct, nullable) | `channel_campaign_id` (direct, denorm) | `campaign_id` (direct, denorm) | `recipient_id` (direct, nullable) |
| `call_logs` | (not yet populated; see canonical doc, "future PR follows the same pattern") | `channel_campaign_id` (direct) | via `channel_campaigns.campaign_id` | (not yet populated) |
| `sms_messages` | (not yet populated) | `channel_campaign_id` (direct) | via `channel_campaigns.campaign_id` | (not yet populated) |
| `channel_campaign_step_recipients` | `channel_campaign_step_id` (direct) | via `channel_campaign_steps.channel_campaign_id` | via `channel_campaign_steps.campaign_id` (denorm) | `recipient_id` (direct) |

Voice/SMS rows do not yet carry `channel_campaign_step_id` or `recipient_id` — that's flagged as future work in the canonical doc. For step-level analytics on voice/SMS, treat the step granularity as "step unknown" and surface a single synthetic step per channel_campaign in the rollup until those columns land. Note this in the response (`"voice_step_attribution": "synthetic"`) so consumers know.

---

## 4. Phased implementation

Ship as **three** PRs, in order. Each is independently reviewable and deployable.

### Phase 1 — ClickHouse query helper + voice analytics fallback

**PR title:** "ClickHouse query helper + voice analytics ClickHouse fallback"

**Files:**
- [`app/clickhouse.py`](app/clickhouse.py) — append `ch_query(query, params=None)` and `ch_available()`. ~40 lines.
- [`app/routers/voice_analytics.py`](app/routers/voice_analytics.py) — wrap each endpoint with `if ch_available(): try ClickHouse else: Postgres`. ~60 lines added.
- `tests/test_clickhouse_client.py` — new. Mock httpx; test `insert_row`, `ch_query`, `ch_available` happy path + failure modes. Reference: [`/Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py).
- `tests/test_voice_analytics_clickhouse.py` — new. Verify ClickHouse path is preferred when available, Postgres fallback when not.

**Implementation notes:**
- Reference: [`/Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62-80`](file:///Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62). ~20 lines. Use ClickHouse native parameterization (`{name:String}` placeholders + `param_<name>` URL params) — **never interpolate values into the SQL string.**
- Adapt for hq-x's `SecretStr` config (`settings.CLICKHOUSE_PASSWORD.get_secret_value()`).
- Field renames in queries used by voice_analytics:
  - OEX `org_id` → hq-x: filter by `brand_id` (voice analytics is brand-scoped).
  - OEX `company_campaign_id` → hq-x: `channel_campaign_id`.
  - Outcome enum: OEX `'transferred'` → hq-x `'qualified_transfer'`.
- Keep the Postgres fallback **byte-identical** to current behavior.

**Acceptance:**
- New tests green; existing voice analytics tests pass unchanged.
- No new env vars required to ship. Without `CLICKHOUSE_*` set, every request still goes to Postgres.

### Phase 2 — RudderStack write integration

**PR title:** "RudderStack: real client behind emit_event"

**Files:**
- [`pyproject.toml`](pyproject.toml) — add the canonical RudderStack Python SDK. Verify on PyPI before adding.
- [`app/config.py`](app/config.py) — add `RUDDERSTACK_WRITE_KEY: SecretStr | None` and `RUDDERSTACK_DATA_PLANE_URL: str | None`.
- `app/rudderstack.py` — new module. Mirrors [`app/clickhouse.py`](app/clickhouse.py) shape:
  - `_is_configured()` — both env vars set.
  - `_get_client()` — lazy singleton init.
  - `track(event_name, *, anonymous_id, properties)` — fire-and-forget. Never raises.
  - `flush()` — call on app shutdown.
- [`app/services/analytics.py`](app/services/analytics.py) — replace the no-op shim with `rudderstack.track()`. The six-tuple + (`recipient_id` if present) goes into `properties`; `anonymous_id` is `organization_id` cast to str.
- [`app/main.py`](app/main.py) — register the rudderstack flush in the lifespan shutdown handler.
- `tests/test_rudderstack_client.py` — mock the SDK; verify `track` is called with the expected payload and an unconfigured client is a no-op.
- `tests/test_analytics_emit.py` — extend to assert RudderStack `track` is called when env is configured.

**Implementation notes:**
- The SDK has a background batch thread. Init once. The `flush()` on shutdown is important — otherwise in-flight events are dropped on container restart.
- **Order:** log first, then ClickHouse, then RudderStack. If RudderStack init throws (it shouldn't), logs and CH still fire.
- Anonymous-id strategy: use `organization_id`. User-level identify is out of scope.
- `event_name`, `occurred_at`, six-tuple, optional `recipient_id` all go in the `properties` dict. The SDK's `event` argument is `event_name`.
- Don't break `emit_event`'s current signature. The canonical helper already accepts `channel_campaign_step_id` (preferred) or `channel_campaign_id`; the RudderStack call sits inside, after `get_step_context()` / `get_channel_campaign_context()` has run.

**Acceptance:**
- Without `RUDDERSTACK_*` env: every existing test passes; `emit_event` still logs + still writes to ClickHouse if configured; RudderStack code is a no-op.
- With env: a `track()` call per `emit_event()`. Mock-verified.
- App shutdown flushes the queue. Mock + lifespan test.

### Phase 3 — Campaign analytics router

**PR title:** "Campaign analytics router + step drilldown + membership funnel + reliability + direct-mail funnels"

This is the largest phase. Build it incrementally — each endpoint is a separate commit if helpful for review.

#### 3.1 Wide `events` table substrate

- `docs/clickhouse-schema.md` — document the wide `events` table DDL (§3.2) and the partitioning/ordering rationale.
- [`app/services/analytics.py`](app/services/analytics.py)`::emit_event` — when the caller passes `clickhouse_table`, ALSO insert a row into the wide `events` table. The wide row uses the six-tuple, `event_name`, `occurred_at`, optional `recipient_id`, and `properties` JSON-serialized. Both writes are fire-and-forget.
- Maintain step-optional and recipient-optional event-name allowlists in `app/services/analytics.py`. Step-bound events (every direct-mail piece event, every call, every SMS, every email send) MUST carry the step id. Per-recipient events MUST carry `recipient_id`.

#### 3.2 Campaign rollup endpoint

`GET /api/v1/analytics/campaigns/{campaign_id}/summary?from=&to=`

`{campaign_id}` is the **umbrella** campaign (`business.campaigns`).

```
{
  "campaign": { "id", "name", "status", "start_date", "brand_id", "organization_id" },
  "totals": {
    "events_total": N,
    "unique_recipients_total": N,
    "cost_total_cents": N
  },
  "channel_campaigns": [
    {
      "channel_campaign_id", "name", "channel", "provider", "status", "scheduled_send_at",
      "events_total", "unique_recipients": N, "cost_total_cents": N,
      "outcomes": { "succeeded": N, "failed": N, "skipped": N },
      "steps": [
        {
          "channel_campaign_step_id", "step_order", "name",
          "external_provider_id",
          "events_total", "cost_total_cents",
          "outcomes": { "succeeded": N, "failed": N, "skipped": N },
          "memberships": { "pending": N, "scheduled": N, "sent": N, "failed": N, "suppressed": N, "cancelled": N }
        }
      ]
    }
  ],
  "by_channel": [
    { "channel", "events_total", "unique_recipients": N, "outcomes", "cost_total_cents" }
  ],
  "by_provider": [
    { "provider", "events_total", "outcomes", "cost_total_cents" }
  ],
  "source": "clickhouse" | "postgres" | "postgres+events"
}
```

Auth: `require_org_context`. Filter by `organization_id` from auth + the requested `campaign_id`. Verify the campaign belongs to the auth's org — return 404 if not (don't leak existence).

ClickHouse path: one query against the wide `events` table grouping by `(channel, provider, channel_campaign_id, channel_campaign_step_id)`, plus `uniqExact(recipient_id)` for unique-recipient counts. Use ClickHouse-native parameterization for `campaign_id`, `from`, `to`. Roll up step rows into their parent channel_campaign in application code. Membership counts always come from Postgres `channel_campaign_step_recipients` regardless of ClickHouse availability — that's relational state, not events. Hence the optional `"postgres+events"` source.

Postgres fallback: see §3.4. Join chain: artifact tables → `channel_campaign_steps` → `channel_campaigns` → `campaigns`. Aggregate in SQL or Python (your call — measure first).

#### 3.3 Channel-campaign analytics endpoint

`GET /api/v1/analytics/channel-campaigns/{channel_campaign_id}/summary?from=&to=`

Same shape as campaign rollup but scoped to one channel_campaign. Includes the `steps` array. Channel-specific extensions:
- voice: `transfer_rate`, `avg_duration_seconds`, cost breakdown (transport / stt / llm / tts / vapi). Voice/SMS step granularity is synthetic until `call_logs.channel_campaign_step_id` is wired (canonical doc, "Out of scope" §).
- sms: `delivery_rate`, `opt_out_count`.
- direct_mail: piece funnel (queued → processed → in_transit → delivered → returned/failed) at the channel_campaign level AND per-step. Each step is 1:1 with a Lob campaign object so per-step funnels are clean.
- email: scaffold — return zeros (emailbison isn't wired).

Auth: same as campaign rollup. Verify the channel_campaign's `organization_id` matches auth.

#### 3.4 Step analytics endpoint

`GET /api/v1/analytics/channel-campaign-steps/{channel_campaign_step_id}/summary?from=&to=`

Per-step drilldown. Returns:
- Step metadata (parent channel_campaign, parent campaign, provider, **`external_provider_id`** so operators can cross-reference the provider's UI 1:1 — e.g. the Lob `cmp_*` id).
- Events / outcomes / costs.
- Channel-specific funnel (direct_mail piece funnel; voice top-N call outcomes; sms delivery breakdown).
- **Membership funnel** — counts of `channel_campaign_step_recipients.status` in each state (`pending → scheduled → sent / failed / suppressed / cancelled`). State machine reference: [`docs/lob-integration.md`](docs/lob-integration.md#membership-status-state-machine).

Auth: same. Verify the step's `organization_id` matches auth (resolved through the FK chain).

#### 3.5 Direct-mail funnel endpoint

`GET /api/v1/analytics/direct-mail?brand_id=&channel_campaign_id=&channel_campaign_step_id=&from=&to=`

Port from OEX [`/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) `/direct-mail` (~350 LOC). Field renames:
- OEX `org_id`/`company_id` → hq-x `organization_id`/`brand_id`/`partner_id`.
- OEX `company_campaign_id` (channel-typed) → hq-x `channel_campaign_id`.
- New optional filter `channel_campaign_step_id` — drills down to one Lob campaign object's pieces.
- Filter scope: auth-context-derived `organization_id`, never request param.

Keep OEX's safety gates: max 93-day window, `max_rows=20000` cap, paginated `failure_reason_breakdown` and `daily_trends`.

Tables: `direct_mail_pieces`, `webhook_events` (provider_slug='lob'). Postgres-only; no ClickHouse path needed.

#### 3.6 Reliability endpoint

`GET /api/v1/analytics/reliability?from=&to=`

Port from OEX. Group `webhook_events` by `provider_slug`, count replays, sum `replay_count`, count errors. Filter by `organization_id` from auth.

Cleanest port in the entire OEX router. ~120 LOC.

#### 3.7 Models

`app/models/analytics.py` (new). Pydantic response shapes for the five endpoint responses. Don't mirror OEX's models 1:1 — design fresh against the new payload shape.

#### 3.8 Router wiring

`app/routers/analytics.py` (new). Mount at `/api/v1/analytics` in [`app/main.py`](app/main.py). Don't reuse OEX's prefix (`/api/analytics`) — hq-x is on `/api/v1`.

#### 3.9 Tests

- `tests/test_campaign_analytics_pure.py` — pure aggregation functions (group/sum/derive at all three granularities + recipient counts). Reference: `tests/test_campaigns_pure.py`.
- `tests/test_campaign_analytics_db_fake.py` — service-level with `get_db_connection` in-memory fake. Reference: `tests/test_campaigns_services_db_fake.py`. Cover the four-level join (artifact → step → channel_campaign → campaign) and the recipient lookup.
- `tests/test_campaign_analytics_clickhouse.py` — ClickHouse path with mocked `ch_query`. Verify SQL parameters (including `channel_campaign_step_id`, `recipient_id`) and result-shape mapping.
- `tests/test_step_analytics.py` — step drilldown. Both ClickHouse path and Postgres path. Membership funnel reads from Postgres regardless of CH availability — assert that.
- `tests/test_membership_funnel.py` — per-state counts on `channel_campaign_step_recipients`; transitions captured correctly.
- `tests/test_reliability_analytics.py` — Postgres-only.
- `tests/test_direct_mail_analytics.py` — Postgres-only; reuse OEX test patterns where logic is identical. Cover the new `channel_campaign_step_id` filter.

Test count target: substantial new coverage across the phase. Full suite must remain green.

**Acceptance for Phase 3:**
- All five endpoints work end-to-end against a seeded test DB.
- Campaign rollup serves ClickHouse when configured (mock-verified), Postgres otherwise.
- **Property test:** `sum(step.events_total for step in cc.steps) == cc.events_total` for every channel_campaign in every fixture. Drift here means an event was written without the right step id.
- **Property test:** unique-recipient count at the campaign level ≤ sum of unique-recipient counts per channel (a recipient can be touched on multiple channels, but not double-counted at the umbrella).
- Direct-mail funnel matches OEX behavior on a port-equivalence test (run identical inputs through both, compare outputs).
- Cross-org leakage: org A auth + org B campaign / channel_campaign / step / recipient → 404. **Add this test for every new endpoint.**
- ruff clean, full suite green.

---

## 5. Hard rules

1. **Six-tuple is sacred.** Every event written via `emit_event()` carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel + provider)`. Per-recipient events also carry `recipient_id`. Don't add a code path that bypasses `emit_event()`. Don't accept `organization_id` as a request body field on any analytics endpoint — it comes from auth context.

2. **Org isolation tested per endpoint.** Every new endpoint gets a "user from org A asking about org B's resource → 404" test. No exceptions.

3. **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, `track()` never raise into the caller. They log on failure. Adding a `raise` to any of these is a bug.

4. **No silent assignment.** If a query needs a `campaign_id` and the campaign doesn't belong to the caller's org, return 404. Don't fall back to "any campaign the user can see." Don't auto-pick.

5. **OEX's per-lead step model is banned; hq-x's first-class steps are mandatory.** Don't reintroduce OEX's `step_order` / `step_status` / `campaign_lead_progress` / `campaign_sequence_steps` per-lead funnel concept under any name. DO use hq-x's first-class `channel_campaign_steps` (each step = one external provider object, not a per-lead state). The two are different abstractions; conflating them produces a confused data model.

6. **ClickHouse parameterization, not interpolation.** Every `ch_query()` call uses `{name:Type}` placeholders + the `params` dict. SQL injection risk if you string-format values into the query.

7. **Postgres fallback stays correct on its own.** ClickHouse is an opt-in perf layer. The Postgres path produces the same response shape and same `source` field, just slower. Membership funnel queries always read from Postgres — that's relational state, not an event stream.

8. **Don't port the OEX analytics router structurally.** Use it as a behavior reference for direct-mail and reliability endpoints. Everything else is built fresh against the campaign + step + recipient model.

9. **Mind the four-level naming.** `campaign_id` = umbrella (`business.campaigns`). `channel_campaign_id` = channel-typed execution unit (`business.channel_campaigns`). `channel_campaign_step_id` = ordered step (`business.channel_campaign_steps`). `recipient_id` = identity (`business.recipients`). Mixing any pair will silently produce wrong rollups. When in doubt, re-read [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) §"The hierarchy in one picture".

10. **Recipients are organization-scoped only.** Never resolve a recipient across orgs. The same business in two orgs is two recipient rows. Any query that filters by `recipient_id` MUST also filter by `organization_id` from auth context. Per the canonical doc: cross-org recipient sharing is intentionally not supported and is not on the roadmap.

11. **Provider adapters are the emit chokepoint.** Direct-mail piece events are emitted from [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py) and [`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py). EmailBison/Vapi/etc. will follow the same pattern. **Don't add new emit sites in webhook handlers, routers, or scattered services** — funnel them through the adapter or the lifecycle services. If you need to emit from outside an adapter (e.g., `campaign.activated`), use the `emit_event()` helper with a step-optional / recipient-optional event name from the §3.1 allowlist.

12. **No frontend, no docs site.** Backend only.

13. **Migration discipline.** ClickHouse DDL is operator-applied; ship as `docs/clickhouse-schema.md` or `scripts/clickhouse/`, NOT as a Postgres migration. New Postgres migrations follow the **timestamp prefix convention** introduced with `20260429T120000_recipients.sql` (`YYYYMMDDTHHMMSS_<slug>.sql`), not the legacy `00NN_*` numeric scheme. Per the canonical doc, this lex-sorts correctly after the legacy files and avoids collisions when multiple agents work in parallel. As of this directive, you don't need any Postgres migration.

---

## 6. File-by-file expected delta

| Phase | File | Action | Approx LOC |
|-------|------|--------|------------|
| 1 | `app/clickhouse.py` | Append `ch_query`, `ch_available` | +40 |
| 1 | `app/routers/voice_analytics.py` | Add ClickHouse branches | +60 |
| 1 | `tests/test_clickhouse_client.py` | New | +200 |
| 1 | `tests/test_voice_analytics_clickhouse.py` | New | +150 |
| 2 | `pyproject.toml` | Add rudder SDK | +1 |
| 2 | `app/config.py` | Add 2 settings | +6 |
| 2 | `app/rudderstack.py` | New | +80 |
| 2 | `app/services/analytics.py` | Wire `track()` call | +15 |
| 2 | `app/main.py` | Lifespan flush | +5 |
| 2 | `tests/test_rudderstack_client.py` | New | +120 |
| 2 | `tests/test_analytics_emit.py` | Extend | +60 |
| 3 | `docs/clickhouse-schema.md` | New | +140 |
| 3 | `app/services/analytics.py` | Wide-events write + recipient_id propagation + allowlists | +40 |
| 3 | `app/services/campaign_analytics.py` | New (campaign + channel_campaign rollups) | +320 |
| 3 | `app/services/step_analytics.py` | New (step drilldown + membership funnel) | +220 |
| 3 | `app/services/direct_mail_analytics.py` | New (port from OEX, with step filter) | +320 |
| 3 | `app/services/reliability_analytics.py` | New | +100 |
| 3 | `app/models/analytics.py` | New | +160 |
| 3 | `app/routers/analytics.py` | New | +260 |
| 3 | `app/main.py` | Mount router | +2 |
| 3 | `tests/test_campaign_analytics_pure.py` | New | +240 |
| 3 | `tests/test_campaign_analytics_db_fake.py` | New | +480 |
| 3 | `tests/test_campaign_analytics_clickhouse.py` | New | +220 |
| 3 | `tests/test_step_analytics.py` | New | +220 |
| 3 | `tests/test_membership_funnel.py` | New | +160 |
| 3 | `tests/test_reliability_analytics.py` | New | +150 |
| 3 | `tests/test_direct_mail_analytics.py` | New | +260 |

**Total: ~3,800 LOC across ~27 files. ~50% tests.** Sizing estimates only.

---

## 7. Reference map (where to read in OEX)

Reference-only. Don't copy structure; understand the behavior, then build against the campaign + step + recipient model.

| Concept | OEX file | What to take from it |
|---------|----------|---------------------|
| `ch_query` / `ch_available` | [`src/clickhouse.py:62-96`](file:///Users/benjamincrane/outbound-engine-x/src/clickhouse.py:62) | Verbatim port (with hq-x SecretStr adapter) |
| ClickHouse fallback pattern | [`src/routers/voice_analytics.py:74-250`](file:///Users/benjamincrane/outbound-engine-x/src/routers/voice_analytics.py:74) | The "if available → CH else PG" idiom |
| Direct-mail funnel logic | [`src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) (search for `/direct-mail`) | Funnel mapping, daily bucket prefill, payload extraction |
| Reliability rollup | same file (`/reliability`) | Group-by + replay/error count logic |
| Channel→provider mapping | [`src/models/multi_channel.py`](file:///Users/benjamincrane/outbound-engine-x/src/models/multi_channel.py) `CHANNEL_BY_PROVIDER_SLUG` | Reference; `provider` is already first-class on `business.channel_campaigns` so we don't need this map |
| Tests | [`tests/test_clickhouse_client.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_clickhouse_client.py), [`tests/test_voice_analytics.py`](file:///Users/benjamincrane/outbound-engine-x/tests/test_voice_analytics.py) | Mock patterns for httpx + DB fakes |

In hq-x, the analogue patterns to study before writing similar code:

| Concept | hq-x file |
|---------|-----------|
| Provider adapter (chokepoint pattern) | [`app/providers/lob/adapter.py`](app/providers/lob/adapter.py) |
| Webhook projector (event routing + emit) | [`app/webhooks/lob_processor.py`](app/webhooks/lob_processor.py) |
| Step context resolver | [`app/services/channel_campaign_steps.py`](app/services/channel_campaign_steps.py) (`get_step_context`) |
| Recipient upsert + memberships | [`app/services/recipients.py`](app/services/recipients.py) |
| Pure-test pattern | [`tests/test_campaigns_pure.py`](tests/test_campaigns_pure.py), [`tests/test_recipients_pure.py`](tests/test_recipients_pure.py) |
| DB-fake test pattern | [`tests/test_campaigns_services_db_fake.py`](tests/test_campaigns_services_db_fake.py), [`tests/test_lob_projector.py`](tests/test_lob_projector.py) |

**Do NOT take from OEX:**
- `_resolve_company_scope` — replaced by `require_org_context`.
- `campaign_sequence_steps`, `campaign_events`, `campaign_lead_progress` — OEX's per-lead step model. Different concept from hq-x's `channel_campaign_steps` (execution-batch with 1:1 external-object mapping). Don't conflate.
- `/clients` org-rollup endpoint — replaced by campaign rollup.
- `/sequence-steps` endpoint — different concept; hq-x's step-analytics endpoint is a fresh design over `channel_campaign_steps` + `channel_campaign_step_recipients`.
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
| Cross-org leakage via recipient lookups specifically | Hard-rule §5.10. Every recipient query filters by `organization_id` from auth in the SAME `WHERE` clause as the recipient match — never a two-step lookup that could leak via a 200 vs 404 timing oracle. |
| Postgres campaign-rollup query is slow on real data | Add `EXPLAIN ANALYZE` to the PR for the largest seed dataset available. If too slow on realistic volume, add a Postgres index in a follow-up migration. ClickHouse availability solves it permanently. |
| Per-event-type tables (`call_events`) drift out of sync with wide `events` table | The wide table is the single source of truth for campaign analytics; per-type tables are queried only by their direct consumers. Document this in `docs/clickhouse-schema.md`. |
| Six-tuple gets bypassed by some new emit site | Code review enforces it. Consider a CI grep-gate that flags direct `clickhouse.insert_row` calls outside `app/services/analytics.py` AND direct `emit_event` calls outside the adapter / projector / lifecycle-service files. |
| Confusing umbrella `campaign_id` vs execution `channel_campaign_id` vs step `channel_campaign_step_id` vs identity `recipient_id` | Hard-rule §5.9. When writing SQL, name your parameters explicitly until the naming is reflexive. Test fixtures should use distinguishable UUIDs at each layer (`00…0c1`, `00…0cc1`, `00…0ccc1`, `00…0ccccr`) so a swapped argument fails loudly. |
| Step-rollup numbers don't reconcile to channel_campaign totals | Property test: `sum(step.events_total) == cc.events_total`. Drift here means an event was written without the right step id. |
| Membership funnel and event counts disagree | They will, sometimes — memberships count distinct (step, recipient) pairs in a state; events count emits that may include non-membership lifecycle rows. Document this clearly in the API response and don't try to force reconciliation. |
| Voice/SMS step granularity is missing for now | Per canonical doc, voice/SMS rows don't yet carry `channel_campaign_step_id` or `recipient_id`. Use the synthetic-step fallback from §3.4 and surface `"voice_step_attribution": "synthetic"` in responses. File a follow-up issue. |

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
- [ ] `campaign_id` (umbrella), `channel_campaign_id` (execution), `channel_campaign_step_id` (step), and `recipient_id` (identity) are not confused in any query, response shape, or test fixture.
- [ ] Step-bound event names always carry a non-null `channel_campaign_step_id`; only allowlisted lifecycle event names omit it.
- [ ] Per-recipient event names always carry a non-null `recipient_id`; only allowlisted non-recipient event names omit it.

## Cross-org leakage
- [ ] Negative test added for every new endpoint: org A auth + org B resource id → 404.
- [ ] Recipient lookups filter by `organization_id` in the same WHERE clause as the recipient match.

## Fallback behavior
- [ ] Without RUDDERSTACK_* env: every test still passes.
- [ ] Without CLICKHOUSE_* env: every test still passes; analytics endpoints serve from Postgres with `"source": "postgres"` (or `"postgres+events"` for hybrid views).

## Test plan
- [ ] `pytest -q` green.
- [ ] `ruff check` clean on every touched file.
- [ ] Manual smoke against a seeded org: campaign rollup returns expected counts at all four granularities.
```

---

## 10. Definition of done (whole directive)

- All three phases merged to `main`.
- `pytest -q` green; substantial new test coverage across all three phases.
- `ruff check` clean.
- `docs/clickhouse-schema.md` exists; cluster operator has applied the wide `events` DDL (or has a ticket to do so).
- A short post-ship summary at `docs/analytics-buildout-pr-notes.md` describing what shipped, what's deferred, and any caveats.
- Out-of-scope items from §2 are explicitly listed in the post-ship summary so the next agent has the inheritance context.

---

## 11. Questions to confirm with Ben before starting

1. **RudderStack SDK choice** — Python SDK from PyPI, or a custom httpx wrapper? (Default: SDK.)
2. **`anonymous_id` strategy** — `organization_id` is a fine default. Confirm we're not user-tracking yet.
3. **ClickHouse cluster ownership** — who runs the cluster, who applies the wide `events` DDL?
4. **Direct-mail funnel test data** — is there a seeded test fixture that exercises the full Lob webhook lifecycle? If not, we add one in Phase 3.
5. **Voice/SMS step + recipient wiring** — at the time Phase 3 starts, do `call_logs` / `sms_messages` carry `channel_campaign_step_id` / `recipient_id`, or only `direct_mail_pieces`? If only direct-mail, Phase 3 ships with the synthetic-step fallback for voice/SMS rollups, and a follow-up PR upgrades them once the upstream wiring lands.
6. **Recipient-timeline endpoint** — confirmed deferred. Re-confirm scope is OK to ship without it.
7. **Phase ordering** — `Phase 1 → 2 → 3` is the lowest-risk path. Front-loading RudderStack is fine. ClickHouse query helper (Phase 1) must precede campaign analytics (Phase 3).

---

**Author:** Drafted by the audit agent that produced [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) and [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md). Updated through the campaigns rename ([#22](https://github.com/bencrane/hq-x/pull/22)), the `channel_campaign_steps` workstream ([#28](https://github.com/bencrane/hq-x/pull/28)), and the `recipients` identity layer ([#29](https://github.com/bencrane/hq-x/pull/29)). All file:line citations were live at draft time; verify before relying on any specific reference.
