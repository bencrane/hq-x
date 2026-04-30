# Directive — hq-x analytics buildout: remaining slices

**For:** an implementation agent picking up the analytics workstream after **slice 1a** (the `/reliability` endpoint + analytics router scaffold) has been committed and merged. Worktree path: `/Users/benjamincrane/hq-x/.claude/worktrees/lucid-mcclintock-ee4e8b`. Branch: `claude/lucid-mcclintock-ee4e8b`.

This directive supersedes the original [`DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md`](DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md) for the remaining work. Read both: the original has full architectural rationale; this one captures decisions that were made interactively and the precise scope of what's left.

---

## 0. What's already done (don't redo it)

Commit `9fd9e02` on branch `claude/lucid-mcclintock-ee4e8b` ships:

- [`app/routers/analytics.py`](app/routers/analytics.py) — analytics router mounted at `/api/v1/analytics`. Currently has one endpoint: `GET /reliability`.
- [`app/services/reliability_analytics.py`](app/services/reliability_analytics.py) — `summarize_reliability(*, organization_id, brand_id, start, end)`. Joins `webhook_events → business.brands` for org isolation.
- [`app/models/analytics.py`](app/models/analytics.py) — Pydantic response models. Currently has `ReliabilityResponse` + helpers. Add new response models here.
- [`tests/test_reliability_analytics.py`](tests/test_reliability_analytics.py) — 11 tests covering service layer + endpoint, including a cross-org leakage guard and window validation.
- [`app/main.py`](app/main.py) — `analytics_router` is imported and mounted.

The full test suite is at **433 passing, 0 failing, ruff clean**. Keep it that way — every slice you ship should leave the suite green.

---

## 1. What changed vs. the original directive (read this carefully)

The interactive design conversation that produced this remainder reached the following decisions. **Do not relitigate them — they are the operating constraints.**

### 1.1 ClickHouse is dropped from scope, entirely

- No ClickHouse cluster is provisioned. The free trial expired; the old data was for a different business model and is irrelevant.
- The wide `events` table DDL doc, `ch_query`/`ch_available` helpers, and "ClickHouse-preferred + Postgres-fallback" branching from the original directive are **all out of scope.**
- Every analytics endpoint is **Postgres-only**. Response payloads carry `"source": "postgres"`.
- The existing `app/clickhouse.py` (`insert_row` only) is left untouched. The `clickhouse_table` parameter on `emit_event()` continues to fire-and-forget no-op insert when env vars are unset, which is the perpetual state.
- **Do not add any ClickHouse code, tests, or docs.** If/when a cluster is ever provisioned later, that's a future project.

### 1.2 RudderStack IS in scope (Phase 2)

- A RudderStack workspace already exists. A Python source named `hq-x-server` has been created.
- Doppler `hq-x` already has these secrets in both `dev` and `prd`:
  - `RUDDERSTACK_WRITE_KEY`
  - `RUDDERSTACK_DATA_PLANE_URL=https://substratevyaxk.dataplane.rudderstack.com`
- Wire the `rudder-sdk-python` SDK behind the existing [`emit_event()`](app/services/analytics.py:74) chokepoint. Lifespan flush on shutdown. See §3 below.

### 1.3 Recipient timeline endpoint IS in scope

- The original directive deferred `GET /api/v1/analytics/recipients/{recipient_id}/timeline`. **It's now in scope.**
- The substrate (recipient_id on `direct_mail_pieces`, channel_campaign_step_recipients) is in place. Voice/SMS rows don't yet carry recipient_id — that's flagged in §2.4.

### 1.4 Voice/SMS step + recipient wiring stays out of scope

- `call_logs` and `sms_messages` still don't carry `channel_campaign_step_id` or `recipient_id`. This is a separate workstream.
- Use the **synthetic-step fallback** for voice/SMS in any rollup that touches them: surface a single synthetic step per channel_campaign and include `"voice_step_attribution": "synthetic"` (or the SMS analog) in the response payload so consumers know.
- Do NOT add migrations to wire these columns. That's a future PR.

### 1.5 PR strategy

- Each slice (1b, 1c, 1d, 1e, 1f) is its own commit on branch `claude/lucid-mcclintock-ee4e8b`, opened as its own PR against `main`.
- The RudderStack phase (2) is a separate commit + PR after the slices are done.
- Land each PR before opening the next, OR open them all sequentially against `main` if you prefer — both are fine. Per-slice review is the goal.
- Keep PR titles short (under 70 chars). Use the format from `git log --oneline` on `main` — short imperative summaries.

### 1.6 Test conventions

The test patterns established in slice 1a are the template:

- **Auth bypass:** `app.dependency_overrides[require_org_context] = lambda: _user(ORG_A)`. Use the `_user(...)` factory pattern from [`tests/test_reliability_analytics.py`](tests/test_reliability_analytics.py:29).
- **Cross-org leakage test:** every endpoint must have a test that verifies the SQL params include the auth's `organization_id` and the SQL goes through an org-scoped join. Where applicable, also test "user in org A asking about org B's resource → 404."
- **Postgres fake:** the `_FakeCursor` / `_FakeConn` / `_patch_pg` pattern from `test_reliability_analytics.py` is reusable. Capture SQL + params via the fake so you can assert on them.
- **Window validation:** every endpoint that takes `?from=&to=` should reuse `_resolve_window` from `app/routers/analytics.py` (or refactor it into a shared module if you add a third copy).

### 1.7 Hard rules (unchanged from original directive — re-stated for clarity)

1. **Six-tuple is sacred.** Every event written via `emit_event()` carries `(organization_id, brand_id, campaign_id, channel_campaign_id, channel_campaign_step_id, channel + provider)`. Per-recipient events also carry `recipient_id`. Don't add a code path that bypasses `emit_event()`.
2. **Org isolation tested per endpoint.** Every new endpoint gets a "user from org A asking about org B's resource → 404" test. No exceptions.
3. **Fire-and-forget on writes.** `emit_event()`, `insert_row()`, `track()` (RudderStack) never raise into the caller. They log on failure.
4. **No silent assignment.** If a query needs a `campaign_id` and the campaign doesn't belong to the caller's org, return 404. Don't fall back.
5. **Don't reintroduce OEX's per-lead step model.** No `step_order` / `campaign_lead_progress` / `campaign_sequence_steps` per-lead state. hq-x's `channel_campaign_steps` are first-class execution units.
6. **Mind the four-level naming.** `campaign_id` = umbrella. `channel_campaign_id` = channel-typed execution unit. `channel_campaign_step_id` = ordered step. `recipient_id` = identity. Mixing any pair silently produces wrong rollups.
7. **Recipients are organization-scoped only.** Any query filtering by `recipient_id` MUST also filter by `organization_id` from auth context **in the same WHERE clause** as the recipient match — never a two-step lookup that could leak via timing.
8. **Provider adapters are the emit chokepoint.** Don't add new emit sites in webhook handlers or scattered services. Use the existing `emit_event()` helper from `app/services/analytics.py`.
9. **No frontend, no doc-site updates.** Backend only.
10. **Migration discipline.** No new Postgres migrations needed for any slice. If you find yourself wanting one, stop and ask — most likely you're trying to wire voice/SMS step+recipient (out of scope per §1.4).

### 1.8 What "the canonical model" means

Read [`docs/campaign-rename-pr-notes.md`](docs/campaign-rename-pr-notes.md) **front-to-back before writing any code**. It is the canonical reference for the campaigns hierarchy:

```
business.organizations
  └── business.brands
        └── business.campaigns                  (umbrella)
              └── business.channel_campaigns    (one per channel run)
                    └── business.channel_campaign_steps   (ordered touches)
                          ├── business.channel_campaign_step_recipients
                          └── per-recipient artifact rows
                                (direct_mail_pieces, call_logs, sms_messages)
business.recipients ◄── channel-agnostic identity (org-scoped)
```

Reference [`docs/lob-integration.md`](docs/lob-integration.md) for the membership state machine (`pending → scheduled → sent / failed / suppressed / cancelled`) and the `_PIECE_TERMINAL_SENT` / `_PIECE_TERMINAL_FAILED` event sets that drive transitions.

---

## 2. Slices to ship (in order)

Each slice is one commit + one PR. Estimated LOC includes tests.

### Slice 1b — Campaign rollup endpoint

**Goal:** given an umbrella `campaign_id`, return per-channel + per-channel_campaign + per-step rollups of events, outcomes, costs, plus unique-recipient counts per channel.

**Endpoint:** `GET /api/v1/analytics/campaigns/{campaign_id}/summary?from=&to=`

**Auth:** `require_org_context`. The campaign must belong to the caller's org or return 404 (don't leak existence).

**Response shape:**

```json
{
  "campaign": {
    "id": "...",
    "name": "...",
    "status": "active",
    "start_date": "2026-04-01",
    "brand_id": "...",
    "organization_id": "..."
  },
  "window": {"from": "...", "to": "..."},
  "totals": {
    "events_total": 0,
    "unique_recipients_total": 0,
    "cost_total_cents": 0
  },
  "channel_campaigns": [
    {
      "channel_campaign_id": "...",
      "name": "...",
      "channel": "direct_mail",
      "provider": "lob",
      "status": "scheduled",
      "scheduled_send_at": "...",
      "events_total": 0,
      "unique_recipients": 0,
      "cost_total_cents": 0,
      "outcomes": {"succeeded": 0, "failed": 0, "skipped": 0},
      "steps": [
        {
          "channel_campaign_step_id": "...",
          "step_order": 1,
          "name": "...",
          "external_provider_id": "cmp_...",
          "events_total": 0,
          "cost_total_cents": 0,
          "outcomes": {"succeeded": 0, "failed": 0, "skipped": 0},
          "memberships": {
            "pending": 0, "scheduled": 0, "sent": 0,
            "failed": 0, "suppressed": 0, "cancelled": 0
          }
        }
      ]
    }
  ],
  "by_channel": [
    {"channel": "direct_mail", "events_total": 0, "unique_recipients": 0, "outcomes": {}, "cost_total_cents": 0}
  ],
  "by_provider": [
    {"provider": "lob", "events_total": 0, "outcomes": {}, "cost_total_cents": 0}
  ],
  "source": "postgres"
}
```

**Data sources (Postgres-only):**
- Channel campaigns + steps for the campaign: join `business.channel_campaigns` → `business.channel_campaign_steps`. Steps are 1:1 with provider primitives (`external_provider_id`).
- Per-step events / outcomes / costs from artifact tables:
  - `direct_mail_pieces` (has `channel_campaign_step_id`, `recipient_id`, `cost_cents`, `status`)
  - `direct_mail_piece_events` (events log; for event counts)
  - `call_logs` — synthetic step (no `channel_campaign_step_id` yet); roll up at channel_campaign level only.
  - `sms_messages` — synthetic step likewise.
- Memberships: `business.channel_campaign_step_recipients` — group by `status`.
- Unique recipients per channel: `COUNT(DISTINCT recipient_id)` over the artifact rows per channel.

**Outcome mapping (status → succeeded/failed/skipped):**
- `direct_mail_pieces.status`: `delivered` / `mailed` / `in_transit` / `processed_for_delivery` / `in_local_area` → succeeded; `failed` / `returned` / `rejected` → failed; everything else (`unknown`, `created`, `processing`) → skipped.
- `call_logs.outcome`: `qualified_transfer` / `interested` → succeeded; `do_not_call` / `failed` → failed; rest → skipped.
- `sms_messages.status`: `delivered` → succeeded; `failed` / `undelivered` → failed; rest → skipped.

**Synthetic-step note for voice/SMS:** when rolling up a voice or SMS channel_campaign, surface ONE synthetic step (step_order 0, name "(synthetic)", `external_provider_id` null) and include in the channel_campaign object: `"voice_step_attribution": "synthetic"` (or `"sms_step_attribution"`). Direct-mail channel_campaigns roll up real steps from `channel_campaign_steps`.

**Files:**
- Service: `app/services/campaign_analytics.py` — `summarize_campaign(*, organization_id, campaign_id, start, end) -> dict`. Raises `CampaignNotFound` if the campaign isn't in the caller's org.
- Models: extend `app/models/analytics.py` with `CampaignSummaryResponse` + nested types.
- Router: add `@router.get("/campaigns/{campaign_id}/summary")` to `app/routers/analytics.py`.
- Tests: `tests/test_campaign_analytics.py` — service + endpoint + cross-org guard + property test (sum of step events == channel_campaign events).

**Estimated:** ~600 LOC code + ~500 LOC tests.

---

### Slice 1c — Step funnel endpoint

**Goal:** per-step drilldown including the membership funnel.

**Endpoint:** `GET /api/v1/analytics/channel-campaign-steps/{step_id}/summary?from=&to=`

**Auth:** `require_org_context`. The step's `organization_id` (denormalized on the step row) must match the auth's org; else 404.

**Response shape:**

```json
{
  "step": {
    "id": "...",
    "channel_campaign_id": "...",
    "campaign_id": "...",
    "step_order": 1,
    "name": "...",
    "channel": "direct_mail",
    "provider": "lob",
    "external_provider_id": "cmp_...",
    "status": "scheduled",
    "scheduled_send_at": "...",
    "activated_at": "..."
  },
  "window": {"from": "...", "to": "..."},
  "events": {
    "total": 0,
    "by_event_type": {"piece.mailed": 0, "piece.in_transit": 0, ...},
    "outcomes": {"succeeded": 0, "failed": 0, "skipped": 0},
    "cost_total_cents": 0
  },
  "memberships": {
    "pending": 0,
    "scheduled": 0,
    "sent": 0,
    "failed": 0,
    "suppressed": 0,
    "cancelled": 0
  },
  "channel_specific": {
    "direct_mail": {
      "piece_funnel": {
        "queued": 0, "processed": 0, "in_transit": 0,
        "delivered": 0, "returned": 0, "failed": 0
      }
    }
  },
  "source": "postgres"
}
```

For voice/SMS steps (which today don't exist as real steps — they'd be synthetic), the endpoint should return 404 if the step_id doesn't resolve to a real `channel_campaign_steps` row.

**Data sources:**
- Step row from `business.channel_campaign_steps`.
- Memberships: `business.channel_campaign_step_recipients` grouped by `status`.
- For direct-mail steps: `direct_mail_pieces` filtered by `channel_campaign_step_id`; `direct_mail_piece_events` joined for `by_event_type`.

**Files:**
- Service: `app/services/step_analytics.py` — `summarize_step(*, organization_id, step_id, start, end) -> dict`.
- Models: add `StepSummaryResponse` to `app/models/analytics.py`.
- Router: add the endpoint.
- Tests: `tests/test_step_analytics.py` — funnel correctness, membership counts, cross-org guard.

**Estimated:** ~350 LOC code + ~350 LOC tests.

---

### Slice 1d — Recipient timeline endpoint

**Goal:** given a recipient_id, return all activity across all channels and channel_campaigns, in time order. The recipient's view of their own touchpoints with the org.

**Endpoint:** `GET /api/v1/analytics/recipients/{recipient_id}/timeline?from=&to=&limit=&offset=`

**Auth:** `require_org_context`. Recipients are strictly org-scoped (per `business.recipients.organization_id`). Cross-org access → 404. **Use a single SQL WHERE clause that combines `recipient_id` AND `organization_id` — no two-step lookup that could leak via 404 vs 200 timing.**

**Response shape:**

```json
{
  "recipient": {
    "id": "...",
    "organization_id": "...",
    "recipient_type": "business",
    "external_source": "fmcsa",
    "external_id": "123456",
    "display_name": "...",
    "created_at": "..."
  },
  "window": {"from": "...", "to": "..."},
  "summary": {
    "total_events": 0,
    "by_channel": {"direct_mail": 0, "voice_outbound": 0, "sms": 0},
    "campaigns_touched": 0,
    "channel_campaigns_touched": 0
  },
  "events": [
    {
      "occurred_at": "2026-04-15T...",
      "channel": "direct_mail",
      "provider": "lob",
      "event_type": "piece.delivered",
      "campaign_id": "...",
      "channel_campaign_id": "...",
      "channel_campaign_step_id": "...",
      "artifact_id": "...",
      "artifact_kind": "direct_mail_piece",
      "metadata": {}
    }
  ],
  "pagination": {"limit": 100, "offset": 0, "total": 0},
  "source": "postgres"
}
```

**Data sources:**
- Recipient row: `business.recipients` filtered by `(id, organization_id)`.
- Direct-mail events for the recipient: join `direct_mail_pieces` (filter `recipient_id = ?`) → `direct_mail_piece_events`.
- Voice events: today, `call_logs.recipient_id` is not populated. **Skip voice/SMS in the events list for now** — they'll appear automatically once the upstream wiring lands (out of scope §1.4). The `summary.by_channel` shows zero for those channels; document this in the endpoint docstring.
- Membership transitions: `channel_campaign_step_recipients` rows for this recipient — surface as synthetic `membership.{status}` events with `occurred_at = processed_at`.

**Pagination:** defaults `limit=100`, max `limit=500`. Order by `occurred_at DESC`.

**Files:**
- Service: `app/services/recipient_analytics.py` — `recipient_timeline(*, organization_id, recipient_id, start, end, limit, offset) -> dict`.
- Models: add `RecipientTimelineResponse` + nested event row.
- Router: add the endpoint.
- Tests: `tests/test_recipient_analytics.py` — covers normal timeline, cross-org guard (recipient in org B → 404 for org A user), pagination, time ordering.

**Estimated:** ~400 LOC code + ~400 LOC tests.

---

### Slice 1e — Direct-mail funnel endpoint

**Goal:** the direct-mail piece funnel rolled up at brand / channel_campaign / step granularity. Largest port from OEX's `/direct-mail` endpoint.

**Endpoint:** `GET /api/v1/analytics/direct-mail?brand_id=&channel_campaign_id=&channel_campaign_step_id=&from=&to=`

**Auth:** `require_org_context`. Filter by org-from-auth always; `brand_id` / `channel_campaign_id` / `channel_campaign_step_id` are optional drilldowns. Each must be in the auth's org or 404.

**Response shape (port from OEX with renames):**

```json
{
  "window": {"from": "...", "to": "..."},
  "totals": {
    "pieces": 0,
    "delivered": 0,
    "in_transit": 0,
    "returned": 0,
    "failed": 0,
    "test_mode_count": 0
  },
  "funnel": {
    "queued": 0,
    "processed": 0,
    "in_transit": 0,
    "delivered": 0,
    "returned": 0,
    "failed": 0
  },
  "by_piece_type": [
    {"piece_type": "postcard", "count": 0, "delivered": 0, "failed": 0}
  ],
  "daily_trends": [
    {"date": "2026-04-15", "created": 0, "delivered": 0, "failed": 0}
  ],
  "failure_reason_breakdown": [
    {"reason": "address_undeliverable", "count": 0}
  ],
  "source": "postgres"
}
```

**Reference:** [`/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py`](file:///Users/benjamincrane/outbound-engine-x/src/routers/analytics.py) — search for `/direct-mail`. ~350 LOC. Keep OEX's safety gates: max 93-day window (already in `_resolve_window`), `max_rows=20000` cap on raw piece reads, paginated `failure_reason_breakdown` and `daily_trends` (top 50, etc.).

**Field renames vs OEX:**
- `org_id` / `company_id` → `organization_id` / `brand_id` / `partner_id`
- `company_campaign_id` → `channel_campaign_id`
- New optional filter: `channel_campaign_step_id`.
- Filter scope: org from auth, never request param.

**Data sources:** `direct_mail_pieces` + `direct_mail_piece_events`. Postgres-only.

**Files:**
- Service: `app/services/direct_mail_analytics.py` — `summarize_direct_mail(*, organization_id, brand_id, channel_campaign_id, channel_campaign_step_id, start, end) -> dict`.
- Models: add `DirectMailAnalyticsResponse`.
- Router: add the endpoint.
- Tests: `tests/test_direct_mail_analytics.py` — funnel mapping, daily trend prefill, failure reason breakdown, all the drilldown filters, cross-org guards (brand in org B → 404, channel_campaign in org B → 404, etc.).

**Estimated:** ~600 LOC code + ~500 LOC tests.

---

### Slice 1f — Channel-campaign analytics endpoint

**Goal:** per-channel_campaign drilldown. Same shape as campaign rollup but scoped to one channel_campaign, with channel-specific extensions.

**Endpoint:** `GET /api/v1/analytics/channel-campaigns/{channel_campaign_id}/summary?from=&to=`

**Auth:** `require_org_context`. `channel_campaign_id` must be in auth's org → 404.

**Response shape:**

```json
{
  "channel_campaign": {
    "id": "...",
    "campaign_id": "...",
    "name": "...",
    "channel": "direct_mail",
    "provider": "lob",
    "status": "scheduled",
    "scheduled_send_at": "...",
    "brand_id": "...",
    "organization_id": "..."
  },
  "window": {"from": "...", "to": "..."},
  "totals": {
    "events_total": 0,
    "unique_recipients": 0,
    "cost_total_cents": 0
  },
  "outcomes": {"succeeded": 0, "failed": 0, "skipped": 0},
  "steps": [ /* same step shape as in slice 1b */ ],
  "channel_specific": {
    "direct_mail": {
      "piece_funnel": {"queued": 0, "processed": 0, "in_transit": 0, "delivered": 0, "returned": 0, "failed": 0}
    },
    "voice_outbound": {
      "transfer_rate": 0.0,
      "avg_duration_seconds": 0,
      "cost_breakdown": {"transport": 0, "stt": 0, "llm": 0, "tts": 0, "vapi": 0},
      "voice_step_attribution": "synthetic"
    },
    "sms": {
      "delivery_rate": 0.0,
      "opt_out_count": 0,
      "sms_step_attribution": "synthetic"
    },
    "email": {/* zeros — emailbison not wired */}
  },
  "source": "postgres"
}
```

**Note:** `channel_specific` only contains the section for the channel_campaign's actual channel. Don't return all four sections — return one.

**Data sources:** Same as slice 1b but scoped to one channel_campaign.

**Files:**
- Service: extend `app/services/campaign_analytics.py` (or a new `channel_campaign_analytics.py` if it gets unwieldy).
- Models: add `ChannelCampaignSummaryResponse`.
- Router: add the endpoint.
- Tests: `tests/test_channel_campaign_analytics.py` — voice + sms + direct_mail variants, cross-org guard.

**Estimated:** ~400 LOC code + ~400 LOC tests.

---

## 3. Phase 2 — RudderStack write integration

After all slices land. Separate commit + PR.

### 3.1 Add the SDK

```bash
uv add rudder-sdk-python
```

Verify it's the canonical PyPI package (`rudder-sdk-python`). The docs at `/Users/benjamincrane/api-reference-docs-new/rudderstack/sources/event-streams/sdks/rudderstack-python-sdk.md` confirm the API.

### 3.2 Config

Add to `app/config.py`:

```python
RUDDERSTACK_WRITE_KEY: SecretStr | None = None
RUDDERSTACK_DATA_PLANE_URL: str | None = None
```

Both already exist in Doppler `hq-x/dev` and `hq-x/prd`.

### 3.3 New module — `app/rudderstack.py`

Mirror the shape of `app/clickhouse.py`:

- `_is_configured()` — both env vars set.
- `_get_client()` — lazy singleton init.
- `track(event_name, *, anonymous_id, properties)` — fire-and-forget. Never raises.
- `flush()` — call on app shutdown to drain the SDK's batch queue.

Use the SDK exactly as documented:
```python
import rudderstack.analytics as rudder_analytics
rudder_analytics.write_key = settings.RUDDERSTACK_WRITE_KEY.get_secret_value()
rudder_analytics.dataPlaneUrl = settings.RUDDERSTACK_DATA_PLANE_URL
rudder_analytics.track(anonymous_id=..., event=event_name, properties=properties)
```

### 3.4 Wire into `emit_event()`

Edit [`app/services/analytics.py:74`](app/services/analytics.py:74) `emit_event()` to call `rudderstack.track()` after the existing log + ClickHouse writes:

- `event=event_name`
- `anonymous_id=str(organization_id)` (the only ID we have today; the admin user is the only user)
- `properties` = the full payload dict (six-tuple + `recipient_id` if present + extras)

Keep the order: log first, ClickHouse second (no-op without cluster), RudderStack third. Each in its own try/except. Never re-raise.

Update the module docstring to reflect that RudderStack is now wired (it currently says "intentionally a no-op shim today").

### 3.5 Lifespan flush

In [`app/main.py`](app/main.py) lifespan handler shutdown path:

```python
from app import rudderstack
...
async def lifespan(app):
    ...
    yield
    rudderstack.flush()  # drain in-flight events
    ...
```

Find the existing lifespan handler — there's already one for `init_pool`/`close_pool`. Add the rudder flush alongside `close_pool`.

### 3.6 Tests

`tests/test_rudderstack_client.py` — mock the SDK; verify:
- `track` called with the expected event/anonymous_id/properties when configured.
- Unconfigured → `_is_configured()` False, `track` is a no-op (never imports the SDK).
- SDK exception inside `track` doesn't propagate (caught and logged).
- `flush()` is a safe no-op when unconfigured.

Extend `tests/test_analytics_emit.py` (find or create) to assert `rudderstack.track` is called once per `emit_event()` when configured.

### 3.7 Verification

After deploying, fire a test event manually (e.g., trigger a Lob webhook or call `emit_event` from a script). Check the **Live Events** tab in the RudderStack source UI to confirm events arrive at the data plane. Document the verification step in the PR description.

**Estimated:** ~250 LOC code + ~200 LOC tests.

---

## 4. Definition of done (whole directive)

- All five slices (1b, 1c, 1d, 1e, 1f) merged to `main`.
- Phase 2 RudderStack merged to `main`.
- `uv run pytest -q` green at every step.
- `uv run ruff check` clean on every touched file at every step.
- A short post-ship summary at `docs/analytics-buildout-pr-notes.md` describing what shipped (six endpoints + RudderStack), what's deferred (ClickHouse provisioning, voice/SMS step+recipient wiring), and any caveats encountered.
- Update [`AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md`](AUDIT_RUDDERSTACK_CLICKHOUSE_PORT.md) and [`AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md`](AUDIT_CLICKHOUSE_QUERY_AND_MULTICHANNEL_ANALYTICS.md) — add a note at the top of each saying "superseded by `DIRECTIVE_HQX_ANALYTICS_REMAINDER.md`; ClickHouse out of scope as of [date]; analytics endpoints shipped via PRs #X #Y #Z."

---

## 5. Working order (recommended)

1. **Read** `docs/campaign-rename-pr-notes.md` and `docs/lob-integration.md` end to end.
2. **Read** [`tests/test_reliability_analytics.py`](tests/test_reliability_analytics.py) to internalize the test pattern.
3. **Read** [`app/routers/analytics.py`](app/routers/analytics.py) and [`app/services/reliability_analytics.py`](app/services/reliability_analytics.py) to internalize the slice 1a pattern.
4. **Build slice 1b** (campaign rollup). Ship as a PR. Wait for green CI / get it merged.
5. Repeat for 1c, 1d, 1e, 1f in order.
6. Build Phase 2 (RudderStack). Ship as final PR.
7. Write the post-ship summary doc, update the audit notes.

If any slice hits a real architectural snag (not a routine bug), STOP and surface it in the PR description rather than improvising a workaround. The directive should be enough — but the schema can change under you, and "I assumed X but the table doesn't have that column" is the kind of thing worth flagging instead of jamming through.

---

## 6. Style + conventions

- Follow ruff config in `pyproject.toml` — line length 100, target py312, lint set `["E", "F", "I", "W", "UP", "B"]`.
- File header docstrings in the style of slice 1a — explain why the module exists, what's in scope, what's deferred.
- No new emojis in code, comments, or commit messages.
- Commit messages: short imperative subject (under 72 chars), then a blank line, then 1–3 paragraphs explaining the why and the org-isolation guarantee. End with `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`.
- PR descriptions: use the template from §9 of [`DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md`](DIRECTIVE_HQX_ANALYTICS_BUILDOUT.md) but trimmed for the actually-relevant checkboxes (drop the ClickHouse and RudderStack ones for slices 1b–1f; include them for the RudderStack PR).

---

## 7. Reference paths cheat sheet

| What | Where |
|---|---|
| Canonical hierarchy doc | [docs/campaign-rename-pr-notes.md](docs/campaign-rename-pr-notes.md) |
| Direct-mail integration depth | [docs/lob-integration.md](docs/lob-integration.md) |
| Tenancy + auth | [docs/tenancy-model.md](docs/tenancy-model.md), [app/auth/roles.py](app/auth/roles.py) |
| The `emit_event()` chokepoint | [app/services/analytics.py](app/services/analytics.py) |
| Step context resolver | [app/services/channel_campaign_steps.py](app/services/channel_campaign_steps.py) (`get_step_context`) |
| Recipient + memberships | [app/services/recipients.py](app/services/recipients.py) |
| Lob adapter (provider chokepoint) | [app/providers/lob/adapter.py](app/providers/lob/adapter.py) |
| Lob webhook projector | [app/webhooks/lob_processor.py](app/webhooks/lob_processor.py) |
| Slice 1a service template | [app/services/reliability_analytics.py](app/services/reliability_analytics.py) |
| Slice 1a router template | [app/routers/analytics.py](app/routers/analytics.py) |
| Slice 1a test template | [tests/test_reliability_analytics.py](tests/test_reliability_analytics.py) |
| OEX direct-mail funnel reference | `/Users/benjamincrane/outbound-engine-x/src/routers/analytics.py` |
| OEX voice analytics reference | `/Users/benjamincrane/outbound-engine-x/src/routers/voice_analytics.py` |
| RudderStack Python SDK docs | `/Users/benjamincrane/api-reference-docs-new/rudderstack/sources/event-streams/sdks/rudderstack-python-sdk.md` |

---

**End of directive.** Total estimated remaining work: ~3,200 LOC across 6 PRs. Ship them in order, one at a time, with full test coverage and lint clean at every step.
